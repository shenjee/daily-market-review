"""Tests for path resolution."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401

from marketreview.paths import resolve_db_path


class TestPaths(unittest.TestCase):
    def test_default_home(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            path = resolve_db_path()
        self.assertEqual(path, Path.home() / ".marketreview" / "market_review.sqlite3")

    def test_marketreview_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MARKETREVIEW_HOME": tmp}, clear=True):
                path = resolve_db_path()
            self.assertEqual(path.resolve(), (Path(tmp) / "market_review.sqlite3").resolve())

    def test_db_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "custom.sqlite3"
            self.assertEqual(resolve_db_path(db), db.resolve())

    def test_db_override_without_sqlite3_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "review.db"
            self.assertEqual(resolve_db_path(db), db.resolve())


if __name__ == "__main__":
    unittest.main()
