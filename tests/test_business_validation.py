"""Business validation tests."""

from __future__ import annotations

import unittest
from datetime import datetime

import _bootstrap  # noqa: F401

from marketreview.business_validation import SHANGHAI_TZ, WriteGuard
from marketreview.errors import InvalidFieldValueError


def _clock_at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=SHANGHAI_TZ)


class TestWriteGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = WriteGuard(clock=lambda: _clock_at(2026, 8, 27, 16, 0))

    def test_rejects_non_trading_day(self) -> None:
        with self.assertRaises(InvalidFieldValueError):
            self.guard.validate_write_trade_date("2026-08-22")

    def test_rejects_future_trading_day(self) -> None:
        with self.assertRaises(InvalidFieldValueError):
            self.guard.validate_write_trade_date("2026-12-31")

    def test_rejects_today_before_market_close(self) -> None:
        guard = WriteGuard(clock=lambda: _clock_at(2026, 8, 27, 14, 59))
        with self.assertRaises(InvalidFieldValueError):
            guard.validate_write_trade_date("2026-08-27")

    def test_accepts_today_after_market_close(self) -> None:
        guard = WriteGuard(clock=lambda: _clock_at(2026, 8, 27, 15, 0))
        self.assertEqual(guard.validate_write_trade_date("2026-08-27"), "2026-08-27")

    def test_accepts_past_trading_day(self) -> None:
        guard = WriteGuard(clock=lambda: _clock_at(2026, 8, 27, 10, 0))
        self.assertEqual(guard.validate_write_trade_date("2026-08-21"), "2026-08-21")

    def test_rejects_invalid_event(self) -> None:
        with self.assertRaises(InvalidFieldValueError):
            self.guard.validate_price_limit_event(
                {
                    "market": "xx",
                    "code": "oops",
                    "name": "无效",
                    "direction": "sideways",
                    "closed_at_limit": True,
                    "limit_rate_bp": 1000,
                    "streak_height": -1,
                }
            )

    def test_rejects_uppercase_market(self) -> None:
        with self.assertRaises(InvalidFieldValueError):
            self.guard.validate_price_limit_event(
                {
                    "market": "SH",
                    "code": "600519",
                    "name": "贵州茅台",
                    "direction": "up",
                    "closed_at_limit": True,
                    "limit_rate_bp": 1000,
                    "streak_height": 4,
                }
            )

    def test_rejects_fullwidth_digit_code(self) -> None:
        with self.assertRaises(InvalidFieldValueError) as ctx:
            self.guard.validate_price_limit_event(
                {
                    "market": "sz",
                    "code": "１２３４５６",
                    "name": "全角代码",
                    "direction": "up",
                    "closed_at_limit": True,
                    "limit_rate_bp": 1000,
                    "streak_height": 1,
                }
            )
        self.assertIn("code", str(ctx.exception))

    def test_accepts_valid_event(self) -> None:
        self.guard.validate_price_limit_event(
            {
                "market": "sh",
                "code": "600519",
                "name": "贵州茅台",
                "direction": "up",
                "closed_at_limit": True,
                "limit_rate_bp": 1000,
                "streak_height": 4,
            }
        )

    def test_accepts_valid_event_detail(self) -> None:
        self.guard.validate_event_detail(
            {
                "market": "sh",
                "code": "600519",
                "direction": "up",
                "sectors": ["白酒"],
                "limit_up_reasons": ["业绩增长"],
                "previous_close": 1520.5,
                "is_leader": False,
            }
        )

    def test_rejects_limit_up_reasons_on_down_event(self) -> None:
        with self.assertRaises(InvalidFieldValueError):
            self.guard.validate_event_detail(
                {
                    "market": "sh",
                    "code": "600519",
                    "direction": "down",
                    "limit_up_reasons": ["误标"],
                }
            )

    def test_rejects_non_positive_price(self) -> None:
        with self.assertRaises(InvalidFieldValueError):
            self.guard.validate_event_detail(
                {
                    "market": "sh",
                    "code": "600519",
                    "direction": "up",
                    "open_price": 0,
                }
            )

    def test_rejects_integer_is_leader(self) -> None:
        with self.assertRaises(InvalidFieldValueError):
            self.guard.validate_event_detail(
                {
                    "market": "sh",
                    "code": "600519",
                    "direction": "up",
                    "is_leader": 1,
                }
            )


if __name__ == "__main__":
    unittest.main()
