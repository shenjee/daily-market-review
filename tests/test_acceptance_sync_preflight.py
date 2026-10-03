"""Readonly sync preflight must not mutate the source DB and must not ignore baselines."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from marketreview.repository import MarketReviewRepository
from marketreview.sync_groups import read_local_groups
from marketreview.sync_ledger import ensure_ledger, ensure_sync_schema, write_baseline

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "acceptance_sync_preflight.py"
FIXTURE = ROOT / "tests" / "fixtures" / "step6_sentinel_cloud_snapshot.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("acceptance_sync_preflight", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _empty_cloud_snapshot(*, revision: int = 1, reviews: list | None = None) -> dict:
    reviews = list(reviews or [])
    return {
        "format_version": 1,
        "schema_version": 1,
        "complete": True,
        "revision": revision,
        "ledger_key": "main",
        "reviews": reviews,
        "events": [],
        "details": [],
        "sectors": [],
        "reasons": [],
        "history": [],
        "sync_results": [],
        "counts": {
            "reviews": len(reviews),
            "events": 0,
            "details": 0,
            "sectors": 0,
            "reasons": 0,
            "history": 0,
            "sync_results": 0,
        },
    }


def _seed_review(db: Path, trade_date: str = "2026-09-23") -> None:
    with MarketReviewRepository(db) as repo:
        repo.save_review(trade_date, {"advancing_count": 1, "declining_count": 2})


class TestAcceptanceSyncPreflight(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def test_first_join_does_not_create_sync_tables_or_change_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "source.sqlite3"
            _seed_review(db)
            before = _sha(db)
            report = self.mod.build_report(sqlite_path=db, snapshot=_empty_cloud_snapshot())
            after = _sha(db)
            self.assertEqual(before, after)
            self.assertTrue(report["source_unchanged"])
            self.assertEqual(report["mode"], "first_join")
            self.assertFalse(report["sync_metadata"]["any_sync_table_present"])
            self.assertEqual(report["categories"].get("local_only"), 1)
            self.assertTrue(report["ready_for_authorized_push"])
            conn = sqlite3.connect(db)
            try:
                names = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sync_%'"
                    )
                }
            finally:
                conn.close()
            self.assertEqual(names, set())

    def test_first_join_refuses_existing_sync_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "with_sync.sqlite3"
            _seed_review(db)
            conn = sqlite3.connect(db)
            try:
                ensure_sync_schema(conn)
            finally:
                conn.close()
            before = _sha(db)
            with self.assertRaises(self.mod.PreflightError) as raised:
                self.mod.build_report(sqlite_path=db, snapshot=_empty_cloud_snapshot())
            self.assertEqual(raised.exception.code, "PREFLIGHT_NOT_FIRST_JOIN")
            self.assertEqual(_sha(db), before)

    def test_existing_sync_state_reports_local_delete_not_ready(self) -> None:
        project_id = "testproject"
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "synced.sqlite3"
            _seed_review(db, "2026-09-23")
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                ensure_sync_schema(conn)
                ledger_id = ensure_ledger(conn)
                groups = read_local_groups(conn)
                group = groups["review:2026-09-23"]
                write_baseline(
                    conn,
                    project_id=project_id,
                    ledger_id=ledger_id,
                    group=group,
                    cloud_revision=3,
                    operation_id="op-1",
                    present=True,
                )
                conn.execute("DELETE FROM daily_market_review WHERE trade_date = ?", ("2026-09-23",))
                conn.commit()
                cloud_review = dict(group["review"])
            finally:
                conn.close()

            report = self.mod.build_report(
                sqlite_path=db,
                snapshot=_empty_cloud_snapshot(revision=3, reviews=[cloud_review]),
                with_existing_sync_state=True,
                project_id=project_id,
            )
            self.assertEqual(report["mode"], "existing_sync_state")
            self.assertEqual(report["categories"].get("local_delete"), 1)
            self.assertFalse(report["ready_for_authorized_push"])
            self.assertIn("review:2026-09-23", report["blockers_needing_choice"])

    def test_with_existing_sync_state_does_not_downgrade_missing_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "empty_sync.sqlite3"
            _seed_review(db)
            conn = sqlite3.connect(db)
            try:
                ensure_sync_schema(conn)
            finally:
                conn.close()
            with self.assertRaises(self.mod.PreflightError) as raised:
                self.mod.build_report(
                    sqlite_path=db,
                    snapshot=_empty_cloud_snapshot(),
                    with_existing_sync_state=True,
                    project_id="testproject",
                )
            self.assertEqual(raised.exception.code, "PREFLIGHT_SYNC_STATE_MISSING")

    def test_refuses_nonempty_wal_with_committed_identity(self) -> None:
        """N6: pending WAL must not be ignored via immutable=1 / first-join misread."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "wal-source.sqlite3"
            _seed_review(db, "2026-09-23")
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA wal_autocheckpoint=0")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                groups = read_local_groups(conn)
                group = groups["review:2026-09-23"]
                ensure_sync_schema(conn)
                ledger_id = ensure_ledger(conn)
                write_baseline(
                    conn,
                    project_id="p",
                    ledger_id=ledger_id,
                    group=group,
                    cloud_revision=35,
                    operation_id="op",
                    present=True,
                )
                conn.execute("DELETE FROM daily_market_review WHERE trade_date = ?", ("2026-09-23",))
                conn.commit()
                wal = Path(str(db) + "-wal")
                self.assertTrue(wal.is_file())
                self.assertGreater(wal.stat().st_size, 0)
                before = _sha(db)
                with self.assertRaises(self.mod.PreflightError) as raised:
                    self.mod.build_report(
                        sqlite_path=db,
                        snapshot=_empty_cloud_snapshot(revision=35, reviews=[dict(group["review"])]),
                    )
                self.assertEqual(raised.exception.code, "PREFLIGHT_WAL_PENDING")
                self.assertEqual(_sha(db), before)
            finally:
                conn.close()

    def test_main_refuses_out_hardlink_alias_of_source(self) -> None:
        """N5: --out hard-linked to source must not overwrite SQLite with JSON."""
        if not FIXTURE.is_file():
            raise unittest.SkipTest(f"missing fixture: {FIXTURE}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "out-source.sqlite3"
            _seed_review(db)
            alias = root / "output-alias.json"
            alias.hardlink_to(db)
            before = _sha(db)
            rc = self.mod.main(
                [
                    "--source",
                    str(db),
                    "--snapshot",
                    str(FIXTURE.resolve()),
                    "--out",
                    str(alias),
                ]
            )
            self.assertEqual(rc, 1)
            self.assertEqual(_sha(db), before)
            self.assertFalse(db.read_bytes().startswith(b"{"))

    def test_main_refuses_out_same_path_as_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "same.sqlite3"
            _seed_review(db)
            before = _sha(db)
            rc = self.mod.main(
                [
                    "--source",
                    str(db),
                    "--snapshot",
                    str(FIXTURE.resolve()),
                    "--out",
                    str(db),
                ]
            )
            self.assertEqual(rc, 1)
            self.assertEqual(_sha(db), before)

    def test_main_refuses_out_symlink_alias_of_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "source.sqlite3"
            _seed_review(db)
            link = root / "out-link.json"
            link.symlink_to(db)
            before = _sha(db)
            rc = self.mod.main(
                [
                    "--source",
                    str(db),
                    "--snapshot",
                    str(FIXTURE.resolve()),
                    "--out",
                    str(link),
                ]
            )
            self.assertEqual(rc, 1)
            self.assertEqual(_sha(db), before)

    def test_main_refuses_out_alias_of_snapshot(self) -> None:
        if not FIXTURE.is_file():
            raise unittest.SkipTest(f"missing fixture: {FIXTURE}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "source.sqlite3"
            _seed_review(db)
            snap_copy = root / "snap.json"
            snap_copy.write_bytes(FIXTURE.read_bytes())
            before_snap = _sha(snap_copy)
            before_db = _sha(db)
            rc = self.mod.main(
                [
                    "--source",
                    str(db),
                    "--snapshot",
                    str(snap_copy),
                    "--out",
                    str(snap_copy),
                ]
            )
            self.assertEqual(rc, 1)
            self.assertEqual(_sha(snap_copy), before_snap)
            self.assertEqual(_sha(db), before_db)

    def test_main_writes_independent_out_without_touching_source(self) -> None:
        if not FIXTURE.is_file():
            raise unittest.SkipTest(f"missing fixture: {FIXTURE}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "source.sqlite3"
            out = root / "preflight.json"
            _seed_review(db)
            before = _sha(db)
            rc = self.mod.main(
                [
                    "--source",
                    str(db),
                    "--snapshot",
                    str(FIXTURE.resolve()),
                    "--out",
                    str(out),
                ]
            )
            self.assertEqual(rc, 0)
            self.assertEqual(_sha(db), before)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["kind"], "sync_preflight_readonly")
            self.assertTrue(payload["source_unchanged"])


if __name__ == "__main__":
    unittest.main()
