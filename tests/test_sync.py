"""Sync push/pull tests. They use temporary SQLite files and a fake cloud."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import _bootstrap  # noqa: F401

import cli

from marketreview.errors import MarketReviewError, RemoteStoreError
from marketreview.repository import MarketReviewRepository
from marketreview.schema import ATOMIC_FIELD_NAMES, PriceLimitEventInput
from marketreview.sqlite_schema import connect
from marketreview.sync_engine import GroupChoice, protect_snapshot, pull_sync, push_sync
from marketreview.sync_groups import parse_group_ref, read_local_groups, same_evidence
from marketreview.sync_ledger import ensure_sync_schema, get_operation, has_coverage, read_baselines
from marketreview.write_gate import read_pending


PROJECT = "project1"
DAY = "2026-08-21"
T1 = "2026-08-21T07:00:00+00:00"
T2 = "2026-08-21T08:00:00+00:00"


class FakeCloud:
    def __init__(self) -> None:
        self.revision = 0
        self.groups: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self.results: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.advance_before_commit = 0
        self.drop_next_response = False
        self.fail_before_apply = False
        self.lie = False
        self.reject_limit = 1500
        # Mimic PostgreSQL jsonb: whole double-precision values become JSON ints.
        self.integerize_whole_floats = False

    def call(self, function: str, request: dict[str, Any], *, write: bool) -> dict[str, Any]:
        self.calls.append(function)
        if function == "marketreview_sync_snapshot":
            return self.snapshot()
        if function == "marketreview_sync_commit":
            return self.commit(request)
        if function == "marketreview_sync_result":
            return self.result(request)
        raise AssertionError(function)

    def snapshot(self) -> dict[str, Any]:
        reviews, events, details, sectors, reasons = _flatten(self.groups)
        results = list(self.results.values())
        payload = {
            "format_version": 1,
            "schema_version": 1,
            "complete": True,
            "revision": self.revision,
            "ledger_key": "main",
            "reviews": reviews,
            "events": events,
            "details": details,
            "sectors": sectors,
            "reasons": reasons,
            "history": list(self.history),
            "sync_results": results,
            "counts": {
                "reviews": len(reviews),
                "events": len(events),
                "details": len(details),
                "sectors": len(sectors),
                "reasons": len(reasons),
                "history": len(self.history),
                "sync_results": len(results),
            },
        }
        return payload

    def commit(self, request: dict[str, Any]) -> dict[str, Any]:
        operation_id = request["operation_id"]
        digest = request["request_digest"]
        stored = self.results.get(operation_id)
        if stored is not None:
            if stored["request_digest"] != digest:
                raise RemoteStoreError("摘要不一致", code="OPERATION_DIGEST_MISMATCH")
            return stored
        groups = request["groups"]
        if not isinstance(groups, list) or not groups:
            raise RemoteStoreError("空提交", code="EMPTY_COMMIT")
        if self.fail_before_apply:
            self.fail_before_apply = False
            raise RemoteStoreError("超时", code="REMOTE_RESULT_UNKNOWN")
        if self.advance_before_commit:
            self.revision += self.advance_before_commit
            self.advance_before_commit = 0
        if request["expected_revision"] != self.revision:
            raise RemoteStoreError("版本已变化", code="REVISION_CONFLICT")
        for group in groups:
            for event in group.get("events") or []:
                if event.get("limit_rate_bp") == self.reject_limit:
                    raise RemoteStoreError("非法涨跌幅", code="INVALID_REQUEST")
        applied = []
        histories = []
        for group in groups:
            ref = _ref(group)
            before = ref in self.groups and self.groups[ref].get("exists") is True
            if group.get("exists") is True:
                self.groups[ref] = json.loads(json.dumps(group))
                applied.append(json.loads(json.dumps(group)))
                histories.append(_history(group, before, True, "sync"))
            else:
                self.groups.pop(ref, None)
                applied.append(
                    {
                        "group_kind": group["group_kind"],
                        "group_key": group["group_key"],
                        "exists": False,
                    }
                )
                histories.append(_history(group, before, False, "delete"))
        self.revision += 1
        for row in histories:
            row["revision"] = self.revision
            row["operation_id"] = operation_id
            self.history.append(row)
        if self.lie and applied:
            applied = json.loads(json.dumps(applied))
            if applied[0].get("exists") and "review" in applied[0]:
                applied[0]["review"]["pe_sh"] = 999.0
        if self.integerize_whole_floats:
            applied = _integerize_whole_floats(applied)
            for ref, group in list(self.groups.items()):
                self.groups[ref] = _integerize_whole_floats([group])[0]
        payload = {
            "format_version": 1,
            "schema_version": 1,
            "operation_id": operation_id,
            "project_id": request["project_id"],
            "ledger_id": request["ledger_id"],
            "request_digest": digest,
            "expected_revision": request["expected_revision"],
            "committed_revision": self.revision,
            "groups": applied,
        }
        self.results[operation_id] = payload
        if self.drop_next_response:
            self.drop_next_response = False
            raise RemoteStoreError("响应丢失", code="REMOTE_RESULT_UNKNOWN")
        return payload

    def result(self, request: dict[str, Any]) -> dict[str, Any]:
        found = self.results.get(request["operation_id"])
        if found is None:
            raise RemoteStoreError("没有结果", code="OPERATION_NOT_FOUND")
        return found


def _ref(group: dict[str, Any]) -> str:
    key = group["group_key"]
    if group["group_kind"] == "review":
        return f"review:{key['trade_date']}"
    return f"event:{key['trade_date']}:{key['market']}:{key['code']}"


def _integerize_whole_floats(value: Any) -> Any:
    """Recreate PostgreSQL jsonb number typing for whole floats."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_integerize_whole_floats(item) for item in value]
    if isinstance(value, dict):
        return {key: _integerize_whole_floats(item) for key, item in value.items()}
    return value


