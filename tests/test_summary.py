"""Tests for read-side summary statistics."""

from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401

from marketreview.schema import DailyMarketReviewAtoms, PriceLimitEventRecord
from marketreview.summary import _limit_up_down_ratio, compute_summary


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
            margin_balance_bj=50.0,
            turnover_amount_sh=10.0,
            turnover_amount_sz=20.0,
            turnover_amount_bj=5.0,
            sh_index_close=3200.0,
            sh_index_prev_close=3180.0,
        )
        summary = compute_summary(review, events)
        self.assertEqual(summary["effective_limit_up_count"], 1)
        self.assertEqual(summary["limit_up_broken_count"], 1)
        self.assertEqual(summary["down_closed_count"], 1)
        self.assertEqual(summary["highest_board"], 4)
        self.assertEqual(summary["margin_balance_total"], 350.0)
        self.assertEqual(summary["turnover_amount_total"], 35.0)
        self.assertEqual(summary["sh_index"]["change_points"], 20.0)

    def test_totals_require_all_components(self) -> None:
        review = DailyMarketReviewAtoms(
            trade_date="2026-08-21",
            margin_balance_sh=100.0,
            margin_balance_sz=200.0,
            turnover_amount_sh=10.0,
            turnover_amount_sz=20.0,
        )
        summary = compute_summary(review, [])
        self.assertIsNone(summary["margin_balance_total"])
        self.assertIsNone(summary["turnover_amount_total"])

    def test_limit_up_down_ratio_preserves_zero_side(self) -> None:
        ratio = _limit_up_down_ratio(12, 0)
        assert ratio is not None
        self.assertEqual(ratio["display"], "12:0")

        ratio = _limit_up_down_ratio(0, 12)
        assert ratio is not None
        self.assertEqual(ratio["display"], "0:12")

    def test_limit_up_down_ratio_normalizes_both_positive(self) -> None:
        ratio = _limit_up_down_ratio(58, 15)
        assert ratio is not None
        self.assertEqual(ratio["display"], "3.87:1")

    def test_streak_rate_uses_previous_day_effective_limit_up(self) -> None:
        today = [
            PriceLimitEventRecord("2026-08-21", "sh", "600519", "贵州茅台", "up", True, 1000, 2),
            PriceLimitEventRecord("2026-08-21", "sz", "000858", "五粮液", "up", True, 1000, 1),
        ]
        previous = [
            PriceLimitEventRecord("2026-08-20", "sh", "600519", "贵州茅台", "up", True, 1000, 1),
            PriceLimitEventRecord("2026-08-20", "sz", "000858", "五粮液", "up", True, 1000, 1),
            PriceLimitEventRecord("2026-08-20", "sz", "000001", "平安银行", "up", True, 1000, 1),
            PriceLimitEventRecord("2026-08-20", "sz", "300001", "示例科技", "up", False, 2000, 0),
        ]
        summary = compute_summary(None, today, previous)
        self.assertEqual(summary["streak_board_count"], 1)
        self.assertEqual(summary["effective_limit_up_count"], 2)
        self.assertEqual(summary["streak_rate_pct"], 33.33)

    def test_streak_rate_empty_when_previous_effective_limit_up_is_zero(self) -> None:
        today = [
            PriceLimitEventRecord("2026-08-21", "sh", "600519", "贵州茅台", "up", True, 1000, 2),
        ]
        previous = [
            PriceLimitEventRecord("2026-08-20", "sz", "300001", "示例科技", "up", False, 2000, 0),
        ]
        summary = compute_summary(None, today, previous)
        self.assertIsNone(summary["streak_rate_pct"])
        summary = compute_summary(None, today)
        self.assertIsNone(summary["streak_rate_pct"])

    def test_streak_rate_zero_when_today_has_no_streak_boards(self) -> None:
        today = [
            PriceLimitEventRecord("2026-08-21", "sz", "000858", "五粮液", "up", True, 1000, 1),
        ]
        previous = [
            PriceLimitEventRecord("2026-08-20", "sh", "600519", "贵州茅台", "up", True, 1000, 1),
        ]
        summary = compute_summary(None, today, previous)
        self.assertEqual(summary["streak_rate_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
