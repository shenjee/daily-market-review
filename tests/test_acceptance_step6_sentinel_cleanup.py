"""Step-6 B-branch sentinel cleanup: whitelist, backup gate, and execute wiring."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import _bootstrap  # noqa: F401

from marketreview.supabase_store import SupabaseRepository
from marketreview.sync_groups import digest_of

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "acceptance_step6_sentinel_cleanup.py"
FIXTURE = ROOT / "tests" / "fixtures" / "step6_sentinel_cloud_snapshot.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("acceptance_step6_sentinel_cleanup", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_fixture() -> dict:
    if not FIXTURE.is_file():
        raise unittest.SkipTest(f"missing readonly snapshot fixture: {FIXTURE}")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _envelope(**extra: Any) -> dict[str, Any]:
    payload = {"format_version": 1, "schema_version": 1, "complete": True}
    payload.update(extra)
    return payload


def _cleared_snapshot(before: dict[str, Any]) -> dict[str, Any]:
    after = copy.deepcopy(before)
    after["reviews"] = []
    after["events"] = []
    after["details"] = []
    after["sectors"] = []
    after["reasons"] = []
    after["revision"] = int(before["revision"]) + 5
    # Keep history/sync_results arrays aligned with counts; execute only asserts sentinel refs empty.
    after["counts"] = {
        **after.get("counts", {}),
        "reviews": 0,
        "events": 0,
        "details": 0,
        "sectors": 0,
        "reasons": 0,
        "history": len(after.get("history") or []),
        "sync_results": len(after.get("sync_results") or []),
    }
    return after


class FakeTransport:
    def __init__(self, before: dict[str, Any], after: dict[str, Any]) -> None:
        self.before = before
        self.after = after
        self.calls: list[tuple[str, dict[str, Any], bool]] = []
        self._snapshots_served = 0

    def call(self, function: str, request: dict[str, Any], *, write: bool) -> dict[str, Any]:
        self.calls.append((function, dict(request), write))
        if function == "marketreview_sync_snapshot":
            self._snapshots_served += 1
            return self.before if self._snapshots_served == 1 else self.after
        if function == "marketreview_probe":
            return _envelope(revision=int(self.before["revision"]))
        if function in {"marketreview_delete_event", "marketreview_delete_review"}:
            return _envelope(noop=False, revision=int(self.before["revision"]) + len(self.calls))
        raise AssertionError(f"unexpected RPC {function}")


class TestStep6SentinelCleanup(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def test_plan_accepts_exact_whitelist_from_live_fixture(self) -> None:
        plan = self.mod.build_plan(_load_fixture())
        self.assertEqual(set(plan["whitelist_refs"]), self.mod.ALLOWED_REFS)
        self.assertEqual(len(plan["steps"]), 5)

    def test_plan_refuses_extra_2099_09_event(self) -> None:
        snap = copy.deepcopy(_load_fixture())
        sample = copy.deepcopy(snap["events"][0])
        sample["code"] = "600000"
        snap["events"].append(sample)
        snap["counts"]["events"] = len(snap["events"])
        with self.assertRaises(self.mod.SentinelCleanupError):
            self.mod.build_plan(snap)

    def test_plan_refuses_missing_whitelist_event(self) -> None:
        snap = copy.deepcopy(_load_fixture())
        snap["events"] = snap["events"][:-1]
        removed = ("2099-09-02", "bj", "830799", "up")
        for name in ("details", "sectors", "reasons"):
            snap[name] = [
                row
                for row in snap[name]
                if (
                    row.get("trade_date"),
                    row.get("market"),
                    row.get("code"),
                    row.get("direction"),
                )
                != removed
            ]
            snap["counts"][name] = len(snap[name])
        snap["counts"]["events"] = len(snap["events"])
        with self.assertRaises(self.mod.SentinelCleanupError):
            self.mod.build_plan(snap)

    def test_assert_trade_date_rejects_capacity_and_real(self) -> None:
        with self.assertRaises(self.mod.SentinelCleanupError):
            self.mod.assert_trade_date_allowed("2099-08-01")
        with self.assertRaises(self.mod.SentinelCleanupError):
            self.mod.assert_trade_date_allowed("2026-09-30")
        self.assertEqual(self.mod.assert_trade_date_allowed("2099-09-01"), "2099-09-01")

    def test_execute_requires_backup_ok_before_any_delete(self) -> None:
        before = _load_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "nobackup"
            bad.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            transport = FakeTransport(before, before)
            repo = SupabaseRepository(
                transport,
                state_dir=state,
                project_ref="example.supabase.co",
            )
            with self.assertRaises(self.mod.SentinelCleanupError) as raised:
                self.mod.execute_cleanup(
                    repo=repo,
                    transport=transport,
                    pre_backup_dir=bad,
                    expected_project_ref="example.supabase.co",
                )
            self.assertIn("BACKUP_OK", str(raised.exception))
            write_calls = [name for name, _req, write in transport.calls if write]
            self.assertEqual(write_calls, [])

    def test_require_pre_backup_rejects_filename_only_fake_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake"
            fake.mkdir()
            (fake / "BACKUP_OK").write_text("ok\n", encoding="utf-8")
            (fake / "CHECKSUMS").write_text("bogus  BACKUP_OK\n", encoding="utf-8")
            with self.assertRaises(self.mod.SentinelCleanupError) as raised:
                self.mod.require_pre_backup(
                    fake,
                    expected_revision=35,
                    expected_snapshot_digest="deadbeef",
                    expected_project_ref="example.supabase.co",
                )
            self.assertIn("校验失败", str(raised.exception))

    def test_require_pre_backup_rejects_revision_mismatch_after_checksums(self) -> None:
        before = _load_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "backup"
            backup.mkdir()
            with mock.patch.object(self.mod, "_verify_checksums"):
                (backup / "BACKUP_OK").write_text("ok\n", encoding="utf-8")
                (backup / "manifest.json").write_text(
                    json.dumps(
                        {
                            "revision": 21,
                            "retention": "migration-snapshot",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (backup / "snapshot.json").write_text(
                    json.dumps(before) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(self.mod.SentinelCleanupError) as raised:
                    self.mod.require_pre_backup(
                        backup,
                        expected_revision=int(before["revision"]),
                        expected_snapshot_digest=digest_of(before),
                        expected_project_ref="example.supabase.co",
                    )
            self.assertIn("revision", str(raised.exception))

    def test_require_pre_backup_rejects_snapshot_digest_mismatch(self) -> None:
        before = _load_fixture()
        other = copy.deepcopy(before)
        other["revision"] = before["revision"]
        other["counts"] = {**before["counts"], "history": int(before["counts"]["history"]) + 1}
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "backup"
            backup.mkdir()
            with mock.patch.object(self.mod, "_verify_checksums"):
                (backup / "BACKUP_OK").write_text("ok\n", encoding="utf-8")
                (backup / "manifest.json").write_text(
                    json.dumps(
                        {
                            "revision": before["revision"],
                            "retention": "migration-snapshot",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (backup / "snapshot.json").write_text(json.dumps(other) + "\n", encoding="utf-8")
                with self.assertRaises(self.mod.SentinelCleanupError) as raised:
                    self.mod.require_pre_backup(
                        backup,
                        expected_revision=int(before["revision"]),
                        expected_snapshot_digest=digest_of(before),
                        expected_project_ref="example.supabase.co",
                    )
            self.assertIn("digest", str(raised.exception))

    def test_execute_with_state_dir_completes_five_deletes(self) -> None:
        before = _load_fixture()
        after = _cleared_snapshot(before)
        transport = FakeTransport(before, after)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            backup = root / "backup"
            backup.mkdir()
            (backup / "BACKUP_OK").write_text("ok\n", encoding="utf-8")
            (backup / "manifest.json").write_text(
                json.dumps(
                    {
                        "revision": before["revision"],
                        "retention": "migration-snapshot",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (backup / "snapshot.json").write_text(json.dumps(before) + "\n", encoding="utf-8")
            repo = SupabaseRepository(
                transport,
                clock=lambda: "2026-10-02T00:00:00+00:00",
                state_dir=state,
                project_ref="example.supabase.co",
            )
            with mock.patch.object(self.mod, "_verify_checksums"):
                result = self.mod.execute_cleanup(
                    repo=repo,
                    transport=transport,
                    pre_backup_dir=backup,
                    expected_project_ref="example.supabase.co",
                )
            self.assertEqual(result["before_revision"], before["revision"])
            self.assertEqual(result["after_revision"], after["revision"])
            self.assertEqual(len(result["deleted_events"]), 3)
            self.assertEqual(len(result["deleted_reviews"]), 2)
            self.assertEqual(result["after_sentinel_refs"], [])
            write_names = [name for name, _req, write in transport.calls if write]
            self.assertEqual(
                write_names.count("marketreview_delete_event"),
                3,
            )
            self.assertEqual(
                write_names.count("marketreview_delete_review"),
                2,
            )

    def test_execute_without_state_dir_stops_before_writes(self) -> None:
        before = _load_fixture()
        transport = FakeTransport(before, before)
        repo = SupabaseRepository(transport)  # missing state_dir/project_ref
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "backup"
            backup.mkdir()
            (backup / "BACKUP_OK").write_text("ok\n", encoding="utf-8")
            with self.assertRaises(self.mod.SentinelCleanupError) as raised:
                self.mod.execute_cleanup(
                    repo=repo,
                    transport=transport,
                    pre_backup_dir=backup,
                    expected_project_ref="example.supabase.co",
                )
            self.assertEqual(raised.exception.code, "PENDING_UNREADABLE")
            self.assertEqual(transport.calls, [])

    def test_main_plan_with_saved_snapshot_no_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "plan.json"
            rc = self.mod.main(["plan", "--snapshot", str(FIXTURE), "--out", str(out_path)])
            self.assertEqual(rc, 0)
            plan = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["kind"], "step6_sentinel_cleanup_plan")

    def test_build_repository_injects_state_dir_and_project_ref(self) -> None:
        transport = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            settings = mock.Mock()
            settings.url = "https://example.supabase.co"
            repo, ref = self.mod.build_repository(
                transport=transport,
                state_dir=state,
                settings=settings,
            )
            self.assertEqual(ref, "example.supabase.co")
            self.assertEqual(repo._state_dir, state)
            self.assertEqual(repo._project_ref, "example.supabase.co")
            self.assertTrue(state.is_dir())


if __name__ == "__main__":
    unittest.main()
