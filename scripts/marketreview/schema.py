"""Public shapes for daily market review persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

ATOMIC_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "pullback_count",
        "median_change_pct",
        "advancing_count",
        "declining_count",
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

PRICE_LIMIT_EVENT_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "market",
        "code",
        "name",
        "direction",
        "closed_at_limit",
        "limit_rate_bp",
        "streak_height",
    }
)
PRICE_LIMIT_EVENT_IGNORED_FIELDS: frozenset[str] = frozenset(
    {"trade_date", "created_at", "updated_at"}
)
REVIEW_SELECT_COLUMNS: tuple[str, ...] = (
    "trade_date",
    *sorted(ATOMIC_FIELD_NAMES),
)


@dataclass(frozen=True)
class PriceLimitEventInput:
    market: str
    code: str
    name: str
    direction: str
    closed_at_limit: bool
    limit_rate_bp: int
    streak_height: int


@dataclass(frozen=True)
class PriceLimitEventRecord:
    trade_date: str
    market: str
    code: str
    name: str
    direction: str
    closed_at_limit: bool
    limit_rate_bp: int
    streak_height: int


@dataclass
class DailyMarketReviewAtoms:
    trade_date: str
    pullback_count: int | None = None
    median_change_pct: float | None = None
    advancing_count: int | None = None
    declining_count: int | None = None
    margin_balance_sh: float | None = None
    margin_balance_sz: float | None = None
    margin_balance_bj: float | None = None
    sh_index_close: float | None = None
    sh_index_prev_close: float | None = None
    sz_index_close: float | None = None
    sz_index_prev_close: float | None = None
    cy_index_close: float | None = None
    cy_index_prev_close: float | None = None
    turnover_amount_sh: float | None = None
    turnover_amount_sz: float | None = None
    turnover_amount_cy: float | None = None
    turnover_amount_bj: float | None = None
    total_market_cap: float | None = None
    float_market_cap: float | None = None
    pe_sh: float | None = None
    pe_sz: float | None = None
    pe_cy: float | None = None
    pe_all: float | None = None
    avg_stock_price: float | None = None


PriceLimitEventLike = PriceLimitEventInput | PriceLimitEventRecord | Mapping[str, Any]
