"""Read-side statistics for daily market review display."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from typing import Any, Mapping, Sequence

from .schema import DailyMarketReviewAtoms, PriceLimitEventRecord


def _effective_limit_up(events: Sequence[PriceLimitEventRecord]) -> list[PriceLimitEventRecord]:
    return [
        event
        for event in events
        if event.direction == "up" and event.closed_at_limit
    ]


def _index_change(close: float | None, prev_close: float | None) -> dict[str, float | None]:
    if close is None or prev_close is None:
        return {"change_points": None, "change_pct": None}
    change_points = round(close - prev_close, 2)
    if prev_close == 0:
        return {"change_points": change_points, "change_pct": None}
    change_pct = round(change_points / prev_close * 100, 2)
    return {"change_points": change_points, "change_pct": change_pct}


def _limit_up_down_ratio(effective_up: int, closed_down: int) -> dict[str, Any] | None:
    if effective_up <= 0 and closed_down <= 0:
        return None
    if effective_up <= 0 or closed_down <= 0:
        left = effective_up if effective_up >= closed_down else closed_down
        right = 1
        display = f"{left}:1" if effective_up >= closed_down else f"1:{left}"
        return {
            "effective_limit_up": effective_up,
            "closed_limit_down": closed_down,
            "display": display,
        }
    if effective_up >= closed_down:
        ratio = round(effective_up / closed_down, 2)
        display = f"{ratio}:1"
    else:
        ratio = round(closed_down / effective_up, 2)
        display = f"1:{ratio}"
    return {
        "effective_limit_up": effective_up,
        "closed_limit_down": closed_down,
        "display": display,
    }


def _sum_present(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return float(sum(present))


def compute_summary(
    review: DailyMarketReviewAtoms | None,
    events: Sequence[PriceLimitEventRecord],
) -> dict[str, Any]:
    effective_up = _effective_limit_up(events)
    limit_up_20 = [
        event for event in effective_up if event.limit_rate_bp == 2000
    ]
    limit_up_broken = [
        event for event in events
        if event.direction == "up" and not event.closed_at_limit
    ]
    down_opened = [
        event for event in events
        if event.direction == "down" and not event.closed_at_limit
    ]
    down_closed = [
        event for event in events
        if event.direction == "down" and event.closed_at_limit
    ]

    first_board = [event for event in effective_up if event.streak_height == 1]
    streak_board = [event for event in effective_up if event.streak_height >= 2]
    heights = [event.streak_height for event in effective_up]
    highest_board = max(heights) if heights else 0
    highest_board_reps = [
        {"market": event.market, "code": event.code, "name": event.name}
        for event in effective_up
        if event.streak_height == highest_board and highest_board > 0
    ]
    streak_by_height = dict(sorted(Counter(heights).items()))

    up_total = len(effective_up) + len(limit_up_broken)
    broken_rate = (
        round(len(limit_up_broken) / up_total * 100, 2)
        if up_total > 0
        else None
    )
    streak_rate = (
        round(len(streak_board) / len(effective_up) * 100, 2)
        if effective_up
        else None
    )

    review_payload = asdict(review) if review is not None else {}
    margin_total = _sum_present(
        [
            review_payload.get("margin_balance_sh"),
            review_payload.get("margin_balance_sz"),
            review_payload.get("margin_balance_bj"),
        ]
    )
    turnover_total = _sum_present(
        [
            review_payload.get("turnover_amount_sh"),
            review_payload.get("turnover_amount_sz"),
            review_payload.get("turnover_amount_bj"),
        ]
    )

    return {
        "effective_limit_up_count": len(effective_up),
        "limit_up_20pct_count": len(limit_up_20),
        "limit_up_broken_count": len(limit_up_broken),
        "down_opened_count": len(down_opened),
        "down_closed_count": len(down_closed),
        "first_board_count": len(first_board),
        "streak_board_count": len(streak_board),
        "highest_board": highest_board,
        "highest_board_representatives": highest_board_reps,
        "streak_by_height": streak_by_height,
        "broken_rate_pct": broken_rate,
        "streak_rate_pct": streak_rate,
        "limit_up_down_ratio": _limit_up_down_ratio(len(effective_up), len(down_closed)),
        "margin_balance_total": margin_total,
        "turnover_amount_total": turnover_total,
        "sh_index": _index_change(
            review_payload.get("sh_index_close"),
            review_payload.get("sh_index_prev_close"),
        ),
        "sz_index": _index_change(
            review_payload.get("sz_index_close"),
            review_payload.get("sz_index_prev_close"),
        ),
        "cy_index": _index_change(
            review_payload.get("cy_index_close"),
            review_payload.get("cy_index_prev_close"),
        ),
    }


def review_to_dict(review: DailyMarketReviewAtoms | None) -> Mapping[str, Any] | None:
    if review is None:
        return None
    return asdict(review)


def events_to_dict(events: Sequence[PriceLimitEventRecord]) -> list[dict[str, Any]]:
    return [asdict(event) for event in events]
