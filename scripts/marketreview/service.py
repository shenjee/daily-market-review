"""Small query helpers for daily market review persistence."""

from __future__ import annotations

from datetime import date
from typing import Mapping

from .repository import MarketReviewRepository
from .schema import ATOMIC_FIELD_NAMES
from .validation import normalize_trade_date

TRACKED_MISSING_FIELDS: tuple[str, ...] = tuple(sorted(ATOMIC_FIELD_NAMES))


def missing_atomic_fields(
    repository: MarketReviewRepository,
    trade_date: str | date,
    *,
    field_names: Mapping[str, str] | None = None,
) -> list[str]:
    normalize_trade_date(trade_date)
    review = repository.get_review(trade_date)
    names = field_names or {name: name for name in TRACKED_MISSING_FIELDS}
    missing: list[str] = []
    for field_name in names:
        if review is None or getattr(review, field_name) is None:
            missing.append(field_name)
    return missing
