"""Structural checks for daily market review persistence."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any

from .errors import InvalidFieldValueError
from .schema import ATOMIC_FIELD_NAMES

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INT_FIELDS = frozenset(
    {
        "pullback_count",
        "advancing_count",
        "declining_count",
    }
)
_FLOAT_FIELDS = frozenset(
    {
        "median_change_pct",
        "margin_balance_sh",
        "margin_balance_sz",
        "margin_balance_bj",
        "sh_index_close",
        "sh_index_prev_close",
        "sz_index_close",
        "sz_index_prev_close",
        "cy_index_close",
        "cy_index_prev_close",
        "turnover_amount_sh",
        "turnover_amount_sz",
        "turnover_amount_cy",
        "turnover_amount_bj",
        "total_market_cap",
        "float_market_cap",
        "pe_sh",
        "pe_sz",
        "pe_cy",
        "pe_all",
        "avg_stock_price",
    }
)


def normalize_trade_date(value: str | date) -> str:
    if isinstance(value, datetime):
        raise InvalidFieldValueError(f"trade_date 必须为日期或 YYYY-MM-DD 字符串：{value!r}")
    if isinstance(value, date):
        return value.isoformat()
    if type(value) is not str:
        raise InvalidFieldValueError(f"trade_date 必须为日期或 YYYY-MM-DD 字符串：{value!r}")
    if not _DATE_PATTERN.fullmatch(value):
        raise InvalidFieldValueError(f"trade_date 必须为 YYYY-MM-DD：{value!r}")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise InvalidFieldValueError(f"trade_date 无效：{value!r}") from exc


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_atomic_field(key: str, value: Any) -> Any:
    if key not in ATOMIC_FIELD_NAMES:
        raise InvalidFieldValueError(f"未知字段：{key}")
    if value is None:
        return None
    if key in _INT_FIELDS:
        if type(value) is not int:
            raise InvalidFieldValueError(f"{key} 必须为整数")
        return value
    if key in _FLOAT_FIELDS:
        if not _is_finite_number(value):
            raise InvalidFieldValueError(f"{key} 必须为有限数值")
        return float(value)
    raise InvalidFieldValueError(f"未知字段：{key}")
