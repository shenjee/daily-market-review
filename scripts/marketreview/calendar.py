"""Minimal A-share trading calendar for the daily market review skill."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CALENDAR_PATH = SKILL_ROOT / "assets" / "trading_calendar.json"
_WALK_LIMIT_DAYS = 800


class CalendarUnavailableError(ValueError):
    """Raised when calendar data for a year is missing."""


class TradingCalendar:
    def __init__(self, calendar_path: Path | str | None = None) -> None:
        path = Path(calendar_path) if calendar_path else DEFAULT_CALENDAR_PATH
        payload = json.loads(path.read_text(encoding="utf-8"))
        self._closed_by_year: dict[int, frozenset[date]] = {}
        for year_text, year_payload in payload.items():
            year = int(year_text)
            closed = frozenset(date.fromisoformat(item) for item in year_payload["closed_dates"])
            self._closed_by_year[year] = closed

    def is_trading_day(self, trade_date: date | str, market: str = "sh") -> bool:
        del market
        value = _as_date(trade_date)
        closed = self._load_year(value.year)
        if value.weekday() >= 5:
            return False
        return value not in closed

    def previous_trading_day(self, trade_date: date | str, market: str = "sh") -> date | None:
        del market
        cursor = _as_date(trade_date) - timedelta(days=1)
        for _ in range(_WALK_LIMIT_DAYS):
            closed = self._load_year(cursor.year)
            if cursor.weekday() < 5 and cursor not in closed:
                return cursor
            cursor -= timedelta(days=1)
        return None

    def _load_year(self, year: int) -> frozenset[date]:
        if year not in self._closed_by_year:
            raise CalendarUnavailableError(f"calendar year unavailable: {year}")
        return self._closed_by_year[year]


def _as_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)
