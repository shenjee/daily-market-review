"""Repository tests for price-limit event details."""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from marketreview.errors import InvalidFieldValueError
from marketreview.repository import MarketReviewRepository
from marketreview.schema import PriceLimitEventDetailPatch, PriceLimitEventInput

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden_2026_08_21.json"


def _event(
    market: str = "sh",
    code: str = "600519",
    name: str = "贵州茅台",
    direction: str = "up",
    closed_at_limit: bool = True,
    limit_rate_bp: int = 1000,
    streak_height: int = 1,
) -> PriceLimitEventInput:
    return PriceLimitEventInput(
        market,
        code,
        name,
        direction,
        closed_at_limit,
        limit_rate_bp,
        streak_height,
    )


class TestPriceLimitEventDetails(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.repo = MarketReviewRepository(self.conn)

    def tearDown(self) -> None:
        self.repo.close()
        self.conn.close()

    def _save_parent(self, **kwargs) -> None:
        self.repo.save_price_limit_events("2026-08-21", [_event(**kwargs)])

    def _get_one(self):
        details = self.repo.get_price_limit_event_details("2026-08-21")
        self.assertEqual(len(details), 1)
        return details[0]

    def test_golden_fixture_details_round_trip(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.repo.save_price_limit_events(
            fixture["trade_date"],
            [PriceLimitEventInput(**event) for event in fixture["price_limit_events"]],
        )
        self.repo.save_price_limit_event_details(fixture["trade_date"], fixture["event_details"])
        saved = {
            (item.market, item.code, item.direction): item
            for item in self.repo.get_price_limit_event_details(fixture["trade_date"])
        }
        self.assertEqual(len(saved), len(fixture["event_details"]))
        maotai = saved[("sh", "600519", "up")]
        self.assertEqual(maotai.sectors, ["白酒", "消费"])
        self.assertEqual(maotai.limit_up_reasons, ["业绩增长", "消费复苏"])
        self.assertEqual(maotai.is_leader, True)
        self.assertEqual(maotai.turnover_rate, 0.0125)
        missing_detail = ("sz", "000858", "up")
        self.assertNotIn(missing_detail, saved)

    def test_scalar_patch_add_update_clear_and_preserve(self) -> None:
        self._save_parent()
        self.repo.save_price_limit_event_details(
            "2026-08-21",
            [
                {
                    "market": "sh",
                    "code": "600519",
                    "direction": "up",
                    "previous_close": 10.0,
                    "open_price": 11.0,
                    "is_leader": True,
                    "note": "初值",
                }
            ],
        )
        self.repo.save_price_limit_event_details(
            "2026-08-21",
            [
                {
                    "market": "sh",
                    "code": "600519",
                    "direction": "up",
                    "open_price": 12.0,
                    "is_leader": None,
                }
            ],
        )
        record = self._get_one()
        self.assertEqual(record.previous_close, 10.0)
        self.assertEqual(record.open_price, 12.0)
        self.assertIsNone(record.is_leader)
        self.assertEqual(record.note, "初值")

        self.repo.save_price_limit_event_details(
            "2026-08-21",
            [
                {
                    "market": "sh",
                    "code": "600519",
                    "direction": "up",
                    "note": None,
                    "previous_turnover_amount": 0,
                }
            ],
        )
        record = self._get_one()
        self.assertIsNone(record.note)
        self.assertEqual(record.previous_turnover_amount, 0.0)
        self.assertEqual(record.previous_close, 10.0)

    def test_false_zero_and_null_are_distinct(self) -> None:
        self._save_parent()
        self.repo.save_price_limit_event_details(
            "2026-08-21",
            [
                {
                    "market": "sh",
                    "code": "600519",
                    "direction": "up",
                    "is_leader": False,
                    "turnover_amount": 0,
                    "note": None,
                }
            ],
        )
        record = self._get_one()
        self.assertIs(record.is_leader, False)
        self.assertEqual(record.turnover_amount, 0.0)
        self.assertIsNone(record.note)

    def test_is_leader_rejects_integer_zero_and_one(self) -> None:
        self._save_parent()
        with self.assertRaises(InvalidFieldValueError):
            self.repo.save_price_limit_event_details(
                "2026-08-21",
                [
                    {
                        "market": "sh",
                        "code": "600519",
                        "direction": "up",
                        "is_leader": 1,
                    }
                ],
            )
        self.assertEqual(self.repo.get_price_limit_event_details("2026-08-21"), [])

    def test_list_replace_clear_preserve_dedup_and_order(self) -> None:
        self._save_parent()
        self.repo.save_price_limit_event_details(
            "2026-08-21",
            [
                {
                    "market": "sh",
                    "code": "600519",
                    "direction": "up",
                    "sectors": [" 白酒 ", "消费", "白酒"],
                    "limit_up_reasons": ["业绩", " 消费复苏 ", "业绩"],
                }
            ],
        )
        with self.assertRaises(InvalidFieldValueError):
            self.repo.save_price_limit_event_details(
                "2026-08-21",
                [
                    {
                        "market": "sh",
                        "code": "600519",
                        "direction": "up",
                        "sectors": ["白酒", ""],
                    }
                ],
            )
        record = self._get_one()
        self.assertEqual(record.sectors, ["白酒", "消费"])
        self.assertEqual(record.limit_up_reasons, ["业绩", "消费复苏"])

        self.repo.save_price_limit_event_details(
            "2026-08-21",
            [
                {
                    "market": "sh",
                    "code": "600519",
                    "direction": "up",
                    "sectors": [],
                }
            ],
        )
        record = self._get_one()
        self.assertEqual(record.sectors, [])
        self.assertEqual(record.limit_up_reasons, ["业绩", "消费复苏"])

    def test_sectors_can_be_saved_without_detail_row(self) -> None:
        self._save_parent()
        self.repo.save_price_limit_event_details(
            "2026-08-21",
            [
                {
                    "market": "sh",
                    "code": "600519",
                    "direction": "up",
                    "sectors": ["白酒"],
                }
            ],
        )
        row = self.conn.execute(
            "SELECT COUNT(*) FROM daily_price_limit_event_detail"
        ).fetchone()
        self.assertEqual(row[0], 0)
        record = self._get_one()
        self.assertEqual(record.sectors, ["白酒"])
        self.assertIsNone(record.previous_close)

    def test_missing_parent_rejects_entire_batch(self) -> None:
        self._save_parent(code="600519")
        with self.assertRaises(InvalidFieldValueError) as ctx:
            self.repo.save_price_limit_event_details(
                "2026-08-21",
                [
                    {
                        "market": "sh",
                        "code": "600519",
                        "direction": "up",
                        "note": "应回滚",
                    },
                    {
                        "market": "sz",
                        "code": "000858",
                        "direction": "up",
                        "note": "父事件不存在",
                    },
                ],
            )
        self.assertIn("父事件不存在", str(ctx.exception))
        self.assertEqual(self.repo.get_price_limit_event_details("2026-08-21"), [])

    def test_invalid_item_rolls_back_entire_batch(self) -> None:
        self._save_parent()
        with self.assertRaises(InvalidFieldValueError):
            self.repo.save_price_limit_event_details(
                "2026-08-21",
                [
                    {
                        "market": "sh",
                        "code": "600519",
                        "direction": "up",
                        "previous_close": 10.0,
                    },
                    {
                        "market": "sh",
                        "code": "600519",
                        "direction": "up",
                        "open_price": 0,
                    },
                ],
            )
        self.assertEqual(self.repo.get_price_limit_event_details("2026-08-21"), [])

    def test_duplicate_identities_in_batch_are_rejected(self) -> None:
        self._save_parent()
        with self.assertRaises(InvalidFieldValueError):
            self.repo.save_price_limit_event_details(
                "2026-08-21",
                [
                    {
                        "market": "sh",
                        "code": "600519",
                        "direction": "up",
                        "note": "第一次",
                    },
                    {
                        "market": "sh",
                        "code": "600519",
                        "direction": "up",
                        "note": "第二次",
                    },
                ],
            )
        self.assertEqual(self.repo.get_price_limit_event_details("2026-08-21"), [])

    def test_empty_details_is_noop(self) -> None:
        self._save_parent()
        self.repo.save_price_limit_event_details("2026-08-21", [])
        self.assertEqual(self.repo.get_price_limit_event_details("2026-08-21"), [])

    def test_limit_up_reasons_rejected_for_down_event(self) -> None:
        self._save_parent(direction="down", streak_height=0)
        with self.assertRaises(InvalidFieldValueError) as ctx:
            self.repo.save_price_limit_event_details(
                "2026-08-21",
                [
                    {
                        "market": "sh",
                        "code": "600519",
                        "direction": "down",
                        "limit_up_reasons": ["误标"],
                    }
                ],
            )
        self.assertIn("limit_up_reasons", str(ctx.exception))
        self.assertEqual(self.repo.get_price_limit_event_details("2026-08-21"), [])

    def test_down_event_can_save_sectors(self) -> None:
        self._save_parent(direction="down", streak_height=0)
        self.repo.save_price_limit_event_details(
            "2026-08-21",
            [
                {
                    "market": "sh",
                    "code": "600519",
                    "direction": "down",
                    "sectors": ["地产"],
                }
            ],
        )
        record = self._get_one()
        self.assertEqual(record.sectors, ["地产"])
        self.assertEqual(record.limit_up_reasons, [])

    def test_delete_event_cascades_extension_rows(self) -> None:
        self._save_parent()
        self.repo.save_price_limit_event_details(
            "2026-08-21",
            [
                {
                    "market": "sh",
                    "code": "600519",
                    "direction": "up",
                    "sectors": ["白酒"],
                    "limit_up_reasons": ["业绩"],
                    "note": "将被级联删除",
                }
            ],
        )
        self.repo.delete_price_limit_event("2026-08-21", "sh", "600519", "up")
        self.assertEqual(self.repo.get_price_limit_event_details("2026-08-21"), [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM daily_price_limit_event_detail").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM daily_price_limit_event_sector").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM daily_price_limit_event_reason").fetchone()[0],
            0,
        )

    def test_replace_direction_migrates_generic_fields_but_not_reasons(self) -> None:
        self._save_parent(direction="up", streak_height=4)
        self.repo.save_price_limit_event_details(
            "2026-08-21",
            [
                {
                    "market": "sh",
                    "code": "600519",
                    "direction": "up",
                    "sectors": ["白酒"],
                    "limit_up_reasons": ["业绩增长"],
                    "previous_close": 1520.5,
                    "is_leader": True,
                    "note": "身份修订",
                }
            ],
        )
        self.repo.replace_price_limit_event_direction(
            "2026-08-21",
            "sh",
            "600519",
            "up",
            _event(direction="down", closed_at_limit=True, streak_height=0),
        )
        events = self.repo.get_price_limit_events("2026-08-21")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].direction, "down")
        record = self._get_one()
        self.assertEqual(record.direction, "down")
        self.assertEqual(record.sectors, ["白酒"])
        self.assertEqual(record.limit_up_reasons, [])
        self.assertEqual(record.previous_close, 1520.5)
        self.assertIs(record.is_leader, True)
        self.assertEqual(record.note, "身份修订")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM daily_price_limit_event_reason").fetchone()[0],
            0,
        )

    def test_replace_direction_down_to_up_does_not_invent_reasons(self) -> None:
        self._save_parent(direction="down", streak_height=0)
        self.repo.save_price_limit_event_details(
            "2026-08-21",
            [
                {
                    "market": "sh",
                    "code": "600519",
                    "direction": "down",
                    "sectors": ["地产"],
                    "note": "打开跌停后改判涨停",
                }
            ],
        )
        self.repo.replace_price_limit_event_direction(
            "2026-08-21",
            "sh",
            "600519",
            "down",
            _event(direction="up", closed_at_limit=True, streak_height=1),
        )
        record = self._get_one()
        self.assertEqual(record.direction, "up")
        self.assertEqual(record.sectors, ["地产"])
        self.assertEqual(record.limit_up_reasons, [])
        self.assertEqual(record.note, "打开跌停后改判涨停")

    def test_note_whitespace_normalizes_to_null(self) -> None:
        self._save_parent()
        self.repo.save_price_limit_event_details(
            "2026-08-21",
            [
                {
                    "market": "sh",
                    "code": "600519",
                    "direction": "up",
                    "note": "   ",
                }
            ],
        )
        record = self._get_one()
        self.assertIsNone(record.note)

    def test_typed_patch_runs_the_same_validation_as_mapping(self) -> None:
        self._save_parent()
        with self.assertRaises(InvalidFieldValueError):
            self.repo.save_price_limit_event_details(
                "2026-08-21",
                [
                    PriceLimitEventDetailPatch(
                        market="sh",
                        code="600519",
                        direction="up",
                        provided_fields=frozenset({"previous_close"}),
                        previous_close=-1,
                    )
                ],
            )
        with self.assertRaises(InvalidFieldValueError):
            self.repo.save_price_limit_event_details(
                "2026-08-21",
                [
                    PriceLimitEventDetailPatch(
                        market="sh",
                        code="600519",
                        direction="up",
                        provided_fields=frozenset({"is_leader"}),
                        is_leader=1,  # type: ignore[arg-type]
                    )
                ],
            )
        with self.assertRaises(InvalidFieldValueError) as ctx:
            self.repo.save_price_limit_event_details(
                "2026-08-21",
                [
                    PriceLimitEventDetailPatch(
                        market="sh",
                        code="600519",
                        direction="up",
                        provided_fields=frozenset({"auction_ratio"}),
                    )
                ],
            )
        self.assertIn("auction_ratio", str(ctx.exception))
        self.assertEqual(self.repo.get_price_limit_event_details("2026-08-21"), [])

        self.repo.save_price_limit_event_details(
            "2026-08-21",
            [
                PriceLimitEventDetailPatch(
                    market="sh",
                    code="600519",
                    direction="up",
                    provided_fields=frozenset({"sectors", "is_leader"}),
                    sectors=(" 白酒 ", "消费", "白酒"),
                    is_leader=True,
                )
            ],
        )
        record = self._get_one()
        self.assertEqual(record.sectors, ["白酒", "消费"])
        self.assertIs(record.is_leader, True)

    def test_derived_fields_cannot_be_written(self) -> None:
        self._save_parent()
        with self.assertRaises(InvalidFieldValueError) as ctx:
            self.repo.save_price_limit_event_details(
                "2026-08-21",
                [
                    {
                        "market": "sh",
                        "code": "600519",
                        "direction": "up",
                        "auction_ratio": 0.1,
                    }
                ],
            )
        self.assertIn("auction_ratio", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
