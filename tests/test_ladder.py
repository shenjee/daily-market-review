"""Tests for the daily ladder read-side view."""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from marketreview.ladder import build_ladder, compute_auction_ratio, compute_open_change
from marketreview.repository import MarketReviewRepository
from marketreview.schema import PriceLimitEventInput, PriceLimitEventRecord
from marketreview.summary import compute_summary

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden_2026_08_21.json"


def _record(**kwargs) -> PriceLimitEventRecord:
    payload = {
        "trade_date": "2026-08-21",
        "market": "sh",
        "code": "600519",
        "name": "贵州茅台",
        "direction": "up",
        "closed_at_limit": True,
        "limit_rate_bp": 1000,
        "streak_height": 1,
    }
    payload.update(kwargs)
    return PriceLimitEventRecord(**payload)


class TestLadderDerivedFields(unittest.TestCase):
    def test_auction_ratio_and_open_change(self) -> None:
        self.assertAlmostEqual(compute_auction_ratio(326000000, 8450000000), 326000000 / 8450000000)
        self.assertAlmostEqual(compute_open_change(1568.0, 1520.5), 1568.0 / 1520.5 - 1)

    def test_derived_fields_are_null_when_missing_or_zero_denominator(self) -> None:
        self.assertIsNone(compute_auction_ratio(None, 100.0))
        self.assertIsNone(compute_auction_ratio(10.0, None))
        self.assertIsNone(compute_auction_ratio(10.0, 0))
        self.assertIsNone(compute_open_change(None, 10.0))
        self.assertIsNone(compute_open_change(11.0, None))
        self.assertIsNone(compute_open_change(11.0, 0))


class TestLadderGrouping(unittest.TestCase):
    def test_first_board_enters_height_one_group(self) -> None:
        ladder = build_ladder([_record(streak_height=1)])
        self.assertEqual([group.streak_height for group in ladder.groups], [1])
        self.assertEqual(ladder.groups[0].count, 1)
        self.assertEqual(ladder.groups[0].stocks[0].code, "600519")

    def test_groups_are_sorted_by_actual_height_descending(self) -> None:
        ladder = build_ladder(
            [
                _record(code="600519", name="贵州茅台", streak_height=4),
                _record(market="sz", code="000858", name="五粮液", streak_height=2),
                _record(market="bj", code="830799", name="艾融软件", streak_height=1),
                _record(code="688001", name="十五板", streak_height=15, limit_rate_bp=2000),
            ]
        )
        self.assertEqual([group.streak_height for group in ladder.groups], [15, 4, 2, 1])
        self.assertEqual([group.count for group in ladder.groups], [1, 1, 1, 1])

    def test_broken_and_limit_down_go_to_auxiliary_lists(self) -> None:
        ladder = build_ladder(
            [
                _record(streak_height=1),
                _record(
                    market="sz",
                    code="300001",
                    name="示例科技",
                    closed_at_limit=False,
                    streak_height=0,
                ),
                _record(
                    code="600002",
                    name="示例实业",
                    direction="down",
                    closed_at_limit=False,
                    streak_height=0,
                ),
                _record(
                    market="sz",
                    code="000002",
                    name="收盘跌停",
                    direction="down",
                    closed_at_limit=True,
                    streak_height=0,
                ),
            ]
        )
        self.assertEqual(len(ladder.groups), 1)
        self.assertEqual([item.code for item in ladder.broken_limit_up], ["300001"])
        self.assertEqual([item.code for item in ladder.opened_limit_down], ["600002"])
        self.assertEqual([item.code for item in ladder.closed_limit_down], ["000002"])

    def test_same_height_sorts_by_market_and_code(self) -> None:
        ladder = build_ladder(
            [
                _record(market="sz", code="000858", name="五粮液", streak_height=1),
                _record(market="bj", code="830799", name="艾融软件", streak_height=1),
                _record(market="sh", code="600519", name="贵州茅台", streak_height=1),
            ]
        )
        codes = [item.code for item in ladder.groups[0].stocks]
        self.assertEqual(codes, ["830799", "600519", "000858"])

    def test_missing_detail_still_enters_ladder(self) -> None:
        ladder = build_ladder([_record(streak_height=2)])
        stock = ladder.groups[0].stocks[0]
        self.assertEqual(stock.sectors, [])
        self.assertEqual(stock.limit_up_reasons, [])
        self.assertIsNone(stock.is_leader)
        self.assertIsNone(stock.auction_ratio)
        self.assertIsNone(stock.open_change)

    def test_dirty_zero_height_closed_limit_up_is_excluded(self) -> None:
        events = [_record(streak_height=0, closed_at_limit=True, direction="up")]
        ladder = build_ladder(events)
        summary = compute_summary(None, events)
        self.assertEqual(summary["effective_limit_up_count"], 1)
        self.assertEqual(ladder.groups, [])


