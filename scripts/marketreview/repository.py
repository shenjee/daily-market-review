"""SQLite repository for daily market review."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date
from typing import Any, Mapping, Sequence

from .db import review_transaction
from .errors import InvalidFieldValueError
from .schema import (
    ATOMIC_FIELD_NAMES,
    DETAIL_SELECT_COLUMNS,
    PRICE_LIMIT_EVENT_DETAIL_LIST_FIELD_NAMES,
    PRICE_LIMIT_EVENT_DETAIL_SCALAR_FIELD_NAMES,
    PRICE_LIMIT_EVENT_DETAIL_SCALAR_FIELD_ORDER,
    PRICE_LIMIT_EVENT_FIELD_NAMES,
    PRICE_LIMIT_EVENT_IGNORED_FIELDS,
    REVIEW_SELECT_COLUMNS,
    DailyMarketReviewAtoms,
    PriceLimitEventDetailLike,
    PriceLimitEventDetailPatch,
    PriceLimitEventDetailRecord,
    PriceLimitEventInput,
    PriceLimitEventLike,
    PriceLimitEventRecord,
)
from .sqlite_schema import PathLike, connect, init_db, utc_now_iso
from .validation import (
    normalize_string_list,
    normalize_trade_date,
    validate_atomic_field,
    validate_event_detail_payload,
    validate_event_detail_scalar,
)


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


def _sql_to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _bool_to_sql(value: bool | None) -> int | None:
    if value is None:
        return None
    return int(value)


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


def _reject_duplicate_detail_identities(patches: Sequence[PriceLimitEventDetailPatch]) -> None:
    seen: set[tuple[str, str, str]] = set()
    for patch in patches:
        key = (patch.market, patch.code, patch.direction)
        if key in seen:
            raise InvalidFieldValueError(
                "同一批写入存在重复事件扩展："
                f"{patch.market}.{patch.code} {patch.direction}"
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


def _detail_patch_as_mapping(detail: PriceLimitEventDetailPatch) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "market": detail.market,
        "code": detail.code,
        "direction": detail.direction,
    }
    for name in detail.provided_fields:
        value = getattr(detail, name, None)
        if name in PRICE_LIMIT_EVENT_DETAIL_LIST_FIELD_NAMES and type(value) is tuple:
            value = list(value)
        payload[name] = value
    return payload


def _normalize_detail_patch(detail: PriceLimitEventDetailLike) -> PriceLimitEventDetailPatch:
    if isinstance(detail, PriceLimitEventDetailPatch):
        return _normalize_detail_patch(_detail_patch_as_mapping(detail))
    if isinstance(detail, Mapping):
        validate_event_detail_payload(detail)
        provided = frozenset(
            name
            for name in (
                PRICE_LIMIT_EVENT_DETAIL_SCALAR_FIELD_NAMES
                | PRICE_LIMIT_EVENT_DETAIL_LIST_FIELD_NAMES
            )
            if name in detail
        )
        payload: dict[str, Any] = {
            "market": _require_str("market", detail["market"]),
            "code": _require_str("code", detail["code"]),
            "direction": _require_str("direction", detail["direction"]),
            "provided_fields": provided,
        }
        for name in PRICE_LIMIT_EVENT_DETAIL_SCALAR_FIELD_ORDER:
            if name in provided:
                payload[name] = validate_event_detail_scalar(name, detail[name])
        for name in PRICE_LIMIT_EVENT_DETAIL_LIST_FIELD_NAMES:
            if name in provided:
                payload[name] = tuple(normalize_string_list(name, detail[name]))
        return PriceLimitEventDetailPatch(**payload)
    raise InvalidFieldValueError(
        "事件扩展必须为映射或 PriceLimitEventDetailPatch："
        f"{detail!r}"
    )


def _trade_date_where(
    start_date: str | date | None,
    end_date: str | date | None,
) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    if start_date is not None:
        clauses.append("trade_date >= ?")
        params.append(normalize_trade_date(start_date))
    if end_date is not None:
        clauses.append("trade_date <= ?")
        params.append(normalize_trade_date(end_date))
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


def _identity_key(row: sqlite3.Row) -> tuple[str, str, str, str]:
    return (row["trade_date"], row["market"], row["code"], row["direction"])


def _empty_detail_record(
    trade_date: str,
    market: str,
    code: str,
    direction: str,
) -> PriceLimitEventDetailRecord:
    return PriceLimitEventDetailRecord(
        trade_date=trade_date,
        market=market,
        code=code,
        direction=direction,
        previous_turnover_amount=None,
        auction_amount=None,
        previous_close=None,
        open_price=None,
        turnover_amount=None,
        turnover_rate=None,
        is_leader=None,
        note=None,
        sectors=[],
        limit_up_reasons=[],
    )


def _detail_record_from_row(row: sqlite3.Row) -> PriceLimitEventDetailRecord:
    return PriceLimitEventDetailRecord(
        trade_date=row["trade_date"],
        market=row["market"],
        code=row["code"],
        direction=row["direction"],
        previous_turnover_amount=row["previous_turnover_amount"],
        auction_amount=row["auction_amount"],
        previous_close=row["previous_close"],
        open_price=row["open_price"],
        turnover_amount=row["turnover_amount"],
        turnover_rate=row["turnover_rate"],
        is_leader=_sql_to_bool(row["is_leader"]),
        note=row["note"],
        sectors=[],
        limit_up_reasons=[],
    )


def _assemble_detail_records(
    detail_rows: Sequence[sqlite3.Row],
    sector_rows: Sequence[sqlite3.Row],
    reason_rows: Sequence[sqlite3.Row],
) -> list[PriceLimitEventDetailRecord]:
    records: dict[tuple[str, str, str, str], PriceLimitEventDetailRecord] = {}
    for row in detail_rows:
        records[_identity_key(row)] = _detail_record_from_row(row)
    for row in sector_rows:
        key = _identity_key(row)
        record = records.get(key) or _empty_detail_record(*key)
        records[key] = replace(record, sectors=[*record.sectors, row["value"]])
    for row in reason_rows:
        key = _identity_key(row)
        record = records.get(key) or _empty_detail_record(*key)
        records[key] = replace(record, limit_up_reasons=[*record.limit_up_reasons, row["value"]])
    return [records[key] for key in sorted(records)]


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

    def save_price_limit_event_details(
        self,
        trade_date: str | date,
        details: Sequence[PriceLimitEventDetailLike],
    ) -> None:
        normalized_date = normalize_trade_date(trade_date)
        if not details:
            return
        patches = [_normalize_detail_patch(detail) for detail in details]
        _reject_duplicate_detail_identities(patches)
        now = utc_now_iso()
        with review_transaction(self._conn):
            for patch in patches:
                self._apply_detail_patch(normalized_date, patch, now=now)

    def get_price_limit_event_details(
        self,
        trade_date: str | date,
    ) -> list[PriceLimitEventDetailRecord]:
        return self.list_price_limit_event_details(start_date=trade_date, end_date=trade_date)

    def list_price_limit_event_details(
        self,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
    ) -> list[PriceLimitEventDetailRecord]:
        where, params = _trade_date_where(start_date, end_date)
        columns = ", ".join(DETAIL_SELECT_COLUMNS)
        detail_rows = self._conn.execute(
            f"SELECT {columns} FROM daily_price_limit_event_detail{where}",
            params,
        ).fetchall()
        sector_rows = self._conn.execute(
            f"""
            SELECT trade_date, market, code, direction, value
            FROM daily_price_limit_event_sector
            {where}
            ORDER BY trade_date, market, code, direction, position
            """,
            params,
        ).fetchall()
        reason_rows = self._conn.execute(
            f"""
            SELECT trade_date, market, code, direction, value
            FROM daily_price_limit_event_reason
            {where}
            ORDER BY trade_date, market, code, direction, position
            """,
            params,
        ).fetchall()
        return _assemble_detail_records(detail_rows, sector_rows, reason_rows)

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
            if not self._event_exists(
                normalized_date,
                market_value,
                code_value,
                old_direction_value,
            ):
                raise InvalidFieldValueError(
                    f"被替换事件不存在：{market_value}.{code_value} {old_direction_value}"
                )
            if self._event_exists(
                normalized_date,
                record.market,
                record.code,
                record.direction,
            ):
                raise InvalidFieldValueError(
                    f"目标方向已存在：{record.market}.{record.code} {record.direction}"
                )
            extension = self._load_event_extension(
                normalized_date,
                market_value,
                code_value,
                old_direction_value,
            )
            self._delete_event_row(
                normalized_date,
                market_value,
                code_value,
                old_direction_value,
            )
            self._upsert_event(record, now=now)
            if extension is not None:
                self._restore_event_extension(
                    normalized_date,
                    record.market,
                    record.code,
                    record.direction,
                    extension,
                    include_reasons=record.direction == "up",
                    now=now,
                )

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

    def _event_exists(self, trade_date: str, market: str, code: str, direction: str) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 FROM daily_price_limit_event
            WHERE trade_date = ? AND market = ? AND code = ? AND direction = ?
            """,
            (trade_date, market, code, direction),
        ).fetchone()
        return row is not None

    def _apply_detail_patch(
        self,
        trade_date: str,
        patch: PriceLimitEventDetailPatch,
        *,
        now: str,
    ) -> None:
        if not self._event_exists(trade_date, patch.market, patch.code, patch.direction):
            raise InvalidFieldValueError(
                f"父事件不存在：{patch.market}.{patch.code} {patch.direction}"
            )
        scalar_names = [
            name for name in PRICE_LIMIT_EVENT_DETAIL_SCALAR_FIELD_ORDER if patch.has(name)
        ]
        if scalar_names:
            self._ensure_detail_row(trade_date, patch.market, patch.code, patch.direction, now)
            assignments = [f"{name} = ?" for name in scalar_names]
            values: list[Any] = []
            for name in scalar_names:
                value = getattr(patch, name)
                if name == "is_leader":
                    value = _bool_to_sql(value)
                values.append(value)
            values.extend([now, trade_date, patch.market, patch.code, patch.direction])
            self._conn.execute(
                f"""
                UPDATE daily_price_limit_event_detail
                SET {", ".join(assignments)}, updated_at = ?
                WHERE trade_date = ? AND market = ? AND code = ? AND direction = ?
                """,
                values,
            )
        if patch.has("sectors"):
            self._replace_string_list(
                "daily_price_limit_event_sector",
                trade_date,
                patch.market,
                patch.code,
                patch.direction,
                list(patch.sectors or ()),
            )
        if patch.has("limit_up_reasons"):
            self._replace_string_list(
                "daily_price_limit_event_reason",
                trade_date,
                patch.market,
                patch.code,
                patch.direction,
                list(patch.limit_up_reasons or ()),
            )

    def _ensure_detail_row(
        self,
        trade_date: str,
        market: str,
        code: str,
        direction: str,
        now: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO daily_price_limit_event_detail (
                trade_date, market, code, direction, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, market, code, direction) DO NOTHING
            """,
            (trade_date, market, code, direction, now, now),
        )

    def _replace_string_list(
        self,
        table: str,
        trade_date: str,
        market: str,
        code: str,
        direction: str,
        values: Sequence[str],
    ) -> None:
        if table not in {
            "daily_price_limit_event_sector",
            "daily_price_limit_event_reason",
        }:
            raise InvalidFieldValueError(f"未知多值表：{table}")
        self._conn.execute(
            f"""
            DELETE FROM {table}
            WHERE trade_date = ? AND market = ? AND code = ? AND direction = ?
            """,
            (trade_date, market, code, direction),
        )
        for position, value in enumerate(values):
            self._conn.execute(
                f"""
                INSERT INTO {table} (
                    trade_date, market, code, direction, position, value
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (trade_date, market, code, direction, position, value),
            )

    def _load_event_extension(
        self,
        trade_date: str,
        market: str,
        code: str,
        direction: str,
    ) -> tuple[PriceLimitEventDetailRecord, bool] | None:
        columns = ", ".join(DETAIL_SELECT_COLUMNS)
        row = self._conn.execute(
            f"""
            SELECT {columns} FROM daily_price_limit_event_detail
            WHERE trade_date = ? AND market = ? AND code = ? AND direction = ?
            """,
            (trade_date, market, code, direction),
        ).fetchone()
        sectors = self._list_string_values(
            "daily_price_limit_event_sector",
            trade_date,
            market,
            code,
            direction,
        )
        reasons = self._list_string_values(
            "daily_price_limit_event_reason",
            trade_date,
            market,
            code,
            direction,
        )
        if row is None and not sectors and not reasons:
            return None
        record = (
            _detail_record_from_row(row)
            if row is not None
            else _empty_detail_record(trade_date, market, code, direction)
        )
        record = replace(record, sectors=sectors, limit_up_reasons=reasons)
        return record, row is not None

    def _list_string_values(
        self,
        table: str,
        trade_date: str,
        market: str,
        code: str,
        direction: str,
    ) -> list[str]:
        if table not in {
            "daily_price_limit_event_sector",
            "daily_price_limit_event_reason",
        }:
            raise InvalidFieldValueError(f"未知多值表：{table}")
        rows = self._conn.execute(
            f"""
            SELECT value FROM {table}
            WHERE trade_date = ? AND market = ? AND code = ? AND direction = ?
            ORDER BY position
            """,
            (trade_date, market, code, direction),
        ).fetchall()
        return [row["value"] for row in rows]

    def _restore_event_extension(
        self,
        trade_date: str,
        market: str,
        code: str,
        direction: str,
        extension: tuple[PriceLimitEventDetailRecord, bool],
        *,
        include_reasons: bool,
        now: str,
    ) -> None:
        record, has_detail_row = extension
        if has_detail_row:
            self._conn.execute(
                """
                INSERT INTO daily_price_limit_event_detail (
                    trade_date, market, code, direction,
                    previous_turnover_amount, auction_amount, previous_close, open_price,
                    turnover_amount, turnover_rate, is_leader, note,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_date,
                    market,
                    code,
                    direction,
                    record.previous_turnover_amount,
                    record.auction_amount,
                    record.previous_close,
                    record.open_price,
                    record.turnover_amount,
                    record.turnover_rate,
                    _bool_to_sql(record.is_leader),
                    record.note,
                    now,
                    now,
                ),
            )
        self._replace_string_list(
            "daily_price_limit_event_sector",
            trade_date,
            market,
            code,
            direction,
            record.sectors,
        )
        if include_reasons:
            self._replace_string_list(
                "daily_price_limit_event_reason",
                trade_date,
                market,
                code,
                direction,
                record.limit_up_reasons,
            )