def _history(group: dict[str, Any], before: bool, after: bool, kind: str) -> dict[str, Any]:
    return {
        "group_kind": group["group_kind"],
        "group_key": group["group_key"],
        "change_kind": kind,
        "before_exists": before,
        "after_exists": after,
        "operation_id": None,
        "revision": 0,
    }


def _flatten(groups: dict[str, dict[str, Any]]) -> tuple[list, list, list, list, list]:
    reviews, events, details, sectors, reasons = [], [], [], [], []
    for group in groups.values():
        if group.get("exists") is not True:
            continue
        if group["group_kind"] == "review":
            reviews.append(group["review"])
            continue
        key = group["group_key"]
        for item in group["events"]:
            events.append(
                {
                    "trade_date": key["trade_date"],
                    "market": key["market"],
                    "code": key["code"],
                    "name": item["name"],
                    "direction": item["direction"],
                    "closed_at_limit": item["closed_at_limit"],
                    "limit_rate_bp": item["limit_rate_bp"],
                    "streak_height": item["streak_height"],
                    "created_at": item["created_at"],
                    "updated_at": item["updated_at"],
                }
            )
            if item["detail_exists"]:
                details.append(
                    {
                        "trade_date": key["trade_date"],
                        "market": key["market"],
                        "code": key["code"],
                        "direction": item["direction"],
                        **item["detail"],
                    }
                )
            for position, value in enumerate(item["sectors"]):
                sectors.append({**_identity(key, item["direction"]), "position": position, "value": value})
            for position, value in enumerate(item["limit_up_reasons"]):
                reasons.append({**_identity(key, item["direction"]), "position": position, "value": value})
    return reviews, events, details, sectors, reasons


def _identity(key: dict[str, str], direction: str) -> dict[str, str]:
    return {
        "trade_date": key["trade_date"],
        "market": key["market"],
        "code": key["code"],
        "direction": direction,
    }


class SyncCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.db = self.root / "market.sqlite3"
        self.cloud = FakeCloud()

    def push(self, db: Path | None = None, **kwargs: Any) -> dict[str, Any]:
        return push_sync(
            sqlite_path=db or self.db,
            transport=self.cloud,
            project_id=PROJECT,
            state_dir=self.state,
            busy_timeout_ms=200,
            **kwargs,
        )

    def pull(self, db: Path | None = None, **kwargs: Any) -> dict[str, Any]:
        return pull_sync(
            sqlite_path=db or self.db,
            transport=self.cloud,
            project_id=PROJECT,
            state_dir=self.state,
            busy_timeout_ms=200,
            **kwargs,
        )

    def seed_review(self, db: Path | None = None, *, day: str = DAY, pe: float = 1.0, stamp: str = T1) -> None:
        path = db or self.db
        with MarketReviewRepository(path) as repo:
            repo.save_review(day, {"pe_sh": pe})
        _stamp_review(path, day, stamp)

    def seed_event(self, db: Path | None = None, *, day: str = DAY, code: str = "600519") -> None:
        path = db or self.db
        with MarketReviewRepository(path) as repo:
            repo.save_price_limit_events(
                day,
                [
                    PriceLimitEventInput(
                        market="sh",
                        code=code,
                        name="贵州茅台",
                        direction="up",
                        closed_at_limit=True,
                        limit_rate_bp=1000,
                        streak_height=2,
                    )
                ],
            )

    def copy_local_to_cloud(self, db: Path | None = None) -> None:
        conn = connect(db or self.db)
        try:
            ensure_sync_schema(conn)
            from marketreview.sqlite_schema import init_db

            init_db(conn)
            self.cloud.groups = read_local_groups(conn)
        finally:
            conn.close()

    def align(self, db: Path | None = None) -> dict[str, Any]:
        self.copy_local_to_cloud(db)
        report = self.push(db)
        self.assertEqual(report["status"], "completed")
        self.assertNotIn("marketreview_sync_commit", self.cloud.calls)
        return report

    def baselines(self, report: dict[str, Any], db: Path | None = None) -> dict[str, Any]:
        conn = connect(db or self.db)
        try:
            return read_baselines(conn, project_id=PROJECT, ledger_id=report["ledger_id"])
        finally:
            conn.close()


