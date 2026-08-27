"""Business validation for CLI write paths."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime, time
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .calendar import TradingCalendar
from .errors import InvalidFieldValueError
from .validation import normalize_trade_date

VALID_MARKETS = frozenset({"sh", "sz", "bj"})
VALID_DIRECTIONS = frozenset({"up", "down"})
VALID_LIMIT_RATE_BP = frozenset({1000, 2000, 3000})
_CODE_PATTERN = re.compile(r"^[0-9]{6}$")
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MARKET_CLOSE_TIME = time(15, 0)

_default_guard: "WriteGuard | None" = None


def _default_clock() -> datetime:
    return datetime.now(SHANGHAI_TZ)


class WriteGuard:
    """Validate trade dates and price-limit events before persistence."""

    def __init__(
        self,
        calendar: TradingCalendar | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._calendar = calendar or TradingCalendar()
        self._clock = clock or _default_clock

    def validate_write_trade_date(self, trade_date: str | Any) -> str:
        normalized = normalize_trade_date(trade_date)
        if not self._calendar.is_trading_day(normalized):
            previous = self._calendar.previous_trading_day(normalized)
            hint = f"，最近交易日为 {previous.isoformat()}" if previous else ""
            raise InvalidFieldValueError(f"{normalized} 不是 A 股交易日{hint}")
        self._validate_closed_trade_date(date.fromisoformat(normalized))
        return normalized

    def _validate_closed_trade_date(self, target: date) -> None:
        now = self._clock()
        if now.tzinfo is None:
            raise InvalidFieldValueError("内部时钟缺少时区信息")
        today = now.astimezone(SHANGHAI_TZ).date()
        if target > today:
            raise InvalidFieldValueError(f"{target.isoformat()} 尚未收盘，不能写入未来交易日")
        if target == today and now.astimezone(SHANGHAI_TZ).time() < MARKET_CLOSE_TIME:
            raise InvalidFieldValueError(
                f"{target.isoformat()} 尚未收盘，A 股日度复盘需等到 15:00 之后"
            )

    def validate_event_identity(self, market: str, code: str, direction: str) -> None:
        self._validate_market(market)
        self._validate_code(code)
        self._validate_direction(direction)

    def validate_price_limit_event(self, event: Mapping[str, Any]) -> None:
        market = event.get("market")
        code = event.get("code")
        direction = event.get("direction")
        if type(market) is not str:
            raise InvalidFieldValueError(f"market 必须为字符串：{market!r}")
        if type(code) is not str:
            raise InvalidFieldValueError(f"code 必须为字符串：{code!r}")
        if type(direction) is not str:
            raise InvalidFieldValueError(f"direction 必须为字符串：{direction!r}")

        self.validate_event_identity(market, code, direction)

        limit_rate_bp = event.get("limit_rate_bp")
        if type(limit_rate_bp) is not int or isinstance(limit_rate_bp, bool):
            raise InvalidFieldValueError(f"limit_rate_bp 必须为整数：{limit_rate_bp!r}")
        if limit_rate_bp not in VALID_LIMIT_RATE_BP:
            raise InvalidFieldValueError(
                f"limit_rate_bp 必须为 1000、2000 或 3000：{limit_rate_bp!r}"
            )

        streak_height = event.get("streak_height")
        if type(streak_height) is not int or isinstance(streak_height, bool):
            raise InvalidFieldValueError(f"streak_height 必须为整数：{streak_height!r}")
        if streak_height < 0:
            raise InvalidFieldValueError(f"streak_height 不能为负数：{streak_height!r}")

    def _validate_market(self, market: str) -> None:
        if market not in VALID_MARKETS:
            raise InvalidFieldValueError(
                f"market 必须为小写 sh、sz 或 bj：{market!r}"
            )

    def _validate_code(self, code: str) -> None:
        if not _CODE_PATTERN.fullmatch(code):
            raise InvalidFieldValueError(f"code 必须为 6 位数字字符串：{code!r}")

    def _validate_direction(self, direction: str) -> None:
        if direction not in VALID_DIRECTIONS:
            raise InvalidFieldValueError(
                f"direction 必须为 up 或 down：{direction!r}"
            )


def default_write_guard() -> WriteGuard:
    global _default_guard
    if _default_guard is None:
        _default_guard = WriteGuard()
    return _default_guard
