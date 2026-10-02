"""Long-term migration register records sync metadata without touching production."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

import acceptance_migration_register as register
import acceptance_sqlite_consistency_backup as backup
from marketreview.sqlite_schema import connect, init_db
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
    def test_register_binds_checksums_and_refuses_daily_cloud_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
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
            (sqlite_dir / "inventory.json").write_text(
                json.dumps(inv), encoding="utf-8"
            )
            cloud = root / "cloud"
            cloud.mkdir()
            (cloud / "BACKUP_OK").write_text("ok\n", encoding="utf-8")
            (cloud / "CHECKSUMS").write_text("abc  BACKUP_OK\n", encoding="utf-8")
            (cloud / "manifest.json").write_text(
                json.dumps(
                    {
                        "retention": "daily",
                        "created_at": "2026-10-01T00:00:00Z",
                        "revision": 3,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                register.build_register([sqlite_dir], [cloud])
            manifest = json.loads((cloud / "manifest.json").read_text(encoding="utf-8"))
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
                register.main(["--out", str(out), "--sqlite", str(out), "--cloud", str(out)])


if __name__ == "__main__":
    unittest.main()