def _stamp_review(path: Path, day: str, stamp: str) -> None:
    conn = connect(path)
    try:
        conn.execute(
            "UPDATE daily_market_review SET created_at = ?, updated_at = ? WHERE trade_date = ?",
            (stamp, stamp, day),
        )
        conn.commit()
    finally:
        conn.close()


def _choice(action: str, ref: str) -> GroupChoice:
    kind, key = ref.split(":", 1)[0], None
    from marketreview.sync_groups import parse_group_ref

    kind, key = parse_group_ref(ref)
    return GroupChoice(kind, key, action)


class TestSyncPush(SyncCase):
    def test_same_business_different_audit_time_is_not_a_conflict(self) -> None:
        self.seed_review()
        self.copy_local_to_cloud()
        self.cloud.groups["review:" + DAY]["review"]["updated_at"] = T2
        report = self.push()
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["conflicts"], [])
        self.assertFalse(report["same"][0]["audit_time_matches"])
        self.assertTrue(report["same"][0]["business_matches"])
        self.assertNotIn("marketreview_sync_commit", self.cloud.calls)
        conn = connect(self.db)
        try:
            local = conn.execute("SELECT updated_at FROM daily_market_review").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(local, T1)
        baseline = self.baselines(report)["review:" + DAY]
        self.assertEqual(baseline["group"]["review"]["updated_at"], T2)

    def test_cloud_only_without_baseline_does_not_block_other_uploads(self) -> None:
        self.seed_review(pe=1.0)
        self.cloud.groups = {
            "review:2026-08-22": _review_group("2026-08-22", 2.0, T1),
        }
        report = self.push()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["download"][0]["reason"], "云端更新待下载")
        self.assertIn("review:" + DAY, report["committed"])
        self.assertNotIn("review:2026-08-22", self.baselines(report))
        self.assertIn("review:" + DAY, self.cloud.groups)

    def test_local_delete_is_not_auto_uploaded(self) -> None:
        self.seed_review()
        report = self.align()
        conn = connect(self.db)
        try:
            conn.execute("DELETE FROM daily_market_review")
            conn.commit()
        finally:
            conn.close()
        again = self.push()
        self.assertEqual(again["status"], "needs_resolution")
        self.assertEqual(again["local_deletes"][0]["options"][0]["label"], "把云端恢复到本地")
        self.assertEqual(again["local_deletes"][0]["options"][1]["label"], "在云端删除")
        self.assertEqual(again["download"], [])
        self.assertEqual(self.cloud.revision, 0)
        self.assertTrue(self.baselines(report)["review:" + DAY]["baseline_state"] == "present")

    def test_conflict_adopt_local_when_local_is_absent_deletes_cloud_group(self) -> None:
        self.seed_review(pe=1.0)
        self.align()
        conn = connect(self.db)
        try:
            conn.execute("DELETE FROM daily_market_review")
            conn.commit()
        finally:
            conn.close()
        self.cloud.groups["review:" + DAY]["review"]["pe_sh"] = 3.0
        preview = self.push()
        self.assertEqual(preview["conflicts"][0]["options"][1]["deletes_cloud_group"], True)
        chosen = self.push(choices=[_choice("adopt_local", "review:" + DAY)])
        self.assertEqual(chosen["status"], "completed")
        self.assertNotIn("review:" + DAY, self.cloud.groups)
        self.assertEqual(self.baselines(chosen)["review:" + DAY]["baseline_state"], "confirmed_absent")
        self.assertTrue(any(row["change_kind"] == "delete" for row in self.cloud.history))

    def test_revision_conflict_rolls_back_and_unknown_result_can_resume(self) -> None:
        self.seed_review()
        self.cloud.advance_before_commit = 1
        failed = self.push()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.cloud.groups, {})
        self.assertEqual(read_pending(self.state)["status"], "closed")
        self.seed_event()
        self.cloud.drop_next_response = True
        unknown = self.push()
        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(read_pending(self.state)["status"], "open")
        self.assertEqual(self.cloud.revision, 2)
        recovered = self.push()
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(self.cloud.revision, 2)
        self.assertEqual(read_pending(self.state)["status"], "closed")

    def test_restore_cloud_then_pull_after_other_blocker_clears(self) -> None:
        self.seed_review()
        self.seed_event()
        self.align()
        conn = connect(self.db)
        try:
            conn.execute("DELETE FROM daily_market_review")
            conn.commit()
        finally:
            conn.close()
        self.push(choices=[_choice("restore_cloud", "review:" + DAY)])
        with MarketReviewRepository(self.db) as repo:
            repo.save_review("2026-08-22", {"pe_sh": 4.0})
        blocked = self.pull()
        self.assertEqual(blocked["wrote"], False)
        self.assertIn("review:2026-08-22", blocked["blockers"])
        self.assertNotIn("review:" + DAY, blocked["blockers"])
        self.push()
        restored = self.pull()
        self.assertEqual(restored["status"], "completed")
        with MarketReviewRepository(self.db) as repo:
            self.assertIsNotNone(repo.get_review(DAY))
        conn = connect(self.db)
        try:
            ensure_sync_schema(conn)
            from marketreview.sync_ledger import read_authorizations

            ledger = restored["ledger_id"]
            auth = read_authorizations(conn, project_id=PROJECT, ledger_id=ledger)
        finally:
            conn.close()
        self.assertEqual(auth["review:" + DAY]["state"], "consumed")

    def test_delete_on_cloud_commits_absence_and_conflict_rolls_back_the_batch(self) -> None:
        self.seed_review()
        self.seed_event()
        self.align()
        conn = connect(self.db)
        try:
            conn.execute("DELETE FROM daily_market_review")
            conn.commit()
        finally:
            conn.close()
        deleted = self.push(choices=[_choice("delete_on_cloud", "review:" + DAY)])
        self.assertEqual(deleted["status"], "completed")
        self.assertNotIn("review:" + DAY, self.cloud.groups)
        self.assertIn("event:2026-08-21:sh:600519", self.cloud.groups)
        self.assertEqual(self.baselines(deleted)["review:" + DAY]["baseline_state"], "confirmed_absent")
        bad = self.root / "bad.sqlite3"
        self.seed_review(bad, day="2026-08-23", pe=1.0)
        self.seed_event(bad, day="2026-08-23", code="600000")
        conn = connect(bad)
        try:
            conn.execute(
                "UPDATE daily_price_limit_event SET limit_rate_bp = ? WHERE trade_date = ?",
                (self.cloud.reject_limit, "2026-08-23"),
            )
            conn.commit()
        finally:
            conn.close()
        before = self.cloud.revision
        failed = self.push(bad)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.cloud.revision, before)
        self.assertNotIn("review:2026-08-23", self.cloud.groups)

    def test_authorization_digest_float_audit_time_and_unrelated_revision(self) -> None:
        self.seed_review(pe=1.0)
        self.align()
        self.cloud.groups["review:" + DAY]["review"]["pe_sh"] = 2.0
        with MarketReviewRepository(self.db) as repo:
            repo.save_review(DAY, {"pe_sh": 3.0})
        _stamp_review(self.db, DAY, T1)
        self.cloud.groups["review:" + DAY]["review"]["updated_at"] = T1
        _stamp_review(self.db, DAY, T1)
        chosen = self.push(choices=[_choice("keep_cloud", "review:" + DAY)])
        self.assertEqual(chosen["authorized_download"][0]["reason"], "已授权保留云端，待下载")
        self.assertEqual(chosen["conflicts"], [])
        baseline_before = self.baselines(chosen)["review:" + DAY]["group"]["review"]["pe_sh"]
        self.assertEqual(baseline_before, 1.0)
        conn = connect(self.db)
        try:
            ensure_sync_schema(conn)
            conn.execute(
                "UPDATE sync_authorization SET local_digest = 'broken' WHERE group_kind = 'review'"
            )
            conn.commit()
        finally:
            conn.close()
        broken = self.push()
        self.assertEqual(broken["conflicts"][0]["group"], "review:" + DAY)
        self.push(choices=[_choice("keep_cloud", "review:" + DAY)])
        conn = connect(self.db)
        try:
            row = conn.execute("SELECT pe_sh FROM daily_market_review").fetchone()
            conn.execute("UPDATE daily_market_review SET pe_sh = ?", (row[0] + 1e-10,))
            conn.commit()
        finally:
            conn.close()
        tolerated = self.push()
        self.assertEqual(tolerated["authorized_download"][0]["group"], "review:" + DAY)
        self.assertEqual(tolerated["conflicts"], [])
        conn = connect(self.db)
        try:
            conn.execute(
                "UPDATE daily_market_review SET updated_at = ? WHERE trade_date = ?",
                (T2, DAY),
            )
            conn.commit()
        finally:
            conn.close()
        self.cloud.groups["review:" + DAY]["review"]["pe_sh"] = 3.0 + 1e-10
        timed = self.push()
        self.assertEqual(timed["conflicts"], [])
        self.assertTrue(timed["same"])
        self.cloud.groups["review:" + DAY]["review"]["updated_at"] = T2
        self.cloud.groups["review:" + DAY]["review"]["pe_sh"] = 9.0
        with MarketReviewRepository(self.db) as repo:
            repo.save_review(DAY, {"pe_sh": 8.0})
        _stamp_review(self.db, DAY, T2)
        self.cloud.groups["review:" + DAY]["review"]["updated_at"] = T2
        self.push(choices=[_choice("keep_cloud", "review:" + DAY)])
        self.seed_event()
        self.push()
        again = self.push()
        self.assertEqual(again["authorized_download"][0]["reason"], "已授权保留云端，待下载")
        self.assertEqual(again["conflicts"], [])
        self.assertNotIn("review:" + DAY, again["committed"])

    def test_pending_classes_do_not_share_a_completed_status(self) -> None:
        self.seed_review()
        self.align()
        self.cloud.groups["review:" + DAY]["review"]["pe_sh"] = 5.0
        only_download = self.push()
        self.assertEqual(only_download["status"], "needs_resolution")
        self.assertEqual(only_download["download"][0]["reason"], "云端更新待下载")
        self.seed_review(day="2026-08-22", pe=1.0)
        mixed = self.push()
        self.assertEqual(mixed["status"], "partial")
        self.assertTrue(mixed["committed"])
        self.assertTrue(mixed["download"])
        self.cloud.groups.pop("review:" + DAY)
        self.cloud.groups.pop("review:2026-08-22", None)
        self.cloud.history.append(
            {
                "group_kind": "review",
                "group_key": {"trade_date": DAY},
                "revision": 1,
                "change_kind": "delete",
                "before_exists": True,
                "after_exists": False,
                "operation_id": "old",
            }
        )
        conn = connect(self.db)
        try:
            conn.execute("DELETE FROM daily_market_review WHERE trade_date = ?", ("2026-08-22",))
            conn.execute("DELETE FROM sync_baseline")
            conn.execute("DELETE FROM sync_coverage")
            conn.commit()
        finally:
            conn.close()
        blocked = self.push()
        self.assertEqual(blocked["status"], "needs_resolution")
        self.assertTrue(blocked["conflicts"])
        self.assertIn("deletes_cloud_group", blocked["conflicts"][0]["options"][1])

    def test_recovery_uses_saved_source_and_stops_on_evidence_mismatch(self) -> None:
        self.seed_review(pe=1.0)
        self.cloud.drop_next_response = True
        unknown = self.push()
        self.assertEqual(unknown["status"], "unknown")
        with MarketReviewRepository(self.db) as repo:
            repo.save_review(DAY, {"pe_sh": 7.0})
        recovered = self.push()
        baseline = self.baselines(recovered)["review:" + DAY]
        self.assertEqual(baseline["group"]["review"]["pe_sh"], 1.0)
        with MarketReviewRepository(self.db) as repo:
            self.assertEqual(repo.get_review(DAY).pe_sh, 7.0)
        self.cloud.lie = True
        self.seed_review(day="2026-08-22", pe=1.0)
        lied = self.push()
        self.assertEqual(lied["status"], "unknown")
        self.assertEqual(read_pending(self.state)["status"], "open")
        again = self.push()
        self.assertEqual(again["status"], "unknown")
        self.assertNotIn("review:2026-08-22", self.baselines(recovered))

    def test_cloud_only_recovery_when_local_groups_evidence_is_gone(self) -> None:
        self.seed_review(pe=1.0)
        self.cloud.drop_next_response = True
        unknown = self.push()
        self.assertEqual(unknown["status"], "unknown")
        pending_path = self.state / "pending-write.json"
        record = json.loads(pending_path.read_text(encoding="utf-8"))
        self.assertIsInstance(record.get("groups"), list)
        record["groups"] = "damaged"
        pending_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        with MarketReviewRepository(self.db) as repo:
            repo.save_review(DAY, {"pe_sh": 9.0})
        recovered = self.push()
        self.assertEqual(recovered["status"], "completed")
        self.assertTrue(recovered.get("resumed"))
        baseline = self.baselines(recovered)["review:" + DAY]
        self.assertEqual(baseline["group"]["review"]["pe_sh"], 1.0)
        with MarketReviewRepository(self.db) as repo:
            self.assertEqual(repo.get_review(DAY).pe_sh, 9.0)
        self.assertEqual(read_pending(self.state)["status"], "closed")

    def test_two_ledgers_upload_disjoint_then_overlapping_data(self) -> None:
        other = self.root / "other.sqlite3"
        self.seed_review(pe=1.0)
        self.seed_review(other, day="2026-08-22", pe=2.0)
        first = self.push()
        second = self.push(other)
        self.assertIn("review:" + DAY, self.cloud.groups)
        self.assertIn("review:2026-08-22", self.cloud.groups)
        self.assertNotEqual(first["ledger_id"], second["ledger_id"])
        overlap = self.root / "overlap.sqlite3"
        self.seed_review(overlap, pe=9.0)
        conflicted = self.push(overlap)
        self.assertEqual(conflicted["conflicts"][0]["group"], "review:" + DAY)
        self.assertEqual(self.cloud.groups["review:" + DAY]["review"]["pe_sh"], 1.0)

    def test_duplicate_commit_after_lost_response_does_not_double_apply(self) -> None:
        self.seed_review()
        self.cloud.drop_next_response = True
        self.push()
        self.push()
        commits = [name for name in self.cloud.calls if name == "marketreview_sync_commit"]
        results = [name for name in self.cloud.calls if name == "marketreview_sync_result"]
        self.assertEqual(commits, ["marketreview_sync_commit"])
        self.assertEqual(results, ["marketreview_sync_result"])
        self.assertEqual(self.cloud.revision, 1)

    def test_pg_jsonb_whole_float_as_int_still_confirms_push(self) -> None:
        self.cloud.integerize_whole_floats = True
        self.seed_review(pe=11.0)
        report = self.push()
        self.assertEqual(report["status"], "completed")
        self.assertEqual(read_pending(self.state)["status"], "closed")
        baseline = self.baselines(report)["review:" + DAY]
        self.assertEqual(baseline["group"]["review"]["pe_sh"], 11.0)
        self.assertIs(type(baseline["group"]["review"]["pe_sh"]), float)
        conn = connect(self.db)
        try:
            local = read_local_groups(conn)["review:" + DAY]
        finally:
            conn.close()
        cloud = self.cloud.groups["review:" + DAY]
        self.assertIs(type(cloud["review"]["pe_sh"]), int)
        self.assertTrue(same_evidence([local], [cloud]))


