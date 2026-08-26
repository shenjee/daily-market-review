"""SQLite repository for daily market review."""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Mapping, Sequence

from .db import review_transaction
from .errors import InvalidFieldValueError
from .schema import (
    ATOMIC_FIELD_NAMES,
    PRICE_LIMIT_EVENT_FIELD_NAMES,
    PRICE_LIMIT_EVENT_IGNORED_FIELDS,
    REVIEW_SELECT_COLUMNS,
    DailyMarketReviewAtoms,
    PriceLimitEventInput,
    PriceLimitEventLike,
    PriceLimitEventRecord,
)
from .sqlite_schema import PathLike, connect, init_db, utc_now_iso
from .validation import normalize_trade_date, validate_atomic_field


def _row_to_atoms(row: sqlite3.Row) -> DailyMarketReviewAtoms:
    payload = {key: row[key] for key in row.keys() if key not in {"created_at", "updated_at"}}
    return DailyMarketReviewAtoms(**payload)


def _row_to_event(row: sqlite3.Row) -> PriceLimitEventRecord:
    return PriceLimitEventRecord(
        trade_date=row["trade_date"],
        market=row["market"],
        code=row["code"],
        name=row["name"],
        direction=row["direction"],
        closed_at_limit=bool(row["closed_at_limit"]),
        limit_rate_bp=row["limit_rate_bp"],
        streak_height=row["streak_height"],
    )


def _require_str(field_name: str, value: Any) -> str:
    if type(value) is not str:
        raise InvalidFieldValueError(f"{field_name} 必须为字符串：{value!r}")
    return value


def _require_int(field_name: str, value: Any) -> int:
    if type(value) is not int:
        raise InvalidFieldValueError(f"{field_name} 必须为整数：{value!r}")
    return value


def _require_bool(field_name: str, value: Any) -> bool:
    if type(value) is not bool:
        raise InvalidFieldValueError(f"{field_name} 必须为布尔值：{value!r}")
    return value


def _event_mapping(event: PriceLimitEventLike) -> dict[str, Any]:
    if isinstance(event, (PriceLimitEventInput, PriceLimitEventRecord)):
        return {name: getattr(event, name) for name in PRICE_LIMIT_EVENT_FIELD_NAMES}
    if isinstance(event, Mapping):
        extra = set(event) - PRICE_LIMIT_EVENT_FIELD_NAMES - PRICE_LIMIT_EVENT_IGNORED_FIELDS
        if extra:
            raise InvalidFieldValueError(f"未知字段：{', '.join(sorted(extra))}")
        missing = PRICE_LIMIT_EVENT_FIELD_NAMES - set(event)
        if missing:
            raise InvalidFieldValueError(f"缺少字段：{', '.join(sorted(missing))}")
        return {name: event[name] for name in PRICE_LIMIT_EVENT_FIELD_NAMES}
    raise InvalidFieldValueError(
        "事件必须为映射、PriceLimitEventInput 或 PriceLimitEventRecord："
        f"{event!r}"
    )


def _reject_duplicate_event_identities(records: Sequence[PriceLimitEventRecord]) -> None:
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        key = (record.market, record.code, record.direction)
        if key in seen:
            raise InvalidFieldValueError(
                "同一批写入存在重复事件："
                f"{record.market}.{record.code} {record.direction}"
            )
        seen.add(key)


def _normalize_event(trade_date: str, event: PriceLimitEventLike) -> PriceLimitEventRecord:
    payload = _event_mapping(event)
    return PriceLimitEventRecord(
        trade_date=trade_date,
        market=_require_str("market", payload["market"]),
        code=_require_str("code", payload["code"]),
        name=_require_str("name", payload["name"]),
        direction=_require_str("direction", payload["direction"]),
        closed_at_limit=_require_bool("closed_at_limit", payload["closed_at_limit"]),
        limit_rate_bp=_require_int("limit_rate_bp", payload["limit_rate_bp"]),
        streak_height=_require_int("streak_height", payload["streak_height"]),
    )


