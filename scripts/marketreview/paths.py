"""Runtime paths for daily market review data."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HOME = Path.home() / ".marketreview"
DB_FILENAME = "market_review.sqlite3"


def default_market_review_db_path() -> Path:
    """Return the default SQLite path for the current user."""
    return resolve_db_path()


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve database path with priority: --db > MARKETREVIEW_HOME > ~/.marketreview."""
    if db_path is not None:
        path = Path(db_path).expanduser()
        if path.suffix == ".sqlite3":
            return path.resolve()
        return (path / DB_FILENAME).resolve()

    home_value = os.environ.get("MARKETREVIEW_HOME")
    if home_value:
        base = Path(home_value).expanduser().resolve()
    else:
        base = DEFAULT_HOME
    return (base / DB_FILENAME).resolve()


def ensure_db_parent_dir(db_path: Path) -> None:
    """Create the database parent directory with user-private permissions."""
    if str(db_path) == ":memory:":
        return
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
