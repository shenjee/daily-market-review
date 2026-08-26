"""SQLite transaction helpers for market review."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from itertools import count
from typing import Iterator

_SAVEPOINT_COUNTER = count(1)


@contextmanager
def review_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    owns_tx = not conn.in_transaction
    savepoint: str | None = None
    if owns_tx:
        conn.execute("BEGIN")
    else:
        savepoint = f"marketreview_patch_{next(_SAVEPOINT_COUNTER)}"
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
        if owns_tx:
            conn.commit()
        else:
            assert savepoint is not None
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        if owns_tx:
            conn.rollback()
        else:
            assert savepoint is not None
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
