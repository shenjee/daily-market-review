"""SQLite schema for daily market review."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Union

from .db import review_transaction
from .errors import DatabaseUnavailableError, MarketReviewError
from .paths import ensure_db_parent_dir

PathLike = Union[str, Path, sqlite3.Connection]

DDL_DAILY_MARKET_REVIEW = """
    CREATE TABLE IF NOT EXISTS daily_market_review (
        trade_date TEXT NOT NULL PRIMARY KEY,
        pullback_count INTEGER,
        median_change_pct REAL,
        advancing_count INTEGER,
        declining_count INTEGER,
        margin_balance_sh REAL,
        margin_balance_sz REAL,
        margin_balance_bj REAL,
        sh_index_close REAL,
        sh_index_prev_close REAL,
        sz_index_close REAL,
        sz_index_prev_close REAL,
        cy_index_close REAL,
        cy_index_prev_close REAL,
        turnover_amount_sh REAL,
        turnover_amount_sz REAL,
        turnover_amount_cy REAL,
        turnover_amount_bj REAL,
        total_market_cap REAL,
        float_market_cap REAL,
        pe_sh REAL,
        pe_sz REAL,
        pe_cy REAL,
        pe_all REAL,
        avg_stock_price REAL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT
"""

DDL_DAILY_PRICE_LIMIT_EVENT = """
    CREATE TABLE IF NOT EXISTS daily_price_limit_event (
        trade_date TEXT NOT NULL,
        market TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT NOT NULL,
        direction TEXT NOT NULL,
        closed_at_limit INTEGER NOT NULL,
        limit_rate_bp INTEGER NOT NULL,
        streak_height INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (trade_date, market, code, direction)
    ) STRICT
"""

DDL_DAILY_PRICE_LIMIT_EVENT_DETAIL = """
    CREATE TABLE IF NOT EXISTS daily_price_limit_event_detail (
        trade_date TEXT NOT NULL,
        market TEXT NOT NULL,
        code TEXT NOT NULL,
        direction TEXT NOT NULL,
        previous_turnover_amount REAL,
        auction_amount REAL,
        previous_close REAL,
        open_price REAL,
        turnover_amount REAL,
        turnover_rate REAL,
        is_leader INTEGER,
        note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (trade_date, market, code, direction),
        FOREIGN KEY (trade_date, market, code, direction)
            REFERENCES daily_price_limit_event (trade_date, market, code, direction)
            ON DELETE CASCADE
    ) STRICT
"""

DDL_DAILY_PRICE_LIMIT_EVENT_SECTOR = """
    CREATE TABLE IF NOT EXISTS daily_price_limit_event_sector (
        trade_date TEXT NOT NULL,
        market TEXT NOT NULL,
        code TEXT NOT NULL,
        direction TEXT NOT NULL,
        position INTEGER NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY (trade_date, market, code, direction, position),
        UNIQUE (trade_date, market, code, direction, value),
        FOREIGN KEY (trade_date, market, code, direction)
            REFERENCES daily_price_limit_event (trade_date, market, code, direction)
            ON DELETE CASCADE
    ) STRICT
"""

DDL_DAILY_PRICE_LIMIT_EVENT_REASON = """
    CREATE TABLE IF NOT EXISTS daily_price_limit_event_reason (
        trade_date TEXT NOT NULL,
        market TEXT NOT NULL,
        code TEXT NOT NULL,
        direction TEXT NOT NULL,
        position INTEGER NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY (trade_date, market, code, direction, position),
        UNIQUE (trade_date, market, code, direction, value),
        FOREIGN KEY (trade_date, market, code, direction)
            REFERENCES daily_price_limit_event (trade_date, market, code, direction)
            ON DELETE CASCADE
    ) STRICT
"""

DDL_STATEMENTS: tuple[str, ...] = (
    DDL_DAILY_MARKET_REVIEW,
    DDL_DAILY_PRICE_LIMIT_EVENT,
    DDL_DAILY_PRICE_LIMIT_EVENT_DETAIL,
    DDL_DAILY_PRICE_LIMIT_EVENT_SECTOR,
    DDL_DAILY_PRICE_LIMIT_EVENT_REASON,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _pragma_value(conn: sqlite3.Connection, name: str) -> Any:
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def _is_memory_database(conn: sqlite3.Connection) -> bool:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return not row[2]
    return False


def _require_journal_mode(conn: sqlite3.Connection) -> None:
    journal_mode = str(_pragma_value(conn, "journal_mode")).lower()
    if _is_memory_database(conn):
        if journal_mode in {"wal", "memory"}:
            return
    elif journal_mode == "wal":
        return
    if conn.in_transaction:
        raise MarketReviewError(
            code="SQLITE_JOURNAL_MODE",
            message=(
                f"无法启用 SQLite WAL：当前 journal_mode={journal_mode}。"
                "连接已在事务中，该 PRAGMA 不会生效。请在开启事务前初始化连接。"
            ),
        )
    raise MarketReviewError(
        code="SQLITE_JOURNAL_MODE",
        message=f"无法启用 SQLite WAL：当前 journal_mode={journal_mode}",
    )


def _require_foreign_keys(conn: sqlite3.Connection) -> None:
    if _pragma_value(conn, "foreign_keys"):
        return
    if conn.in_transaction:
        raise MarketReviewError(
            code="SQLITE_FOREIGN_KEYS",
            message=(
                "无法启用 SQLite foreign_keys：连接已在事务中，"
                "该 PRAGMA 会静默失效。请在开启事务前初始化连接。"
            ),
        )
    raise MarketReviewError(
        code="SQLITE_FOREIGN_KEYS",
        message="无法启用 SQLite foreign_keys",
    )


_ACCESS_ERROR_MARKERS: tuple[str, ...] = (
    "unable to open",
    "readonly",
    "read-only",
    "read only",
    "permission denied",
    "disk i/o",
)


def _is_access_error(exc: BaseException) -> bool:
    if isinstance(exc, OSError):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        lowered = str(exc).lower()
        return any(marker in lowered for marker in _ACCESS_ERROR_MARKERS)
    return False


def _database_unavailable(
    path: Path | str,
    exc: BaseException,
) -> DatabaseUnavailableError | None:
    if _is_access_error(exc):
        return DatabaseUnavailableError(f"{path}: {exc}")
    return None


def _raise_if_unavailable(path: Path | str, exc: BaseException) -> None:
    unavailable = _database_unavailable(path, exc)
    if unavailable is not None:
        raise unavailable from exc


def configure_connection(
    conn: sqlite3.Connection,
    *,
    db_path: Path | str | None = None,
) -> sqlite3.Connection:
    path_label = str(db_path) if db_path is not None else "<connection>"
    conn.row_factory = sqlite3.Row
    access_error: BaseException | None = None
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        if _is_access_error(exc):
            access_error = exc
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _require_journal_mode(conn)
        _require_foreign_keys(conn)
    except sqlite3.OperationalError as exc:
        _raise_if_unavailable(path_label, exc)
        raise
    except MarketReviewError:
        if access_error is not None:
            raise DatabaseUnavailableError(f"{path_label}: {access_error}") from access_error
        raise
    return conn


def connect(db_path: PathLike) -> sqlite3.Connection:
    if isinstance(db_path, sqlite3.Connection):
        return configure_connection(db_path)
    path = Path(db_path)
    conn: sqlite3.Connection | None = None
    try:
        if str(path) != ":memory:":
            ensure_db_parent_dir(path)
        conn = sqlite3.connect(path)
        return configure_connection(conn, db_path=path)
    except (OSError, sqlite3.OperationalError) as exc:
        if conn is not None:
            conn.close()
        _raise_if_unavailable(path, exc)
        raise
    except DatabaseUnavailableError:
        if conn is not None:
            conn.close()
        raise


def init_db(db_path: PathLike) -> sqlite3.Connection:
    owns_connection = not isinstance(db_path, sqlite3.Connection)
    path_label: Path | str
    if isinstance(db_path, sqlite3.Connection):
        path_label = "<connection>"
        conn = connect(db_path)
    else:
        path_label = Path(db_path)
        conn = connect(db_path)
    try:
        with review_transaction(conn):
            for statement in DDL_STATEMENTS:
                conn.execute(statement)
    except (OSError, sqlite3.OperationalError) as exc:
        if owns_connection:
            conn.close()
        _raise_if_unavailable(path_label, exc)
        raise
    except DatabaseUnavailableError:
        if owns_connection:
            conn.close()
        raise
    return conn
