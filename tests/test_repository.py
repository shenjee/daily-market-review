"""Repository tests for daily market review persistence."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import asdict
from datetime import date
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401

from marketreview.errors import InvalidFieldValueError, MarketReviewError
from marketreview.repository import MarketReviewRepository
from marketreview.schema import PriceLimitEventInput
from marketreview.sqlite_schema import (
    DDL_DAILY_MARKET_REVIEW,
    DDL_DAILY_PRICE_LIMIT_EVENT,
    init_db,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden_2026_08_21.json"


def _user_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


class TestMarketReviewRepository(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.repo = MarketReviewRepository(self.conn)

    def tearDown(self) -> None:
        self.repo.close()
        self.conn.close()

    def _event_inputs(self) -> list[PriceLimitEventInput]:
        return [PriceLimitEventInput(**event) for event in self.fixture["price_limit_events"]]

    def test_golden_fixture_round_trip(self) -> None:
        self.repo.save_review(self.fixture["trade_date"], fields=self.fixture["atoms"])
        self.repo.save_price_limit_events(self.fixture["trade_date"], self._event_inputs())

        review = self.repo.get_review(self.fixture["trade_date"])
        assert review is not None
        for key, value in self.fixture["atoms"].items():
            with self.subTest(field=key):
                self.assertEqual(getattr(review, key), value)

        events = self.repo.get_price_limit_events(self.fixture["trade_date"])
        self.assertEqual(len(events), len(self.fixture["price_limit_events"]))
        actual = {
            (item.market, item.code, item.direction): item
            for item in events
        }
        for event in self.fixture["price_limit_events"]:
            saved = actual[(event["market"], event["code"], event["direction"])]
            self.assertEqual(asdict(saved), {"trade_date": self.fixture["trade_date"], **event})

    def test_save_review_rejects_bool_for_integer_field(self) -> None:
        with self.assertRaises(InvalidFieldValueError):
            self.repo.save_review("2026-08-21", fields={"advancing_count": True})
        self.assertIsNone(self.repo.get_review("2026-08-21"))

    def test_replace_price_limit_event_direction_is_atomic(self) -> None:
        self.repo.save_price_limit_events(
            "2026-08-21",
            [PriceLimitEventInput("sh", "600519", "贵州茅台", "up", True, 1000, 4)],
        )
        self.repo.replace_price_limit_event_direction(
            "2026-08-21",
            "sh",
            "600519",
            "up",
            PriceLimitEventInput("sh", "600519", "贵州茅台", "down", True, 1000, 0),
        )
        events = self.repo.get_price_limit_events("2026-08-21")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].direction, "down")

    def test_replace_price_limit_event_direction_requires_existing_old_identity(self) -> None:
        with self.assertRaises(InvalidFieldValueError) as ctx:
            self.repo.replace_price_limit_event_direction(
                "2026-08-21",
                "sh",
                "600519",
                "up",
                PriceLimitEventInput("sh", "600519", "贵州茅台", "down", True, 1000, 0),
            )
        self.assertIn("被替换事件不存在", str(ctx.exception))
        self.assertEqual(self.repo.get_price_limit_events("2026-08-21"), [])

    def test_replace_price_limit_event_direction_rejects_existing_target_identity(self) -> None:
        self.repo.save_price_limit_events(
            "2026-08-21",
            [
                PriceLimitEventInput("sh", "600519", "贵州茅台", "up", True, 1000, 4),
                PriceLimitEventInput("sh", "600519", "贵州茅台", "down", True, 1000, 0),
            ],
        )
        with self.assertRaises(InvalidFieldValueError) as ctx:
            self.repo.replace_price_limit_event_direction(
                "2026-08-21",
                "sh",
                "600519",
                "up",
                PriceLimitEventInput("sh", "600519", "贵州茅台", "down", False, 1000, 0),
            )
        self.assertIn("目标方向已存在", str(ctx.exception))
        events = {
            item.direction: item
            for item in self.repo.get_price_limit_events("2026-08-21")
        }
        self.assertEqual(set(events), {"up", "down"})
        self.assertEqual(events["up"].streak_height, 4)
        self.assertTrue(events["down"].closed_at_limit)

    def test_repository_accepts_path_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market_review.sqlite3"
            with MarketReviewRepository(path) as repo:
                repo.save_review("2026-08-21", fields={"pe_sh": 17.0})
                review = repo.get_review("2026-08-21")
                assert review is not None
                self.assertEqual(review.pe_sh, 17.0)

    def test_overwrite_preserves_created_at(self) -> None:
        repository_module = sys.modules["marketreview.repository"]
        with patch.object(repository_module, "utc_now_iso", return_value="2026-08-21T00:00:00+00:00"):
            self.repo.save_price_limit_events("2026-08-21", self._event_inputs()[:1])
        with patch.object(repository_module, "utc_now_iso", return_value="2026-08-21T01:00:00+00:00"):
            self.repo.save_price_limit_events(
                "2026-08-21",
                [PriceLimitEventInput("sh", "600519", "茅台", "up", True, 1000, 5)],
            )
        row = self.conn.execute(
            """
            SELECT created_at, updated_at, name, streak_height
            FROM daily_price_limit_event
            WHERE trade_date = '2026-08-21' AND code = '600519' AND direction = 'up'
            """
        ).fetchone()
        self.assertEqual(row[0], "2026-08-21T00:00:00+00:00")
        self.assertEqual(row[1], "2026-08-21T01:00:00+00:00")


class TestSchemaInit(unittest.TestCase):
    def test_init_db_creates_review_event_and_extension_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            init_db(conn)
            self.assertEqual(
                _user_tables(conn),
                {
                    "daily_market_review",
                    "daily_price_limit_event",
                    "daily_price_limit_event_detail",
                    "daily_price_limit_event_sector",
                    "daily_price_limit_event_reason",
                },
            )
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertIn(journal_mode.lower(), {"wal", "memory"})
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            conn.close()

    def test_init_db_upgrades_v1_database_without_rewriting_data(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(DDL_DAILY_MARKET_REVIEW)
            conn.execute(DDL_DAILY_PRICE_LIMIT_EVENT)
            conn.execute(
                """
                INSERT INTO daily_price_limit_event (
                    trade_date, market, code, name, direction,
                    closed_at_limit, limit_rate_bp, streak_height,
                    created_at, updated_at
                ) VALUES (
                    '2026-08-21', 'sh', '600519', '贵州茅台', 'up',
                    1, 1000, 4, '2026-08-21T00:00:00+00:00', '2026-08-21T00:00:00+00:00'
                )
                """
            )
            conn.commit()
            self.assertEqual(
                _user_tables(conn),
                {"daily_market_review", "daily_price_limit_event"},
            )
            init_db(conn)
            self.assertEqual(
                _user_tables(conn),
                {
                    "daily_market_review",
                    "daily_price_limit_event",
                    "daily_price_limit_event_detail",
                    "daily_price_limit_event_sector",
                    "daily_price_limit_event_reason",
                },
            )
            row = conn.execute(
                """
                SELECT name, streak_height FROM daily_price_limit_event
                WHERE trade_date = '2026-08-21' AND code = '600519'
                """
            ).fetchone()
            self.assertEqual(tuple(row), ("贵州茅台", 4))
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            conn.close()

    def test_init_db_rejects_in_transaction_connection_without_foreign_keys(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("BEGIN")
            self.assertTrue(conn.in_transaction)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 0)
            with self.assertRaises(MarketReviewError) as ctx:
                init_db(conn)
            self.assertEqual(ctx.exception.code, "SQLITE_FOREIGN_KEYS")
            self.assertIn("事务", ctx.exception.message)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 0)
            self.assertEqual(_user_tables(conn), set())
        finally:
            conn.close()

    def test_init_db_accepts_in_transaction_connection_when_foreign_keys_already_on(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN")
            self.assertTrue(conn.in_transaction)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            init_db(conn)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertIn(
                conn.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                {"wal", "memory"},
            )
            self.assertIn("daily_price_limit_event_detail", _user_tables(conn))
        finally:
            conn.close()

    def test_init_db_rejects_file_connection_in_transaction_when_wal_not_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "market_review.sqlite3")
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN")
                self.assertTrue(conn.in_transaction)
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                    "delete",
                )
                with self.assertRaises(MarketReviewError) as ctx:
                    init_db(conn)
                self.assertEqual(ctx.exception.code, "SQLITE_JOURNAL_MODE")
                self.assertIn("WAL", ctx.exception.message)
                self.assertEqual(
                    conn.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                    "delete",
                )
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(_user_tables(conn), set())
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
