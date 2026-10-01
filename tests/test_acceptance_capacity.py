"""Capacity acceptance script must not delete real trade dates."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "acceptance_capacity_data_api.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("acceptance_capacity_data_api", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # scripts/ 已在模块内插入 path；先装入再执行。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestCapacityDayGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def test_assert_capacity_day_accepts_only_sentinel(self) -> None:
        self.assertEqual(self.mod.assert_capacity_day("2099-08-01"), "2099-08-01")
        with self.assertRaises(self.mod.CapacityDayError):
            self.mod.assert_capacity_day("2026-09-30")
        with self.assertRaises(self.mod.CapacityDayError):
            self.mod.assert_capacity_day("2099-09-01")
        with self.assertRaises(self.mod.CapacityDayError):
            self.mod.assert_capacity_day("2099-08-02")

    def test_main_rejects_real_trade_date_without_database(self) -> None:
        with mock.patch.object(self.mod, "_psql") as psql:
            with mock.patch.object(self.mod, "_db_password") as password:
                with self.assertRaises(SystemExit) as raised:
                    self.mod.main(["--trade-date", "2026-09-30", "--count", "1201"])
        self.assertIn("2099-08-01", str(raised.exception))
        self.assertIn("2026-09-30", str(raised.exception))
        psql.assert_not_called()
        password.assert_not_called()

    def test_insert_rejects_before_psql(self) -> None:
        with mock.patch.object(self.mod, "_psql") as psql:
            with self.assertRaises(self.mod.CapacityDayError):
                self.mod._insert_events("2026-09-30", 1201)
        psql.assert_not_called()

    def test_delete_rejects_before_psql(self) -> None:
        with mock.patch.object(self.mod, "_psql") as psql:
            with self.assertRaises(self.mod.CapacityDayError):
                self.mod._delete_events("2026-09-30")
        psql.assert_not_called()

    def test_insert_sql_hardcodes_capacity_day(self) -> None:
        captured: list[str] = []

        def fake_psql(sql: str) -> str:
            captured.append(sql)
            return "1201\n"

        with mock.patch.object(self.mod, "_psql", side_effect=fake_psql):
            self.assertEqual(self.mod._insert_events("2099-08-01", 1201), 1201)
        self.assertEqual(len(captured), 1)
        self.assertIn("WHERE trade_date = '2099-08-01'", captured[0])
        self.assertNotIn("2026-", captured[0])


if __name__ == "__main__":
    unittest.main()