class TestSyncPull(SyncCase):
    def test_cloud_ahead_downloads_and_local_only_blocks(self) -> None:
        self.seed_review(pe=1.0)
        self.align()
        self.cloud.groups["review:" + DAY]["review"]["pe_sh"] = 4.0
        downloaded = self.pull()
        self.assertEqual(downloaded["status"], "completed")
        with MarketReviewRepository(self.db) as repo:
            self.assertEqual(repo.get_review(DAY).pe_sh, 4.0)
        conn = connect(self.db)
        try:
            self.assertTrue(has_coverage(conn, project_id=PROJECT, ledger_id=downloaded["ledger_id"]))
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(str(mode).lower(), "wal")
        with MarketReviewRepository(self.db) as repo:
            repo.save_review("2026-08-22", {"pe_sh": 1.0})
        before_conn = connect(self.db)
        try:
            before = read_local_groups(before_conn)
        finally:
            before_conn.close()
        blocked = self.pull()
        self.assertEqual(blocked["wrote"], False)
        self.assertEqual(blocked["local_changes"][0]["group"], "review:2026-08-22")
        conn = connect(self.db)
        try:
            after = read_local_groups(conn)
        finally:
            conn.close()
        self.assertEqual(before["review:2026-08-22"]["review"]["pe_sh"], after["review:2026-08-22"]["review"]["pe_sh"])

    def test_keep_cloud_on_one_group_does_not_release_another(self) -> None:
        self.seed_review(pe=1.0)
        self.seed_event()
        self.align()
        self.cloud.groups["review:" + DAY]["review"]["pe_sh"] = 5.0
        with MarketReviewRepository(self.db) as repo:
            repo.save_review(DAY, {"pe_sh": 6.0})
            repo.save_price_limit_events(
                DAY,
                [
                    PriceLimitEventInput(
                        market="sh",
                        code="600519",
                        name="贵州茅台",
                        direction="up",
                        closed_at_limit=True,
                        limit_rate_bp=1000,
                        streak_height=3,
                    )
                ],
            )
        _stamp_review(self.db, DAY, T1)
        self.cloud.groups["review:" + DAY]["review"]["updated_at"] = T1
        blocked = self.pull(choices=[_choice("keep_cloud", "review:" + DAY)])
        self.assertIn("event:2026-08-21:sh:600519", blocked["blockers"])
        self.assertFalse(blocked["wrote"])
        self.push()
        done = self.pull()
        self.assertEqual(done["status"], "completed")
        with MarketReviewRepository(self.db) as repo:
            self.assertEqual(repo.get_review(DAY).pe_sh, 5.0)

    def test_backup_failure_and_transaction_interrupt_leave_target_unchanged(self) -> None:
        self.seed_review(pe=1.0)
        self.align()
        self.cloud.groups["review:" + DAY]["review"]["pe_sh"] = 4.0

        def broken_backup(*_args: Any, **_kwargs: Any) -> str:
            raise MarketReviewError(code="BACKUP_FAILED", message="磁盘满")

        with mock.patch("marketreview.sync_engine._backup_database", broken_backup):
            with self.assertRaises(MarketReviewError) as ctx:
                self.pull()
        self.assertEqual(ctx.exception.code, "BACKUP_FAILED")
        with MarketReviewRepository(self.db) as repo:
            self.assertEqual(repo.get_review(DAY).pe_sh, 1.0)

        def interrupt() -> None:
            raise RuntimeError("事务中断")

        with self.assertRaises(RuntimeError):
            self.pull(before_local_commit=interrupt)
        with MarketReviewRepository(self.db) as repo:
            self.assertEqual(repo.get_review(DAY).pe_sh, 1.0)
        self.assertEqual(read_pending(self.state)["status"], "open")
        resumed = self.pull()
        self.assertEqual(resumed["status"], "completed")
        with MarketReviewRepository(self.db) as repo:
            self.assertEqual(repo.get_review(DAY).pe_sh, 4.0)
        self.assertEqual(read_pending(self.state)["status"], "closed")

    def test_target_busy_and_protected_snapshot_are_rejected(self) -> None:
        self.seed_review()
        self.copy_local_to_cloud()
        holder = sqlite3.connect(self.db)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(MarketReviewError) as ctx:
                self.pull()
            self.assertEqual(ctx.exception.code, "TARGET_BUSY")
        finally:
            holder.rollback()
            holder.close()
        protect_snapshot(self.state, self.db)
        with self.assertRaises(MarketReviewError) as ctx:
            self.pull()
        self.assertEqual(ctx.exception.code, "TARGET_FORBIDDEN")

    def test_two_machines_match_after_pull(self) -> None:
        other = self.root / "other.sqlite3"
        self.seed_review(pe=1.0)
        self.seed_review(other, day="2026-08-22", pe=2.0)
        self.push()
        self.push(other)
        left = self.pull()
        right = self.pull(other)
        self.assertEqual(left["status"], "completed")
        self.assertEqual(right["status"], "completed")
        conn_left = connect(self.db)
        conn_right = connect(other)
        try:
            self.assertEqual(read_local_groups(conn_left), read_local_groups(conn_right))
        finally:
            conn_left.close()
            conn_right.close()


