"""PostgreSQL RPC contract tests for schema version 1.

The database is a throwaway Docker database. These tests do not read or write
~/.marketreview.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from marketreview.schema import ATOMIC_FIELD_NAMES

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "contracts" / "supabase_rpc_v1.json"
MIGRATIONS = [
    ROOT / "sql" / "migrations" / "0001_marketreview_v1.sql",
    ROOT / "sql" / "migrations" / "0002_marketreview_v1_rpc.sql",
    ROOT / "sql" / "migrations" / "0003_marketreview_v1_sync.sql",
]
CONTAINER = "dmr-pg-rpc-v1"
DATABASE = "marketreview_v1_test"
BATCH = "2026-08-21T07:00:00+00:00"
BATCH_2 = "2026-08-21T08:00:00+00:00"


def _split_sql(script: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(script)
    dollar: str | None = None
    in_single = False
    while i < n:
        if dollar is not None:
            if script.startswith(dollar, i):
                buf.append(dollar)
                i += len(dollar)
                dollar = None
                continue
            buf.append(script[i])
            i += 1
            continue
        if in_single:
            buf.append(script[i])
            if script[i] == "'":
                if i + 1 < n and script[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_single = False
            i += 1
            continue
        if script.startswith("--", i):
            end = script.find("\n", i)
            if end < 0:
                break
            i = end + 1
            continue
        if script[i] == "'":
            in_single = True
            buf.append("'")
            i += 1
            continue
        if script[i] == "$":
            match = re.match(r"\$[A-Za-z0-9_]*\$", script[i:])
            if match:
                dollar = match.group(0)
                buf.append(dollar)
                i += len(dollar)
                continue
        if script[i] == ";":
            text = "".join(buf).strip()
            if text:
                statements.append(text)
            buf = []
            i += 1
            continue
        buf.append(script[i])
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    completed = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


class TestFrozenContract(unittest.TestCase):
    def test_contract_matches_issue_rules(self) -> None:
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(contract["schema_version"], 1)
        self.assertEqual(contract["comparison"]["business_equality"]["float"]["rel_tol"], 1e-12)
        self.assertEqual(contract["comparison"]["business_equality"]["float"]["abs_tol"], 1e-9)
        self.assertEqual(
            contract["comparison"]["missing_baseline_means"],
            "no_baseline",
        )
        self.assertIn("independent_copy_shares_ledger_identity", contract["comparison"]["stop_not_first_join"])
        self.assertFalse(contract["sync_commit"]["exists_false"]["writes_business_rows"])
        self.assertFalse(contract["sync_commit"]["exists_false"]["constructs_timestamps"])
        self.assertEqual(
            set(contract["sync_report"]["pending_classes"]),
            {"download", "conflict", "local_delete"},
        )
        self.assertTrue(contract["upgrade"]["must_not_backfill_delete_history_for_rows_absent_before_upgrade"])
        self.assertIn("marketreview_sync_commit", contract["functions"])
        self.assertIn("marketreview_sync_snapshot", contract["functions"])
        self.assertEqual(set(contract["rpcs"]), set(contract["functions"]))
        for name, rpc in contract["rpcs"].items():
            self.assertEqual(rpc["request"]["required"]["schema_version"], "integer", name)
            self.assertIn("format_version", rpc["response"]["required"])
            self.assertGreaterEqual(len(rpc["errors"]), 1, name)
            for error in rpc["errors"]:
                self.assertIn(error["code"], contract["errors"]["codes"])
                self.assertTrue(error["message"].startswith(f"[{error['code']}]"))
        commit = contract["rpcs"]["marketreview_sync_commit"]
        self.assertEqual(commit["group"]["required"]["exists"], "boolean")
        self.assertEqual(commit["group"]["missing_key"], "reject_before_delete")
        preimage = contract["rpcs"]["marketreview_replace_direction_preimage"]
        self.assertIn("old", preimage["response"]["required"])
        self.assertIn("new", preimage["response"]["required"])
        self.assertTrue(preimage["returns_both_states_when_source_is_absent"])

    def test_migration_names_cover_the_contract(self) -> None:
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        sql = "\n".join(path.read_text(encoding="utf-8") for path in MIGRATIONS)
        for name in contract["functions"]:
            short = name.removeprefix("marketreview_")
            self.assertIn(f"'{short}'", sql)
        self.assertNotIn("~/.marketreview", sql)


class PostgresRpcTest(unittest.TestCase):
    _pg_port: str | None = None
    _backend = ""

    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("MARKETREVIEW_PG_TEST") == "0":
            raise unittest.SkipTest("MARKETREVIEW_PG_TEST=0")
        port = os.environ.get("MARKETREVIEW_PGPORT")
        if port:
            cls._backend = "psycopg"
            cls._pg_port = port
        elif _docker_ready():
            cls._backend = "docker"
            cls._pg_port = None
            subprocess.run(
                ["docker", "rm", "-f", CONTAINER],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    CONTAINER,
                    "-e",
                    "POSTGRES_HOST_AUTH_METHOD=trust",
                    "-e",
                    "POSTGRES_PASSWORD=unused",
                    "postgres:16",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            for _ in range(40):
                ready = subprocess.run(
                    ["docker", "exec", CONTAINER, "pg_isready", "-U", "postgres"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if ready.returncode == 0:
                    break
                time.sleep(0.5)
            else:
                raise unittest.SkipTest("PostgreSQL 容器未在时限内就绪")
        else:
            raise unittest.SkipTest("需要已启动的 Docker，或 MARKETREVIEW_PGPORT 指向隔离 PostgreSQL")
        if cls._backend == "psycopg":
            cls._psql("DROP DATABASE IF EXISTS marketreview_v1_test", database="postgres")
        cls._psql("CREATE DATABASE marketreview_v1_test", database="postgres")
        migration = "\n".join(path.read_text(encoding="utf-8") for path in MIGRATIONS)
        cls._psql(migration, database=DATABASE)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._backend == "docker":
            subprocess.run(
                ["docker", "rm", "-f", CONTAINER],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    def setUp(self) -> None:
        self._psql(
            """
            TRUNCATE
              marketreview.group_change_history,
              marketreview.sync_commit_result,
              marketreview.daily_market_review,
              marketreview.daily_price_limit_event
            RESTART IDENTITY CASCADE;
            UPDATE marketreview.ledger SET revision = 0 WHERE ledger_key = 'main';
            """
        )

    @classmethod
    def _execute(cls, sql: str, database: str) -> tuple[int, str, str]:
        if cls._pg_port:
            return cls._execute_psycopg(sql, database)
        completed = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                CONTAINER,
                "psql",
                "-U",
                "postgres",
                "-d",
                database,
                "-v",
                "ON_ERROR_STOP=1",
                "-X",
                "-q",
                "-t",
                "-A",
            ],
            input=sql,
            text=True,
            capture_output=True,
            check=False,
        )
        return completed.returncode, completed.stdout, completed.stderr

    @classmethod
    def _execute_psycopg(cls, sql: str, database: str) -> tuple[int, str, str]:
        import psycopg

        lines: list[str] = []
        try:
            with psycopg.connect(
                host="127.0.0.1",
                port=int(cls._pg_port or "5432"),
                user="postgres",
                dbname=database,
                autocommit=True,
            ) as conn:
                for statement in _split_sql(sql):
                    with conn.cursor() as cursor:
                        cursor.execute(statement)
                        if cursor.description:
                            for row in cursor.fetchall():
                                cells = []
                                for value in row:
                                    if isinstance(value, (dict, list)):
                                        cells.append(json.dumps(value, ensure_ascii=False))
                                    elif value is None:
                                        cells.append("")
                                    else:
                                        cells.append(str(value))
                                lines.append("\t".join(cells))
        except psycopg.Error as exc:
            hint = getattr(exc.diag, "hint", None) or ""
            primary = getattr(exc.diag, "message_primary", None) or str(exc)
            stderr = f"ERROR:  {primary}\nHINT:  {hint}\nSQLSTATE: {exc.sqlstate}\n"
            return 1, "", stderr
        return 0, "\n".join(lines), ""

    @classmethod
    def _psql(cls, sql: str, database: str = DATABASE) -> str:
        code, stdout, stderr = cls._execute(sql, database)
        if code != 0:
            raise AssertionError(f"{stderr}\n{stdout}")
        return stdout.strip()

    def _rpc(self, name: str, payload: dict, role: str = "service_role") -> dict:
        body = json.dumps(payload, ensure_ascii=False)
        sql = f"SET ROLE {role};\nSELECT public.{name}($json${body}$json$::jsonb);"
        return json.loads(self._psql(sql))

    def _rpc_error(self, name: str, payload: dict, role: str = "service_role") -> str:
        body = json.dumps(payload, ensure_ascii=False)
        sql = f"SET ROLE {role};\nSELECT public.{name}($json${body}$json$::jsonb);"
        code, stdout, stderr = self._execute(sql, DATABASE)
        self.assertNotEqual(code, 0, stdout)
        return stderr

    def _probe_revision(self) -> int:
        return int(self._rpc("marketreview_probe", {"schema_version": 1})["revision"])

    def _snapshot(self) -> dict:
        return self._rpc("marketreview_sync_snapshot", {"schema_version": 1})

    def test_public_functions_match_contract_and_anon_is_rejected(self) -> None:
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        rows = self._psql(
            """
            SELECT proname
            FROM pg_proc
            JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
            WHERE nspname = 'public' AND proname LIKE 'marketreview_%'
            ORDER BY proname
            """
        )
        names = [line for line in rows.splitlines() if line]
        self.assertEqual(names, sorted(contract["functions"]))
        error = self._rpc_error("marketreview_probe", {"schema_version": 1}, role="anon")
        self.assertIn("permission denied", error)
        history = self._psql("SELECT count(*) FROM marketreview.group_change_history")
        self.assertEqual(history, "0")

    def test_upgrade_insert_does_not_fabricate_delete_history(self) -> None:
        self._psql(
            """
            INSERT INTO marketreview.daily_market_review (trade_date, created_at, updated_at)
            VALUES ('2026-08-21', '2026-08-21T07:00:00+00:00', '2026-08-21T07:00:00+00:00');
            """
        )
        snapshot = self._snapshot()
        self.assertEqual(snapshot["counts"]["history"], 0)
        self.assertEqual(snapshot["counts"]["reviews"], 1)
        self.assertEqual(snapshot["history"], [])

    def test_review_patch_distinguishes_missing_null_and_zero(self) -> None:
        created = self._rpc(
            "marketreview_save_review",
            {
                "schema_version": 1,
                "trade_date": "2026-08-21",
                "batch_time": BATCH,
                "fields": {"advancing_count": 0, "pullback_count": None, "median_change_pct": 1.5},
            },
        )
        self.assertFalse(created["noop"])
        self.assertEqual(created["revision"], 1)
        patched = self._rpc(
            "marketreview_save_review",
            {
                "schema_version": 1,
                "trade_date": "2026-08-21",
                "batch_time": BATCH_2,
                "fields": {"declining_count": 3},
            },
        )
        self.assertEqual(patched["revision"], 2)
        review = self._snapshot()["reviews"][0]
        self.assertEqual(review["advancing_count"], 0)
        self.assertIsNone(review["pullback_count"])
        self.assertEqual(review["median_change_pct"], 1.5)
        self.assertEqual(review["declining_count"], 3)
        self.assertEqual(review["created_at"], BATCH)
        self.assertEqual(review["updated_at"], BATCH_2)
        self.assertEqual(set(ATOMIC_FIELD_NAMES) - set(review), set())

    def test_boolean_encoding_and_parent_rollback(self) -> None:
        self._rpc(
            "marketreview_save_events",
            {
                "schema_version": 1,
                "trade_date": "2026-08-21",
                "batch_time": BATCH,
                "events": [
                    {
                        "market": "sh",
                        "code": "600519",
                        "name": "贵州茅台",
                        "direction": "up",
                        "closed_at_limit": False,
                        "limit_rate_bp": 1000,
                        "streak_height": 2,
                    }
                ],
            },
        )
        self._rpc(
            "marketreview_save_event_details",
            {
                "schema_version": 1,
                "trade_date": "2026-08-21",
                "batch_time": BATCH,
                "details": [
                    {
                        "market": "sh",
                        "code": "600519",
                        "direction": "up",
                        "is_leader": False,
                        "note": None,
                        "sectors": ["白酒", "消费"],
                    }
                ],
            },
        )
        day = self._rpc(
            "marketreview_get_day",
            {"schema_version": 1, "trade_date": "2026-08-21", "previous_trade_date": "2026-08-20"},
        )
        self.assertFalse(day["events"][0]["closed_at_limit"])
        self.assertFalse(day["details"][0]["is_leader"])
        self.assertEqual(day["counts"]["events"], 1)
        self.assertTrue(day["complete"])
        error = self._rpc_error(
            "marketreview_save_event_details",
            {
                "schema_version": 1,
                "trade_date": "2026-08-21",
                "batch_time": BATCH_2,
                "details": [
                    {"market": "sh", "code": "600519", "direction": "up", "is_leader": "false"}
                ],
            },
        )
        self.assertIn("INVALID_TYPE", error)
        self.assertEqual(self._probe_revision(), 2)
        error = self._rpc_error(
            "marketreview_save_event_details",
            {
                "schema_version": 1,
                "trade_date": "2026-08-21",
                "batch_time": BATCH_2,
                "details": [
                    {"market": "sh", "code": "600519", "direction": "up", "note": "保留"},
                    {"market": "sz", "code": "000001", "direction": "up", "note": "无父事件"},
                ],
            },
        )
        self.assertIn("PARENT_EVENT_MISSING", error)
        self.assertEqual(self._snapshot()["details"][0]["note"], None)
        self.assertEqual(self._probe_revision(), 2)

    def test_direction_replace_keeps_detail_existence_and_rewrites_timestamps(self) -> None:
        self._rpc(
            "marketreview_save_events",
            {
                "schema_version": 1,
                "trade_date": "2026-08-21",
                "batch_time": BATCH,
                "events": [
                    {
                        "market": "sh",
                        "code": "600519",
                        "name": "贵州茅台",
                        "direction": "up",
                        "closed_at_limit": True,
                        "limit_rate_bp": 1000,
                        "streak_height": 1,
                    }
                ],
            },
        )
        self._rpc(
            "marketreview_save_event_details",
            {
                "schema_version": 1,
                "trade_date": "2026-08-21",
                "batch_time": BATCH,
                "details": [
                    {
                        "market": "sh",
                        "code": "600519",
                        "direction": "up",
                        "sectors": ["白酒"],
                        "limit_up_reasons": ["业绩", "提价"],
                    }
                ],
            },
        )
        preimage = self._rpc(
            "marketreview_replace_direction_preimage",
            {
                "schema_version": 1,
                "trade_date": "2026-08-21",
                "market": "sh",
                "code": "600519",
                "old_direction": "up",
                "new_direction": "down",
            },
        )
        self.assertFalse(preimage["old"]["detail_exists"])
        self.assertEqual(preimage["old"]["sectors"], ["白酒"])
        self.assertEqual(preimage["old"]["reasons"], ["业绩", "提价"])
        replaced = self._rpc(
            "marketreview_replace_direction",
            {
                "schema_version": 1,
                "trade_date": "2026-08-21",
                "batch_time": BATCH_2,
                "market": "sh",
                "code": "600519",
                "old_direction": "up",
                "event": {
                    "market": "sh",
                    "code": "600519",
                    "name": "贵州茅台",
                    "direction": "down",
                    "closed_at_limit": True,
                    "limit_rate_bp": 1000,
                    "streak_height": 1,
                },
            },
        )
        self.assertEqual(replaced["revision"], 3)
        snapshot = self._snapshot()
        self.assertEqual(snapshot["events"][0]["direction"], "down")
        self.assertEqual(snapshot["events"][0]["created_at"], BATCH_2)
        self.assertEqual(snapshot["counts"]["details"], 0)
        self.assertEqual(snapshot["counts"]["reasons"], 0)
        self.assertEqual([row["value"] for row in snapshot["sectors"]], ["白酒"])
        self.assertEqual(snapshot["history"][-1]["change_kind"], "direction_replace")
        verified = self._rpc(
            "marketreview_replace_direction_preimage",
            {
                "schema_version": 1,
                "trade_date": "2026-08-21",
                "market": "sh",
                "code": "600519",
                "old_direction": "up",
                "new_direction": "down",
            },
        )
        self.assertFalse(verified["old"]["exists"])
        self.assertTrue(verified["new"]["exists"])
        self.assertEqual(verified["new"]["event"]["direction"], "down")
        self.assertEqual(verified["new"]["event"]["created_at"], BATCH_2)
        self.assertEqual(verified["new"]["reasons"], [])
        self.assertEqual(verified["new"]["sectors"], ["白酒"])

    def test_missing_sync_keys_do_not_delete(self) -> None:
        self._rpc(
            "marketreview_save_review",
            {
                "schema_version": 1,
                "trade_date": "2026-08-21",
                "batch_time": BATCH,
                "fields": {"advancing_count": 3},
            },
        )
        self._rpc(
            "marketreview_save_events",
            {
                "schema_version": 1,
                "trade_date": "2026-08-21",
                "batch_time": BATCH,
                "events": [
                    {
                        "market": "sh",
                        "code": "600519",
                        "name": "贵州茅台",
                        "direction": "up",
                        "closed_at_limit": True,
                        "limit_rate_bp": 1000,
                        "streak_height": 1,
                    }
                ],
            },
        )
        missing_exists = self._rpc_error(
            "marketreview_sync_commit",
            {
                "schema_version": 1,
                "operation_id": "op-missing-exists",
                "project_id": "project",
                "ledger_id": "ledger",
                "request_digest": "digest-missing-exists",
                "expected_revision": 2,
                "groups": [
                    {"group_kind": "review", "group_key": {"trade_date": "2026-08-21"}}
                ],
            },
        )
        self.assertIn("INVALID_TYPE", missing_exists)
        missing_events = self._rpc_error(
            "marketreview_sync_commit",
            {
                "schema_version": 1,
                "operation_id": "op-missing-events",
                "project_id": "project",
                "ledger_id": "ledger",
                "request_digest": "digest-missing-events",
                "expected_revision": 2,
                "groups": [
                    {
                        "group_kind": "event",
                        "group_key": {"trade_date": "2026-08-21", "market": "sh", "code": "600519"},
                        "exists": True,
                    }
                ],
            },
        )
        self.assertIn("INVALID_REQUEST", missing_events)
        snapshot = self._snapshot()
        self.assertEqual(snapshot["counts"]["reviews"], 1)
        self.assertEqual(snapshot["counts"]["events"], 1)
        self.assertEqual(snapshot["revision"], 2)

    def test_save_review_without_schema_version_is_rejected(self) -> None:
        error = self._rpc_error(
            "marketreview_save_review",
            {"trade_date": "2026-08-21", "batch_time": BATCH, "fields": {"advancing_count": 1}},
        )
        self.assertIn("INVALID_REQUEST", error)
        self.assertIn("schema_version", error)
        self.assertEqual(self._probe_revision(), 0)
        self.assertEqual(self._snapshot()["counts"]["reviews"], 0)

    def test_explicit_delete_writes_no_business_row_and_rolls_back_with_version_conflict(self) -> None:
        review = self._blank_review("2026-08-21")
        event_group = self._event_group()
        first = self._commit([review, event_group], operation_id="op-1", digest="digest-1", expected=0)
        self.assertEqual(first["committed_revision"], 1)
        self.assertTrue(first["groups"][0]["exists"])
        replay = self._commit([review, event_group], operation_id="op-1", digest="digest-1", expected=999)
        self.assertEqual(replay, first)
        self.assertEqual(self._probe_revision(), 1)
        mismatch = self._rpc_error(
            "marketreview_sync_commit",
            self._commit_payload([review], operation_id="op-1", digest="digest-2", expected=1),
        )
        self.assertIn("OPERATION_DIGEST_MISMATCH", mismatch)
        deleted = self._commit(
            [
                {"group_kind": "review", "group_key": {"trade_date": "2026-08-21"}, "exists": False},
                {"group_kind": "event", "group_key": event_group["group_key"], "exists": False},
            ],
            operation_id="op-delete",
            digest="digest-delete",
            expected=1,
        )
        self.assertEqual(set(deleted["groups"][0]), {"group_kind", "group_key", "exists"})
        self.assertFalse(deleted["groups"][0]["exists"])
        self.assertNotIn("created_at", deleted["groups"][1])
        snapshot = self._snapshot()
        self.assertEqual(snapshot["counts"]["reviews"], 0)
        self.assertEqual(snapshot["counts"]["events"], 0)
        self.assertTrue(all(row["change_kind"] == "delete" for row in snapshot["history"][-2:]))
        conflict = self._rpc_error(
            "marketreview_sync_commit",
            self._commit_payload([review], operation_id="op-stale", digest="digest-stale", expected=0),
        )
        self.assertIn("REVISION_CONFLICT", conflict)
        self.assertEqual(snapshot["counts"]["reviews"], self._snapshot()["counts"]["reviews"])
        empty = self._rpc_error(
            "marketreview_sync_commit",
            self._commit_payload([], operation_id="op-empty", digest="digest-empty", expected=2),
        )
        self.assertIn("EMPTY_COMMIT", empty)

    def test_partial_group_failure_rolls_back_the_whole_commit(self) -> None:
        bad_event = self._event_group()
        bad_event["events"][0]["limit_rate_bp"] = 1500
        error = self._rpc_error(
            "marketreview_sync_commit",
            self._commit_payload(
                [self._blank_review("2026-08-21"), bad_event],
                operation_id="op-batch",
                digest="digest-batch",
                expected=0,
            ),
        )
        self.assertNotIn("OPERATION_NOT_FOUND", error)
        self.assertEqual(self._probe_revision(), 0)
        self.assertEqual(self._snapshot()["counts"]["reviews"], 0)
        missing = self._rpc_error("marketreview_sync_result", {"schema_version": 1, "operation_id": "op-batch"})
        self.assertIn("OPERATION_NOT_FOUND", missing)

    def test_delete_review_does_not_cascade_to_events(self) -> None:
        self._commit([self._blank_review("2026-08-21"), self._event_group()], operation_id="op-both", digest="d", expected=0)
        deleted = self._rpc(
            "marketreview_delete_review",
            {"schema_version": 1, "trade_date": "2026-08-21", "batch_time": BATCH_2},
        )
        self.assertFalse(deleted["noop"])
        snapshot = self._snapshot()
        self.assertEqual(snapshot["counts"]["reviews"], 0)
        self.assertEqual(snapshot["counts"]["events"], 1)

    def test_list_events_over_1000_is_complete(self) -> None:
        self._psql(
            """
            INSERT INTO marketreview.daily_price_limit_event (
              trade_date, market, code, name, direction, closed_at_limit,
              limit_rate_bp, streak_height, created_at, updated_at
            )
            SELECT
              '2026-08-21',
              'sh',
              lpad(gs::text, 6, '0'),
              '名称',
              'up',
              1,
              1000,
              1,
              '2026-08-21T07:00:00+00:00',
              '2026-08-21T07:00:00+00:00'
            FROM generate_series(1, 1201) AS gs;
            """
        )
        listed = self._rpc("marketreview_list_events", {"schema_version": 1})
        self.assertTrue(listed["complete"])
        self.assertEqual(listed["counts"]["events"], 1201)
        self.assertEqual(len(listed["events"]), 1201)

    def _blank_review(self, day: str) -> dict:
        review = {name: None for name in sorted(ATOMIC_FIELD_NAMES)}
        review.update({"trade_date": day, "created_at": BATCH, "updated_at": BATCH, "advancing_count": 1})
        return {
            "group_kind": "review",
            "group_key": {"trade_date": day},
            "exists": True,
            "review": review,
        }

    def _event_group(self) -> dict:
        return {
            "group_kind": "event",
            "group_key": {"trade_date": "2026-08-21", "market": "sh", "code": "600519"},
            "exists": True,
            "events": [
                {
                    "direction": "up",
                    "name": "贵州茅台",
                    "closed_at_limit": True,
                    "limit_rate_bp": 1000,
                    "streak_height": 2,
                    "created_at": BATCH,
                    "updated_at": BATCH,
                    "detail_exists": True,
                    "detail": {
                        "previous_turnover_amount": None,
                        "auction_amount": None,
                        "previous_close": None,
                        "open_price": None,
                        "turnover_amount": None,
                        "turnover_rate": None,
                        "is_leader": None,
                        "note": None,
                        "created_at": BATCH,
                        "updated_at": BATCH,
                    },
                    "sectors": [],
                    "limit_up_reasons": ["业绩"],
                }
            ],
        }

    def _commit_payload(self, groups: list, operation_id: str, digest: str, expected: int) -> dict:
        return {
            "schema_version": 1,
            "operation_id": operation_id,
            "project_id": "project-1",
            "ledger_id": "ledger-1",
            "request_digest": digest,
            "expected_revision": expected,
            "groups": groups,
        }

    def _commit(self, groups: list, operation_id: str, digest: str, expected: int) -> dict:
        return self._rpc(
            "marketreview_sync_commit",
            self._commit_payload(groups, operation_id, digest, expected),
        )


if __name__ == "__main__":
    unittest.main()
