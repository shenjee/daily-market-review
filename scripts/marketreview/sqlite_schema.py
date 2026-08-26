"""SQLite schema for daily market review."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

from .db import review_transaction
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

DDL_STATEMENTS: tuple[str, ...] = (
    DDL_DAILY_MARKET_REVIEW,
    DDL_DAILY_PRICE_LIMIT_EVENT,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def connect(db_path: PathLike) -> sqlite3.Connection:
    if isinstance(db_path, sqlite3.Connection):
        return configure_connection(db_path)
    path = Path(db_path)
    if str(path) != ":memory:":
        ensure_db_parent_dir(path)
    conn = sqlite3.connect(path)
    return configure_connection(conn)


def init_db(db_path: PathLike) -> sqlite3.Connection:
    conn = connect(db_path)
    with review_transaction(conn):
        for statement in DDL_STATEMENTS:
            conn.execute(statement)
    return conn
