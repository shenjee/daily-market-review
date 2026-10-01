"""pg_dump backup and blank-database restore drill.

The PostgreSQL process is a temporary local cluster. These tests do not start
Docker, do not read ~/.marketreview, and do not set a database password.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401

from marketreview.backend import CLOUD_DEFAULT_ENABLED
from marketreview.pg_backup import (
    DUMP_FLAGS,
    ROLES_SQL,
    PgBackupError,
    PgConn,
    connection_from_env,
    copy_records,
    create_backup,
    dump_marketreview_sql,
    prune_backups,
    psql,
    psql_script,
    redact,
    resolve_pg_bin,
    restore_into_blank,
    rpc,
    rpc_error,
    validate_dump,
    verify_restore,
    _catalog,
)
from marketreview.schema import ATOMIC_FIELD_NAMES
from marketreview.write_gate import FLOAT_ABS_TOL, same_json_value

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "sql" / "migrations"
BATCH = "2026-08-21T07:00:00+00:00"
BATCH_2 = "2026-08-21T08:00:00+00:00"
MARKER = "备份演练标记"


def _minimal_dump() -> str:
    parts = []
    for table in (
        "schema_meta",
        "ledger",
        "group_change_history",
        "sync_commit_result",
        "daily_market_review",
        "daily_price_limit_event",
        "daily_price_limit_event_detail",
        "daily_price_limit_event_sector",
        "daily_price_limit_event_reason",
    ):
        parts.append(f"CREATE TABLE marketreview.{table} (id integer);")
        parts.append(f"COPY marketreview.{table} (id) FROM stdin;")
        parts.append(r"\.")
    parts.append("CREATE FUNCTION marketreview.sync_commit(p_request jsonb) RETURNS jsonb AS $$ SELECT '{}'::jsonb; $$;")
    parts.append("CREATE FUNCTION marketreview.lock_ledger() RETURNS bigint AS $$ SELECT 0::bigint; $$;")
    parts.append("REVOKE ALL ON SCHEMA marketreview FROM PUBLIC;")
    parts.append("GRANT SELECT ON ALL TABLES IN SCHEMA marketreview TO service_role;")
    parts.append("result_payload jsonb")
    return "\n".join(parts)


def _bundle(root: Path, created_at: str, retention: str = "daily") -> None:
    directory = root / created_at.replace(":", "").replace("-", "")
    directory.mkdir()
    (directory / "BACKUP_OK").write_text("ok\n", encoding="utf-8")
    (directory / "manifest.json").write_text(
        json.dumps({"created_at": created_at, "retention": retention}),
        encoding="utf-8",
    )


class TestBackupPolicy(unittest.TestCase):
    def test_cloud_default_stays_sqlite_and_tool_ignores_secret_files(self) -> None:
        self.assertFalse(CLOUD_DEFAULT_ENABLED)
        source = (ROOT / "scripts" / "marketreview" / "pg_backup.py").read_text(encoding="utf-8")
        self.assertNotIn("supabase.secret", source)
        self.assertNotIn("SUPABASE_SECRET", source)
        self.assertNotIn("--table", DUMP_FLAGS)

    def test_redact_removes_only_the_supplied_value(self) -> None:
        secret = "pw-" + ("x" * 12)
        self.assertNotIn(secret, redact(f"失败 {secret}", secret))
        self.assertEqual(redact("普通错误", None), "普通错误")

    def test_dump_validator_rejects_role_passwords_and_platform_objects(self) -> None:
        validate_dump(_minimal_dump())
        with self.assertRaises(PgBackupError):
            validate_dump(_minimal_dump() + "\nCREATE ROLE app LOGIN PASSWORD 'hidden';")
        with self.assertRaises(PgBackupError):
            validate_dump(_minimal_dump() + "\nCOPY auth.users (id) FROM stdin;")

    def test_dump_validator_requires_functions_and_infrastructure(self) -> None:
        sql = _minimal_dump().replace("CREATE FUNCTION marketreview.sync_commit", "CREATE FUNCTION marketreview.other")
        with self.assertRaises(PgBackupError):
            validate_dump(sql)
        records = copy_records("COPY marketreview.ledger (ledger_key, revision) FROM stdin;\nmain\t2\n\\.\n")
        self.assertEqual(records["ledger"][1], ["main\t2"])

    def test_roles_sql_includes_postgres_stub_for_default_acl(self) -> None:
        # Homebrew 等本机集群超级用户常不是 postgres；云端 dump 的
        # DEFAULT PRIVILEGES FOR ROLE postgres 依赖此 stub。
        self.assertRegex(ROLES_SQL, r"CREATE ROLE postgres NOLOGIN")
        self.assertRegex(ROLES_SQL, r"CREATE ROLE anon NOLOGIN")
        self.assertRegex(ROLES_SQL, r"CREATE ROLE authenticated NOLOGIN")
        self.assertRegex(ROLES_SQL, r"CREATE ROLE service_role NOLOGIN")
        self.assertNotRegex(ROLES_SQL, r"(?i)password\s+'")
        self.assertNotRegex(ROLES_SQL, r"(?i)\bLOGIN\b")

    def test_connection_user_allows_session_pooler_dotted_name(self) -> None:
        env = {
            "PGHOST": "aws-0-ap-southeast-1.pooler.supabase.com",
            "PGPORT": "5432",
            "PGUSER": "postgres.nyscgdxrctwchbzclszt",
            "PGDATABASE": "postgres",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            conn = connection_from_env(database="postgres")
        self.assertEqual(conn.user, "postgres.nyscgdxrctwchbzclszt")
        with mock.patch.dict(os.environ, {**env, "PGUSER": "bad user"}, clear=False):
            with self.assertRaises(PgBackupError):
                connection_from_env(database="postgres")

    def test_verify_restore_accepts_float_ulp_drift_via_same_json_value(self) -> None:
        # dump/restore 经 float8 文本往返后 jsonb 字面量可差 1 ULP；
        # verify_restore 必须用同步合同容差，不能要求 dict 全等。
        source = (ROOT / "scripts" / "marketreview" / "pg_backup.py").read_text(encoding="utf-8")
        self.assertIn("same_json_value(actual_snapshot, expected_snapshot)", source)
        self.assertNotIn("actual_snapshot != expected_snapshot", source)
        left = {"turnover_rate": 0.0682766914367676}
        right = {"turnover_rate": 0.06827669143676758}
        self.assertNotEqual(left, right)
        self.assertLess(abs(left["turnover_rate"] - right["turnover_rate"]), FLOAT_ABS_TOL)
        self.assertTrue(same_json_value(left, right))
        self.assertFalse(same_json_value(left, {"turnover_rate": 0.07}))

    def test_catalog_query_excludes_pg18_not_null_contype(self) -> None:
        # PG18 把 NOT NULL 记为 contype='n'；云端 17 dump 的 catalog 无这些名字。
        # 核验改比 attnotnull 列属性，见 _catalog 的 columns 清单。
        source = (ROOT / "scripts" / "marketreview" / "pg_backup.py").read_text(encoding="utf-8")
        self.assertRegex(source, r"con\.contype\s*<>\s*'n'")
        self.assertIn("attnotnull", source)
        self.assertIn("has_table_privilege", source)
        self.assertIn("has_function_privilege", source)

    def test_retention_keeps_recent_month_end_and_migration_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _bundle(root, "2024-01-01T00:00:00Z")
            kept_month = _bundle_path(root, "2024-01-15T00:00:00Z")
            _bundle(root, "2026-03-01T00:00:00Z")
            kept_recent = _bundle_path(root, "2026-03-02T00:00:00Z")
            migration = _bundle_path(root, "2020-05-01T00:00:00Z", "migration-snapshot")
            removed = prune_backups(root, keep_recent=2)
            self.assertEqual(len(removed), 1)
            self.assertTrue(kept_month.exists())
            self.assertTrue(kept_recent.exists())
            self.assertTrue(migration.exists())
            self.assertTrue((root / "20260301T000000Z").exists())


def _bundle_path(root: Path, created_at: str, retention: str = "daily") -> Path:
    _bundle(root, created_at, retention)
    return root / created_at.replace(":", "").replace("-", "")


class TestLocalDumpRestoreDrill(unittest.TestCase):
    tmp: tempfile.TemporaryDirectory[str] | None = None
    bin_dir: Path
    conn: PgConn
    data_dir: Path
    tools_ready = False

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.bin_dir = resolve_pg_bin()
        except PgBackupError as exc:
            raise unittest.SkipTest(str(exc)) from exc
        if not (cls.bin_dir / "initdb").is_file() or not (cls.bin_dir / "pg_ctl").is_file():
            raise unittest.SkipTest("需要 PostgreSQL 服务端来做空白库恢复，不使用 Docker。")
        cls.tools_ready = True
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.data_dir = root / "data"
        port = _free_port()
        init = subprocess.run(
            [
                str(cls.bin_dir / "initdb"),
                "-D",
                str(cls.data_dir),
                "-U",
                "postgres",
                "--auth=trust",
                "--encoding=UTF8",
                "--locale=C",
                "--no-instructions",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if init.returncode != 0:
            raise AssertionError(init.stderr[-2000:])
        start = subprocess.run(
            [
                str(cls.bin_dir / "pg_ctl"),
                "-D",
                str(cls.data_dir),
                "-l",
                str(root / "server.log"),
                "-w",
                "-o",
                f"-p {port} -c listen_addresses=127.0.0.1",
                "start",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if start.returncode != 0:
            log = (root / "server.log").read_text(encoding="utf-8", errors="replace")[-2000:]
            raise AssertionError(f"{start.stderr}\n{log}")
        cls.conn = PgConn(host="127.0.0.1", port=str(port), user="postgres", database="mr_src")
        psql_script(cls.conn, "CREATE DATABASE mr_src;", database="postgres", bin_dir=cls.bin_dir)
        migration = "\n".join(path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS.glob("*.sql")))
        psql_script(cls.conn, migration, database="mr_src", bin_dir=cls.bin_dir)
        cls.created, cls.deleted = _seed(cls.conn, cls.bin_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.tools_ready and cls.tmp is not None:
            subprocess.run(
                [str(cls.bin_dir / "pg_ctl"), "-D", str(cls.data_dir), "-m", "fast", "-w", "stop"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            cls.tmp.cleanup()

    def test_dump_restores_onto_blank_database(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            dest = Path(raw)
            backup = create_backup(self.conn, dest, migrations_dir=MIGRATIONS, bin_dir=self.bin_dir)
            self.assertFalse(str(backup).startswith(str(Path.home() / ".marketreview")))
            dump = (backup / "marketreview.sql").read_text(encoding="utf-8")
            self.assertIn(MARKER, dump)
            self.assertNotIn("CREATE ROLE", dump)
            self.assertNotRegex(dump, r"(?i)password\s+'")
            roles = (backup / "roles.sql").read_text(encoding="utf-8")
            self.assertIn("NOLOGIN", roles)
            self.assertRegex(roles, r"CREATE ROLE postgres NOLOGIN")
            self.assertNotRegex(roles, r"(?i)password\s+'")
            manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["revision"], 2)
            self.assertEqual(manifest["row_counts"]["sync_commit_result"], 2)
            self.assertIn("0003_marketreview_v1_sync.sql", manifest["migrations"])
            self.assertEqual(manifest["contract"], "contracts/supabase_rpc_v1.json")
            self.assertTrue((backup / "contracts" / "supabase_rpc_v1.json").is_file())
            self.assertIn("contracts/supabase_rpc_v1.json", manifest["files"])
            checksums = (backup / "CHECKSUMS").read_text(encoding="utf-8")
            self.assertIn("contracts/supabase_rpc_v1.json", checksums)
            self.assertNotIn("--table", manifest["pg_dump_flags"])

            restore_into_blank(self.conn, backup, "mr_restore", bin_dir=self.bin_dir)
            with self.assertRaises(PgBackupError):
                restore_into_blank(self.conn, backup, "mr_restore", bin_dir=self.bin_dir)
            summary = verify_restore(self.conn, backup, "mr_restore", bin_dir=self.bin_dir)
            self.assertEqual(summary["revision"], 2)

            restored_dump = dest / "restored.sql"
            dump_marketreview_sql(self.conn, restored_dump, database="mr_restore", bin_dir=self.bin_dir)
            self.assertEqual(copy_records(dump), copy_records(restored_dump.read_text(encoding="utf-8")))

            snapshot = rpc(
                self.conn,
                "marketreview_sync_snapshot",
                {"schema_version": 1},
                database="mr_restore",
                bin_dir=self.bin_dir,
            )
            self.assertEqual([row["trade_date"] for row in snapshot["reviews"]], ["2026-08-21"])
            review = snapshot["reviews"][0]
            self.assertEqual(review["advancing_count"], 0)
            self.assertIsNone(review["pullback_count"])
            self.assertEqual(review["median_change_pct"], 1.5)
            self.assertEqual(review["created_at"], BATCH)
            self.assertEqual([row["value"] for row in snapshot["sectors"]], ["白酒", "消费"])
            self.assertEqual([row["position"] for row in snapshot["sectors"]], [0, 1])
            self.assertEqual([row["value"] for row in snapshot["reasons"]], ["业绩", "高分红"])
            deleted = [row for row in snapshot["history"] if row["change_kind"] == "delete"]
            self.assertEqual(deleted[0]["group_key"]["trade_date"], "2026-08-20")
            self.assertFalse(deleted[0]["after_exists"])
            stored = next(row for row in snapshot["sync_results"] if row["operation_id"] == "op-create")
            self.assertEqual(stored["result_payload"], self.created)
            self.assertIsInstance(stored["result_payload"], dict)
            self.assertIn(MARKER, json.dumps(stored["result_payload"], ensure_ascii=False))
            again = rpc(
                self.conn,
                "marketreview_sync_result",
                {"schema_version": 1, "operation_id": "op-delete"},
                database="mr_restore",
                bin_dir=self.bin_dir,
            )
            self.assertEqual(again, self.deleted)

            self._exercise_write_and_rollback()
            source_revision = psql(
                self.conn,
                "SELECT revision FROM marketreview.ledger WHERE ledger_key = 'main'",
                database="mr_src",
                bin_dir=self.bin_dir,
            ).strip()
            self.assertEqual(source_revision, "2")

    def test_catalog_omits_not_null_names_even_when_server_records_them(self) -> None:
        # 临时集群若为 PG18，pg_constraint 会有 *_not_null；备份 catalog 须过滤掉，
        # 同时用 attnotnull 保留列非空属性。
        raw = psql(
            self.conn,
            """
            SELECT count(*) FROM pg_constraint AS con
            JOIN pg_namespace AS nsp ON nsp.oid = con.connamespace
            WHERE nsp.nspname = 'marketreview' AND con.contype = 'n'
            """,
            database="mr_src",
            bin_dir=self.bin_dir,
        ).strip()
        catalog = _catalog(self.conn, database="mr_src", bin_dir=self.bin_dir)
        self.assertTrue(all("not_null" not in name for name in catalog["constraints"]))
        self.assertIn("daily_market_review.updated_at=not_null", catalog["columns"])
        self.assertIn("daily_price_limit_event.trade_date=not_null", catalog["columns"])
        if int(raw) > 0:
            self.assertGreater(int(raw), 0)

    def test_verify_rejects_extra_anon_select_on_event_table(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backup = create_backup(self.conn, Path(raw), migrations_dir=MIGRATIONS, bin_dir=self.bin_dir)
            restore_into_blank(self.conn, backup, "mr_acl_drift", bin_dir=self.bin_dir)
            verify_restore(self.conn, backup, "mr_acl_drift", bin_dir=self.bin_dir)
            psql_script(
                self.conn,
                """
                GRANT USAGE ON SCHEMA marketreview TO anon;
                GRANT SELECT ON marketreview.daily_price_limit_event TO anon;
                """,
                database="mr_acl_drift",
                bin_dir=self.bin_dir,
            )
            with self.assertRaisesRegex(PgBackupError, r"anon 仍有 marketreview\.daily_price_limit_event 的 SELECT"):
                verify_restore(self.conn, backup, "mr_acl_drift", bin_dir=self.bin_dir)

    def test_verify_rejects_dropped_column_not_null(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backup = create_backup(self.conn, Path(raw), migrations_dir=MIGRATIONS, bin_dir=self.bin_dir)
            restore_into_blank(self.conn, backup, "mr_null_drift", bin_dir=self.bin_dir)
            verify_restore(self.conn, backup, "mr_null_drift", bin_dir=self.bin_dir)
            psql_script(
                self.conn,
                "ALTER TABLE marketreview.daily_market_review ALTER COLUMN updated_at DROP NOT NULL;",
                database="mr_null_drift",
                bin_dir=self.bin_dir,
            )
            with self.assertRaisesRegex(PgBackupError, r"列非空属性"):
                verify_restore(self.conn, backup, "mr_null_drift", bin_dir=self.bin_dir)

    def test_failed_backup_does_not_replace_a_valid_one(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            dest = Path(raw)
            moment = datetime(2026, 9, 30, 1, 2, 3, tzinfo=timezone.utc)
            backup = create_backup(
                self.conn,
                dest,
                migrations_dir=MIGRATIONS,
                bin_dir=self.bin_dir,
                created_at=moment,
            )
            digest = (backup / "CHECKSUMS").read_text(encoding="utf-8")
            broken = PgConn(host="127.0.0.1", port="1", user="postgres", database="mr_src")
            with self.assertRaises(PgBackupError):
                create_backup(broken, dest, migrations_dir=MIGRATIONS, bin_dir=self.bin_dir)
            self.assertEqual((backup / "CHECKSUMS").read_text(encoding="utf-8"), digest)
            self.assertFalse(list(dest.glob(".partial-*")))
            with self.assertRaises(PgBackupError):
                create_backup(
                    self.conn,
                    dest,
                    migrations_dir=MIGRATIONS,
                    bin_dir=self.bin_dir,
                    created_at=moment,
                )
            self.assertEqual((backup / "CHECKSUMS").read_text(encoding="utf-8"), digest)

    def test_cli_backup_prints_path_without_a_password(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "PGHOST": self.conn.host,
                "PGPORT": self.conn.port,
                "PGUSER": self.conn.user,
                "PGDATABASE": "postgres",
                "PYTHONPATH": str(ROOT / "scripts"),
                "MARKETREVIEW_PG_BIN": str(self.bin_dir),
            }
            completed = subprocess.run(
                [
                    "python3",
                    str(ROOT / "scripts" / "pg_backup.py"),
                    "backup",
                    "--database",
                    "mr_src",
                    "--dest",
                    raw,
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn("PGPASSWORD", completed.stdout)
            self.assertNotIn("postgres://", completed.stdout)
            self.assertTrue((Path(completed.stdout.strip()) / "BACKUP_OK").is_file())

    def _exercise_write_and_rollback(self) -> None:
        before = rpc(
            self.conn,
            "marketreview_probe",
            {"schema_version": 1},
            database="mr_restore",
            bin_dir=self.bin_dir,
        )["revision"]
        saved = rpc(
            self.conn,
            "marketreview_save_review",
            {
                "schema_version": 1,
                "trade_date": "2026-08-21",
                "batch_time": BATCH_2,
                "fields": {"declining_count": 4},
            },
            database="mr_restore",
            bin_dir=self.bin_dir,
        )
        self.assertEqual(saved["revision"], before + 1)
        invalid_detail = rpc_error(
            self.conn,
            "marketreview_save_event_details",
            {
                "schema_version": 1,
                "trade_date": "2026-08-21",
                "batch_time": BATCH_2,
                "details": [
                    {"market": "sh", "code": "600519", "direction": "up", "is_leader": "false"}
                ],
            },
            database="mr_restore",
            bin_dir=self.bin_dir,
        )
        self.assertIn("INVALID_TYPE", invalid_detail)
        bad_commit = rpc_error(
            self.conn,
            "marketreview_sync_commit",
            {
                "schema_version": 1,
                "operation_id": "op-bad",
                "project_id": "project-1",
                "ledger_id": "ledger-1",
                "request_digest": "digest-bad",
                "expected_revision": before + 1,
                "groups": [_review("2026-08-24"), _bad_event()],
            },
            database="mr_restore",
            bin_dir=self.bin_dir,
        )
        self.assertIn("INVALID_REQUEST", bad_commit)
        snapshot = rpc(
            self.conn,
            "marketreview_sync_snapshot",
            {"schema_version": 1},
            database="mr_restore",
            bin_dir=self.bin_dir,
        )
        self.assertEqual(snapshot["revision"], before + 1)
        self.assertEqual([row["trade_date"] for row in snapshot["reviews"]], ["2026-08-21"])
        self.assertEqual(snapshot["reviews"][0]["created_at"], BATCH)
        self.assertEqual(snapshot["reviews"][0]["updated_at"], BATCH_2)
        self.assertEqual(snapshot["reviews"][0]["declining_count"], 4)
        self.assertEqual(snapshot["reviews"][0]["advancing_count"], 0)
        self.assertEqual(snapshot["details"][0]["note"], MARKER)


class TestPostgresStubOnNonPostgresCluster(unittest.TestCase):
    """复现本机无 postgres 角色时，云端 DEFAULT PRIVILEGES 会阻断恢复。"""

    tmp: tempfile.TemporaryDirectory[str] | None = None
    bin_dir: Path
    conn: PgConn
    data_dir: Path
    tools_ready = False

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.bin_dir = resolve_pg_bin()
        except PgBackupError as exc:
            raise unittest.SkipTest(str(exc)) from exc
        if not (cls.bin_dir / "initdb").is_file() or not (cls.bin_dir / "pg_ctl").is_file():
            raise unittest.SkipTest("需要 PostgreSQL 服务端来验证 postgres 角色 stub。")
        cls.tools_ready = True
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.data_dir = root / "data"
        port = _free_port()
        # 故意不用 postgres 作超级用户，对齐 Homebrew 本机集群场景。
        init = subprocess.run(
            [
                str(cls.bin_dir / "initdb"),
                "-D",
                str(cls.data_dir),
                "-U",
                "mr_admin",
                "--auth=trust",
                "--encoding=UTF8",
                "--locale=C",
                "--no-instructions",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if init.returncode != 0:
            raise AssertionError(init.stderr[-2000:])
        start = subprocess.run(
            [
                str(cls.bin_dir / "pg_ctl"),
                "-D",
                str(cls.data_dir),
                "-l",
                str(root / "server.log"),
                "-w",
                "-o",
                f"-p {port} -c listen_addresses=127.0.0.1",
                "start",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if start.returncode != 0:
            log = (root / "server.log").read_text(encoding="utf-8", errors="replace")[-2000:]
            raise AssertionError(f"{start.stderr}\n{log}")
        cls.conn = PgConn(host="127.0.0.1", port=str(port), user="mr_admin", database="postgres")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.tools_ready and cls.tmp is not None:
            subprocess.run(
                [str(cls.bin_dir / "pg_ctl"), "-D", str(cls.data_dir), "-m", "fast", "-w", "stop"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            cls.tmp.cleanup()

    def test_roles_sql_unblocks_default_privileges_for_role_postgres(self) -> None:
        before = psql(
            self.conn,
            "SELECT count(*) FROM pg_roles WHERE rolname = 'postgres'",
            database="postgres",
            bin_dir=self.bin_dir,
        ).strip()
        self.assertEqual(before, "0")

        # 只引用 FOR ROLE postgres，避免先因缺少 service_role 而掩盖本缺陷。
        cloud_default_acl = """
        CREATE SCHEMA IF NOT EXISTS marketreview;
        ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA marketreview
          GRANT ALL ON FUNCTIONS TO PUBLIC;
        """
        with self.assertRaises(PgBackupError) as raised:
            psql_script(self.conn, cloud_default_acl, database="postgres", bin_dir=self.bin_dir)
        self.assertIn('role "postgres" does not exist', raised.exception.message)

        psql_script(self.conn, ROLES_SQL, database="postgres", bin_dir=self.bin_dir)
        after = psql(
            self.conn,
            "SELECT count(*) FROM pg_roles WHERE rolname = 'postgres'",
            database="postgres",
            bin_dir=self.bin_dir,
        ).strip()
        self.assertEqual(after, "1")
        login = psql(
            self.conn,
            "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'postgres'",
            database="postgres",
            bin_dir=self.bin_dir,
        ).strip()
        self.assertEqual(login, "f")
        psql_script(self.conn, cloud_default_acl, database="postgres", bin_dir=self.bin_dir)


def _seed(conn: PgConn, bin_dir: Path) -> tuple[dict, dict]:
    created = rpc(
        conn,
        "marketreview_sync_commit",
        _commit_payload(
            [_review("2026-08-20", advancing_count=7), _review("2026-08-21"), _event_group()],
            operation_id="op-create",
            digest="digest-create",
            expected=0,
        ),
        database=conn.database,
        bin_dir=bin_dir,
    )
    deleted = rpc(
        conn,
        "marketreview_sync_commit",
        _commit_payload(
            [{"group_kind": "review", "group_key": {"trade_date": "2026-08-20"}, "exists": False}],
            operation_id="op-delete",
            digest="digest-delete",
            expected=1,
        ),
        database=conn.database,
        bin_dir=bin_dir,
    )
    return created, deleted


def _review(day: str, **overrides: object) -> dict:
    body = {name: None for name in ATOMIC_FIELD_NAMES}
    body.update(
        {
            "trade_date": day,
            "created_at": BATCH,
            "updated_at": BATCH,
            "advancing_count": 0,
            "median_change_pct": 1.5,
        }
    )
    body.update(overrides)
    return {
        "group_kind": "review",
        "group_key": {"trade_date": day},
        "exists": True,
        "review": body,
    }


def _event_group() -> dict:
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
                    "is_leader": False,
                    "note": MARKER,
                    "created_at": BATCH,
                    "updated_at": BATCH,
                },
                "sectors": ["白酒", "消费"],
                "limit_up_reasons": ["业绩", "高分红"],
            }
        ],
    }


def _bad_event() -> dict:
    group = _event_group()
    group["group_key"] = {"trade_date": "2026-08-24", "market": "sh", "code": "600519"}
    group["events"][0]["limit_rate_bp"] = 1500
    return group


def _commit_payload(groups: list, operation_id: str, digest: str, expected: int) -> dict:
    return {
        "schema_version": 1,
        "operation_id": operation_id,
        "project_id": "project-1",
        "ledger_id": "ledger-1",
        "request_digest": digest,
        "expected_revision": expected,
        "groups": groups,
    }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    unittest.main()
