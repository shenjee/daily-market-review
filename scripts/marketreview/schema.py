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
PRICE_LIMIT_EVENT_DETAIL_IDENTITY_FIELD_NAMES: frozenset[str] = frozenset(
    {"market", "code", "direction"}
)
PRICE_LIMIT_EVENT_DETAIL_SCALAR_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "previous_turnover_amount",
        "auction_amount",
        "previous_close",
        "open_price",
        "turnover_amount",
        "turnover_rate",
        "is_leader",
        "note",
    }
)
PRICE_LIMIT_EVENT_DETAIL_SCALAR_FIELD_ORDER: tuple[str, ...] = (
    "previous_turnover_amount",
    "auction_amount",
    "previous_close",
    "open_price",
    "turnover_amount",
    "turnover_rate",
    "is_leader",
    "note",
)
PRICE_LIMIT_EVENT_DETAIL_LIST_FIELD_NAMES: frozenset[str] = frozenset(
    {"sectors", "limit_up_reasons"}
)
PRICE_LIMIT_EVENT_DETAIL_DERIVED_FIELD_NAMES: frozenset[str] = frozenset(
    {"auction_ratio", "open_change"}
)
PRICE_LIMIT_EVENT_DETAIL_IGNORED_FIELDS: frozenset[str] = frozenset(
    {"trade_date", "created_at", "updated_at"}
)
REVIEW_SELECT_COLUMNS: tuple[str, ...] = (
    "trade_date",
    *sorted(ATOMIC_FIELD_NAMES),
)
DETAIL_SELECT_COLUMNS: tuple[str, ...] = (
    "trade_date",
    "market",
    "code",
    "direction",
    "previous_turnover_amount",
    "auction_amount",
    "previous_close",
    "open_price",
    "turnover_amount",
    "turnover_rate",
    "is_leader",
    "note",
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


@dataclass(frozen=True)
class PriceLimitEventDetailPatch:
    market: str
    code: str
    direction: str
    provided_fields: frozenset[str]
    previous_turnover_amount: float | None = None
    auction_amount: float | None = None
    previous_close: float | None = None
    open_price: float | None = None
    turnover_amount: float | None = None
    turnover_rate: float | None = None
    is_leader: bool | None = None
    note: str | None = None
    sectors: tuple[str, ...] | None = None
    limit_up_reasons: tuple[str, ...] | None = None

    def has(self, field_name: str) -> bool:
        return field_name in self.provided_fields


@dataclass(frozen=True)
class PriceLimitEventDetailRecord:
    trade_date: str
    market: str
    code: str
    direction: str
    previous_turnover_amount: float | None
    auction_amount: float | None
    previous_close: float | None
    open_price: float | None
    turnover_amount: float | None
    turnover_rate: float | None
    is_leader: bool | None
    note: str | None
    sectors: list[str]
    limit_up_reasons: list[str]


@dataclass(frozen=True)
class LadderStock:
    market: str
    code: str
    name: str
    direction: str
    closed_at_limit: bool
    limit_rate_bp: int
    streak_height: int
    sectors: list[str]
    limit_up_reasons: list[str]
    previous_turnover_amount: float | None
    auction_amount: float | None
    previous_close: float | None
    open_price: float | None
    turnover_amount: float | None
    turnover_rate: float | None
    is_leader: bool | None
    note: str | None
    auction_ratio: float | None
    open_change: float | None


@dataclass(frozen=True)
class LadderGroup:
    streak_height: int
    count: int
    stocks: list[LadderStock]


@dataclass(frozen=True)
class LadderView:
    groups: list[LadderGroup]
    broken_limit_up: list[LadderStock]
    opened_limit_down: list[LadderStock]
    closed_limit_down: list[LadderStock]


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
PriceLimitEventDetailLike = PriceLimitEventDetailPatch | Mapping[str, Any]
