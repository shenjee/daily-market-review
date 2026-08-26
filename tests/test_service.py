"""Tests for persistence helpers."""

from __future__ import annotations

import sqlite3
import unittest

import _bootstrap  # noqa: F401

from marketreview.repository import MarketReviewRepository
from marketreview.schema import PriceLimitEventInput
from marketreview.service import missing_atomic_fields


class TestMissingAtomicFields(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.repo = MarketReviewRepository(self.conn)

    def tearDown(self) -> None:
        self.repo.close()
        self.conn.close()

    def test_missing_atomic_fields_when_review_absent(self) -> None:
        missing = missing_atomic_fields(self.repo, "2026-08-21")
        self.assertIn("sh_index_close", missing)
        self.assertIn("pe_sh", missing)

    def test_missing_atomic_fields_omits_saved_values(self) -> None:
        self.repo.save_review("2026-08-21", fields={"pe_sh": 17.0})
        missing = missing_atomic_fields(self.repo, "2026-08-21")
        self.assertNotIn("pe_sh", missing)
        self.assertIn("pe_sz", missing)

    def test_missing_atomic_fields_when_only_events_exist(self) -> None:
        self.repo.save_price_limit_events(
            "2026-08-21",
            [PriceLimitEventInput("sh", "600519", "贵州茅台", "up", True, 1000, 1)],
        )
        missing = missing_atomic_fields(self.repo, "2026-08-21")
        self.assertIn("pe_sh", missing)


if __name__ == "__main__":
    unittest.main()
