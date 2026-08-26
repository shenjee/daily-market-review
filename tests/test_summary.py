"""Tests for read-side summary statistics."""

from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401

from marketreview.schema import DailyMarketReviewAtoms, PriceLimitEventRecord
from marketreview.summary import compute_summary


class TestSummary(unittest.TestCase):
    def test_compute_summary_from_events(self) -> None:
        events = [
            PriceLimitEventRecord("2026-08-21", "sh", "600519", "贵州茅台", "up", True, 1000, 4),
            PriceLimitEventRecord("2026-08-21", "sz", "300001", "示例科技", "up", False, 2000, 0),
            PriceLimitEventRecord("2026-08-21", "sh", "600002", "示例实业", "down", True, 1000, 0),
        ]
        review = DailyMarketReviewAtoms(
            trade_date="2026-08-21",
            margin_balance_sh=100.0,
            margin_balance_sz=200.0,
            turnover_amount_sh=10.0,
            turnover_amount_sz=20.0,
            sh_index_close=3200.0,
            sh_index_prev_close=3180.0,
        )
        summary = compute_summary(review, events)
        self.assertEqual(summary["effective_limit_up_count"], 1)
        self.assertEqual(summary["limit_up_broken_count"], 1)
        self.assertEqual(summary["down_closed_count"], 1)
        self.assertEqual(summary["highest_board"], 4)
        self.assertEqual(summary["margin_balance_total"], 300.0)
        self.assertEqual(summary["turnover_amount_total"], 30.0)
        self.assertEqual(summary["sh_index"]["change_points"], 20.0)


if __name__ == "__main__":
    unittest.main()
