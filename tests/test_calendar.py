"""Tests for trading calendar."""

from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401

from marketreview.calendar import TradingCalendar


class TestTradingCalendar(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = TradingCalendar()

    def test_weekend_is_not_trading_day(self) -> None:
        self.assertFalse(self.calendar.is_trading_day("2026-08-22"))

    def test_weekday_without_holiday_is_trading_day(self) -> None:
        self.assertTrue(self.calendar.is_trading_day("2026-08-21"))

    def test_holiday_is_not_trading_day(self) -> None:
        self.assertFalse(self.calendar.is_trading_day("2026-01-01"))

    def test_previous_trading_day(self) -> None:
        self.assertEqual(
            self.calendar.previous_trading_day("2026-08-21").isoformat(),
            "2026-08-20",
        )


if __name__ == "__main__":
    unittest.main()