class TestSyncSafety(SyncCase):
    def test_corrupt_baseline_stops_and_does_not_first_join(self) -> None:
        self.seed_review()
        report = self.align()
        conn = connect(self.db)
        try:
            conn.execute(
                """
                UPDATE sync_baseline
                SET group_payload = '{not-json'
                WHERE project_id = ? AND ledger_id = ?
                """,
                (PROJECT, report["ledger_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(MarketReviewError) as ctx:
            self.push()
        self.assertEqual(ctx.exception.code, "BASELINE_CORRUPT")
        self.assertNotIn("marketreview_sync_commit", self.cloud.calls)

    def test_foreign_ledger_identity_in_sync_tables_stops(self) -> None:
        self.seed_review()
        report = self.align()
        conn = connect(self.db)
        try:
            conn.execute(
                """
                INSERT INTO sync_baseline (
                    project_id, ledger_id, group_kind, group_key, baseline_state,
                    group_payload, cloud_revision, confirmed_operation_id
                ) VALUES (?, ?, 'review', ?, 'present', ?, 1, NULL)
                """,
                (
                    PROJECT,
                    "ledger-foreign-copy",
                    json.dumps({"trade_date": "2026-08-22"}, ensure_ascii=False, sort_keys=True),
                    json.dumps({"exists": True}, ensure_ascii=False),
                ),
            )
            conn.commit()
            self.assertEqual(
                conn.execute(
                    "SELECT ledger_id FROM sync_ledger_singleton WHERE id = 1"
                ).fetchone()[0],
                report["ledger_id"],
            )
        finally:
            conn.close()
        with self.assertRaises(MarketReviewError) as ctx:
            self.push()
        self.assertEqual(ctx.exception.code, "IDENTITY_MISMATCH")
        self.assertIn("不会按首次接入重建", str(ctx.exception))
        self.assertNotIn("marketreview_sync_commit", self.cloud.calls)

    def test_delete_history_blocks_first_join_local_only_resurrection(self) -> None:
        self.seed_review()
        self.cloud.history = [
            {
                "group_kind": "review",
                "group_key": {"trade_date": DAY},
                "change_kind": "delete",
                "before_exists": True,
                "after_exists": False,
                "operation_id": "hist-1",
                "revision": 1,
            }
        ]
        report = self.push()
        self.assertEqual(report["status"], "needs_resolution")
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertEqual(report["conflicts"][0]["group"], "review:" + DAY)
        self.assertNotIn("marketreview_sync_commit", self.cloud.calls)

    def test_direction_replace_history_also_blocks_first_join_resurrection(self) -> None:
        self.seed_review()
        self.cloud.history = [
            {
                "group_kind": "review",
                "group_key": {"trade_date": DAY},
                "change_kind": "direction_replace",
                "before_exists": True,
                "after_exists": True,
                "operation_id": "hist-dir",
                "revision": 2,
            }
        ]
        report = self.push()
        self.assertEqual(report["status"], "needs_resolution")
        self.assertEqual(report["conflicts"][0]["group"], "review:" + DAY)
        self.assertNotIn("marketreview_sync_commit", self.cloud.calls)


class TestSyncGate(SyncCase):
    def test_open_pending_blocks_local_sqlite_writes(self) -> None:
        self.seed_review(pe=1.0)
        self.cloud.fail_before_apply = True
        unknown = self.push()
        self.assertEqual(unknown["status"], "unknown")
        with MarketReviewRepository(self.db, state_dir=self.state) as repo:
            with self.assertRaises(RemoteStoreError) as ctx:
                repo.save_review(DAY, {"pe_sh": 2.0})
        self.assertEqual(ctx.exception.code, "PENDING_WRITE")
        with MarketReviewRepository(self.db, state_dir=self.state) as repo:
            self.assertEqual(repo.get_review(DAY).pe_sh, 1.0)

    def test_cli_push_reports_source_path_and_project(self) -> None:
        config = self.root / "config"
        config.mkdir()
        (config / "config").write_text(
            "backend=sqlite\nsupabase_url=https://abc.supabase.co\n",
            encoding="utf-8",
        )
        (config / "supabase.secret").write_text("sb_secret_test_value\n", encoding="utf-8")
        seen: dict[str, Any] = {}

        def fake_push(**kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs)
            return {
                "command": "push",
                "status": "completed",
                "sqlite_path": str(kwargs["sqlite_path"]),
                "project_id": kwargs["project_id"],
            }

        with mock.patch.dict(os.environ, {"MARKETREVIEW_CONFIG_DIR": str(config)}):
            with mock.patch("cli._command_state_dir", lambda: self.state):
                with mock.patch("marketreview.sync_engine.push_sync", fake_push):
                    buffer = __import__("io").StringIO()
                    with mock.patch("sys.stdout", buffer):
                        rc = cli.main(["sync", "push", "--source", str(self.db)])
        self.assertEqual(rc, 0)
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["project_id"], "abc")
        self.assertEqual(seen["sqlite_path"], self.db.resolve())
        self.assertEqual(seen["state_dir"], self.state)
        self.assertEqual((config / "config").read_text(encoding="utf-8").splitlines()[0], "backend=sqlite")

    def test_open_pending_blocks_a_new_push(self) -> None:
        self.seed_review()
        self.cloud.fail_before_apply = True
        unknown = self.push()
        self.assertEqual(unknown["status"], "unknown")
        self.cloud.fail_before_apply = False
        self.cloud.groups["review:2026-08-22"] = _review_group("2026-08-22", 1.0, T1)
        resumed = self.push()
        self.assertEqual(resumed["status"], "completed")
        operation = None
        conn = connect(self.db)
        try:
            ensure_sync_schema(conn)
            operation = get_operation(conn, read_pending(self.state)["operation_id"] if read_pending(self.state) else resumed.get("operation_id", ""))
        finally:
            conn.close()
        self.assertTrue(operation is None or operation["state"] == "committed")


def _review_group(day: str, pe: float, stamp: str) -> dict[str, Any]:
    review = {"trade_date": day, "created_at": stamp, "updated_at": stamp}
    for name in ATOMIC_FIELD_NAMES:
        review[name] = pe if name == "pe_sh" else None
    return {
        "group_kind": "review",
        "group_key": {"trade_date": day},
        "exists": True,
        "review": review,
    }


if __name__ == "__main__":
    unittest.main()