class MarketReviewRepository:
    def __init__(self, db_path: PathLike) -> None:
        self._owns_connection = not isinstance(db_path, sqlite3.Connection)
        self._conn = connect(db_path) if self._owns_connection else db_path
        init_db(self._conn)

    def close(self) -> None:
        if self._owns_connection:
            self._conn.close()

    def __enter__(self) -> "MarketReviewRepository":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def save_review(self, trade_date: str | date, fields: Mapping[str, Any] | None = None) -> None:
        normalized_date = normalize_trade_date(trade_date)
        payload = dict(fields or {})
        if not payload:
            return
        unknown = set(payload) - ATOMIC_FIELD_NAMES
        if unknown:
            raise InvalidFieldValueError(f"未知字段：{', '.join(sorted(unknown))}")
        normalized_fields = {
            key: validate_atomic_field(key, value) for key, value in payload.items()
        }
        with review_transaction(self._conn):
            self._ensure_review_row(normalized_date)
            self._apply_field_patch(normalized_date, normalized_fields)
            self._touch_review(normalized_date)

    def get_review(self, trade_date: str | date) -> DailyMarketReviewAtoms | None:
        normalized_date = normalize_trade_date(trade_date)
        columns = ", ".join(REVIEW_SELECT_COLUMNS)
        row = self._conn.execute(
            f"SELECT {columns} FROM daily_market_review WHERE trade_date = ?",
            (normalized_date,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_atoms(row)

    def list_reviews(
        self,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
    ) -> list[DailyMarketReviewAtoms]:
        dates = self.list_trade_dates(start_date, end_date)
        reviews: list[DailyMarketReviewAtoms] = []
        for trade_date in dates:
            review = self.get_review(trade_date)
            if review is not None:
                reviews.append(review)
        return reviews

    def delete_review(self, trade_date: str | date) -> None:
        normalized_date = normalize_trade_date(trade_date)
        with review_transaction(self._conn):
            self._conn.execute(
                "DELETE FROM daily_market_review WHERE trade_date = ?",
                (normalized_date,),
            )

    def list_trade_dates(
        self,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
    ) -> list[str]:
        query = "SELECT trade_date FROM daily_market_review"
        params: list[str] = []
        clauses: list[str] = []
        if start_date is not None:
            clauses.append("trade_date >= ?")
            params.append(normalize_trade_date(start_date))
        if end_date is not None:
            clauses.append("trade_date <= ?")
            params.append(normalize_trade_date(end_date))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY trade_date"
        rows = self._conn.execute(query, params).fetchall()
        return [row["trade_date"] for row in rows]

    def save_price_limit_events(
        self,
        trade_date: str | date,
        events: Sequence[PriceLimitEventLike],
    ) -> None:
        normalized_date = normalize_trade_date(trade_date)
        if not events:
            return
        records = [_normalize_event(normalized_date, event) for event in events]
        _reject_duplicate_event_identities(records)
        now = utc_now_iso()
        with review_transaction(self._conn):
            for record in records:
                self._upsert_event(record, now=now)

    def get_price_limit_events(self, trade_date: str | date) -> list[PriceLimitEventRecord]:
        return self.list_price_limit_events(start_date=trade_date, end_date=trade_date)

    def list_price_limit_events(
        self,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
    ) -> list[PriceLimitEventRecord]:
        query = """
            SELECT trade_date, market, code, name, direction,
                   closed_at_limit, limit_rate_bp, streak_height
            FROM daily_price_limit_event
        """
        params: list[str] = []
        clauses: list[str] = []
        if start_date is not None:
            clauses.append("trade_date >= ?")
            params.append(normalize_trade_date(start_date))
        if end_date is not None:
            clauses.append("trade_date <= ?")
            params.append(normalize_trade_date(end_date))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY trade_date, market, code, direction"
        rows = self._conn.execute(query, params).fetchall()
        return [_row_to_event(row) for row in rows]

    def delete_price_limit_events(self, trade_date: str | date) -> None:
        normalized_date = normalize_trade_date(trade_date)
        with review_transaction(self._conn):
            self._conn.execute(
                "DELETE FROM daily_price_limit_event WHERE trade_date = ?",
                (normalized_date,),
            )

    def delete_price_limit_event(
        self,
        trade_date: str | date,
        market: str,
        code: str,
        direction: str,
    ) -> None:
        normalized_date = normalize_trade_date(trade_date)
        with review_transaction(self._conn):
            self._delete_event_row(
                normalized_date,
                _require_str("market", market),
                _require_str("code", code),
                _require_str("direction", direction),
            )

    def replace_price_limit_event_direction(
        self,
        trade_date: str | date,
        market: str,
        code: str,
        old_direction: str,
        event: PriceLimitEventLike,
    ) -> None:
        normalized_date = normalize_trade_date(trade_date)
        market_value = _require_str("market", market)
        code_value = _require_str("code", code)
        old_direction_value = _require_str("old_direction", old_direction)
        record = _normalize_event(normalized_date, event)
        if record.market != market_value or record.code != code_value:
            raise InvalidFieldValueError(
                "替换事件的 market/code 必须与删除目标一致："
                f"{market_value}.{code_value}"
            )
        if record.direction == old_direction_value:
            raise InvalidFieldValueError(
                "方向未变化时请使用 save_price_limit_events，不要调用方向替换"
            )
        now = utc_now_iso()
        with review_transaction(self._conn):
            self._delete_event_row(
                normalized_date,
                market_value,
                code_value,
                old_direction_value,
            )
            self._upsert_event(record, now=now)

    def _delete_event_row(
        self,
        trade_date: str,
        market: str,
        code: str,
        direction: str,
    ) -> None:
        self._conn.execute(
            """
            DELETE FROM daily_price_limit_event
            WHERE trade_date = ? AND market = ? AND code = ? AND direction = ?
            """,
            (trade_date, market, code, direction),
        )

    def _ensure_review_row(self, trade_date: str) -> None:
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO daily_market_review (trade_date, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(trade_date) DO NOTHING
            """,
            (trade_date, now, now),
        )

    def _apply_field_patch(self, trade_date: str, fields: Mapping[str, Any]) -> None:
        assignments = [f"{key} = ?" for key in fields]
        values: list[Any] = list(fields.values())
        values.append(trade_date)
        self._conn.execute(
            f"UPDATE daily_market_review SET {', '.join(assignments)} WHERE trade_date = ?",
            values,
        )

    def _touch_review(self, trade_date: str) -> None:
        self._conn.execute(
            "UPDATE daily_market_review SET updated_at = ? WHERE trade_date = ?",
            (utc_now_iso(), trade_date),
        )

    def _upsert_event(self, record: PriceLimitEventRecord, *, now: str) -> None:
        self._conn.execute(
            """
            INSERT INTO daily_price_limit_event (
                trade_date, market, code, name, direction,
                closed_at_limit, limit_rate_bp, streak_height,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, market, code, direction) DO UPDATE SET
                name = excluded.name,
                closed_at_limit = excluded.closed_at_limit,
                limit_rate_bp = excluded.limit_rate_bp,
                streak_height = excluded.streak_height,
                updated_at = excluded.updated_at
            """,
            (
                record.trade_date,
                record.market,
                record.code,
                record.name,
                record.direction,
                int(record.closed_at_limit),
                record.limit_rate_bp,
                record.streak_height,
                now,
                now,
            ),
        )
