"""Long-term migration register records sync metadata without touching production."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

import acceptance_migration_register as register
import acceptance_sqlite_consistency_backup as backup
from marketreview.errors import MarketReviewError
from marketreview.sqlite_schema import connect, init_db
from marketreview.sync_engine import assert_pull_target_allowed, pull_sync
from marketreview.sync_ledger import ensure_ledger, ensure_sync_schema


class SyncCensusTest(unittest.TestCase):
    def test_missing_sync_tables_are_recorded_as_absent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "plain.sqlite3"
            conn = connect(path)
            try:
                init_db(conn)
                census = backup.sync_census(conn)
            finally:
                conn.close()
        self.assertFalse(census["ledger_identity_present"])
        self.assertFalse(census["tables"]["sync_baseline"]["present"])
        self.assertEqual(census["tables"]["sync_baseline"]["rows"], 0)

    def test_ledger_without_baselines_is_present_and_empty(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "synced.sqlite3"
            conn = connect(path)
            try:
                init_db(conn)
                ensure_sync_schema(conn)
                ensure_ledger(conn)
                census = backup.sync_census(conn)
            finally:
                conn.close()
        self.assertTrue(census["ledger_identity_present"])
        self.assertEqual(census["tables"]["sync_ledger_singleton"]["rows"], 1)
        self.assertEqual(census["tables"]["sync_baseline"]["rows"], 0)
        self.assertTrue(census["tables"]["sync_operation"]["present"])


class RegisterTest(unittest.TestCase):
    def _prepare_sqlite_and_cloud(self, root: Path) -> tuple[Path, Path]:
        sqlite_dir = root / "sqlite"
        sqlite_dir.mkdir()
        source = root / "source.sqlite3"
        conn = connect(source)
        try:
            init_db(conn)
        finally:
            conn.close()
        backup.online_backup(source, sqlite_dir / "market_review.sqlite3.consistent")
        inv = backup.inventory(source, sqlite_dir / "market_review.sqlite3.consistent", "m3")
        (sqlite_dir / "inventory.json").write_text(json.dumps(inv), encoding="utf-8")
        cloud = root / "cloud"
        cloud.mkdir()
        (cloud / "BACKUP_OK").write_text("ok\n", encoding="utf-8")
        (cloud / "CHECKSUMS").write_text("abc  BACKUP_OK\n", encoding="utf-8")
        (cloud / "manifest.json").write_text(
            json.dumps(
                {
                    "retention": "migration-snapshot",
                    "created_at": "2026-10-01T00:00:00Z",
                    "revision": 3,
                }
            ),
            encoding="utf-8",
        )
        return sqlite_dir, cloud

    def test_register_binds_checksums_and_refuses_daily_cloud_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sqlite_dir, cloud = self._prepare_sqlite_and_cloud(root)
            manifest = json.loads((cloud / "manifest.json").read_text(encoding="utf-8"))
            manifest["retention"] = "daily"
            (cloud / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(SystemExit):
                register.build_register([sqlite_dir], [cloud])
            manifest["retention"] = "migration-snapshot"
            (cloud / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            payload = register.build_register([sqlite_dir], [cloud])
        self.assertEqual(payload["policy"]["retention"]["keep_recent"], 30)
        self.assertTrue(payload["cloud_snapshots"][0]["long_term"])
        self.assertEqual(payload["sqlite_snapshots"][0]["role"], "m3")
        self.assertFalse(payload["sqlite_snapshots"][0]["sync_metadata"]["ledger_identity_present"])
        self.assertTrue(payload["sqlite_snapshots"][0]["matches_existing_inventory"])

    def test_register_does_not_replace_an_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw)
            target = out / "migration_register.json"
            target.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                register.main(
                    [
                        "--out",
                        str(out),
                        "--sqlite",
                        str(out),
                        "--cloud",
                        str(out),
                        "--state-dir",
                        str(out / "state"),
                    ]
                )

    def test_register_protects_sqlite_copies_from_pull(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sqlite_dir, cloud = self._prepare_sqlite_and_cloud(root)
            out = root / "register"
            state = root / "state"
            result = register.main(
                [
                    "--out",
                    str(out),
                    "--sqlite",
                    str(sqlite_dir),
                    "--cloud",
                    str(cloud),
                    "--state-dir",
                    str(state),
                ]
            )
            self.assertEqual(result, 0)
            backup_path = sqlite_dir / "market_review.sqlite3.consistent"
            with self.assertRaises(MarketReviewError) as ctx:
                assert_pull_target_allowed(backup_path, state)
            self.assertEqual(ctx.exception.code, "TARGET_FORBIDDEN")
            alias = root / "alias.sqlite3"
            os.link(backup_path, alias)
            with self.assertRaises(MarketReviewError) as ctx:
                assert_pull_target_allowed(alias, state)
            self.assertEqual(ctx.exception.code, "TARGET_FORBIDDEN")
            link = root / "link.sqlite3"
            link.symlink_to(backup_path)
            with self.assertRaises(MarketReviewError) as ctx:
                pull_sync(
                    sqlite_path=link,
                    transport=_RefuseTransport(),
                    project_id="project1",
                    state_dir=state,
                    choices=[],
                )
            self.assertEqual(ctx.exception.code, "TARGET_FORBIDDEN")
            self.assertEqual(
                json.loads((state / "protected-snapshots.json").read_text(encoding="utf-8"))["paths"],
                [str(backup_path.resolve())],
            )

    def test_protect_register_cli_does_not_rewrite_register(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sqlite_dir, cloud = self._prepare_sqlite_and_cloud(root)
            out = root / "register"
            first_state = root / "first-state"
            register.main(
                [
                    "--out",
                    str(out),
                    "--sqlite",
                    str(sqlite_dir),
                    "--cloud",
                    str(cloud),
                    "--state-dir",
                    str(first_state),
                ]
            )
            register_path = out / "migration_register.json"
            before = register_path.read_bytes()
            later_state = root / "later-state"
            result = register.main(
                [
                    "--protect-register",
                    str(register_path),
                    "--state-dir",
                    str(later_state),
                ]
            )
            self.assertEqual(result, 0)
            self.assertEqual(register_path.read_bytes(), before)
            backup_path = sqlite_dir / "market_review.sqlite3.consistent"
            with self.assertRaises(MarketReviewError) as ctx:
                assert_pull_target_allowed(backup_path, later_state)
            self.assertEqual(ctx.exception.code, "TARGET_FORBIDDEN")


class _RefuseTransport:
    def call(self, function: str, request: dict, *, write: bool = False) -> dict:
        raise AssertionError(f"pull must stop before cloud call: {function}")


if __name__ == "__main__":
    unittest.main()