class TestGoldenLadder(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.conn = sqlite3.connect(":memory:")
        self.repo = MarketReviewRepository(self.conn)
        self.repo.save_review(self.fixture["trade_date"], fields=self.fixture["atoms"])
        self.repo.save_price_limit_events(
            self.fixture["trade_date"],
            [PriceLimitEventInput(**event) for event in self.fixture["price_limit_events"]],
        )
        self.repo.save_price_limit_event_details(
            self.fixture["trade_date"],
            self.fixture["event_details"],
        )

    def tearDown(self) -> None:
        self.repo.close()
        self.conn.close()

    def test_golden_ladder_matches_effective_limit_up_and_preserves_v1_summary(self) -> None:
        events = self.repo.get_price_limit_events(self.fixture["trade_date"])
        details = self.repo.get_price_limit_event_details(self.fixture["trade_date"])
        review = self.repo.get_review(self.fixture["trade_date"])
        summary = compute_summary(review, events)
        ladder = build_ladder(events, details)

        self.assertEqual(summary["effective_limit_up_count"], 3)
        self.assertEqual(summary["limit_up_broken_count"], 1)
        self.assertEqual(summary["down_opened_count"], 1)
        self.assertEqual(summary["down_closed_count"], 1)
        self.assertEqual(summary["first_board_count"], 1)
        self.assertEqual(summary["streak_board_count"], 2)
        self.assertEqual(summary["highest_board"], 4)
        self.assertEqual(summary["streak_by_height"], {1: 1, 2: 1, 4: 1})

        self.assertEqual(sum(group.count for group in ladder.groups), summary["effective_limit_up_count"])
        self.assertEqual(
            {group.streak_height: group.count for group in ladder.groups},
            summary["streak_by_height"],
        )
        self.assertEqual([group.streak_height for group in ladder.groups], [4, 2, 1])

        leader = ladder.groups[0].stocks[0]
        self.assertEqual(leader.code, "600519")
        self.assertIs(leader.is_leader, True)
        self.assertEqual(leader.sectors, ["白酒", "消费"])
        self.assertEqual(leader.limit_up_reasons, ["业绩增长", "消费复苏"])
        self.assertAlmostEqual(leader.auction_ratio, 326000000 / 8450000000)
        self.assertAlmostEqual(leader.open_change, 1568.0 / 1520.5 - 1)

        missing = ladder.groups[1].stocks[0]
        self.assertEqual(missing.code, "000858")
        self.assertEqual(missing.sectors, [])
        self.assertIsNone(missing.is_leader)

        first_board = ladder.groups[2].stocks[0]
        self.assertEqual(first_board.code, "830799")
        self.assertIs(first_board.is_leader, False)
        self.assertAlmostEqual(first_board.open_change, 0.3)
        self.assertIsNone(first_board.auction_ratio)

        broken = ladder.broken_limit_up[0]
        self.assertEqual(broken.code, "300001")
        self.assertEqual(broken.sectors, ["科技"])
        self.assertIsNone(broken.auction_ratio)

        self.assertEqual(ladder.opened_limit_down[0].code, "600519")
        self.assertEqual(ladder.closed_limit_down[0].code, "600002")
        self.assertEqual(ladder.closed_limit_down[0].sectors, ["地产"])
        self.assertEqual(ladder.closed_limit_down[0].limit_up_reasons, [])


if __name__ == "__main__":
    unittest.main()
