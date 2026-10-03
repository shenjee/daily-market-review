"""CLI tests."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401

import cli

_ISOLATED_STATE = tempfile.TemporaryDirectory()


def setUpModule() -> None:
    _ISOLATED_STATE.__enter__()


def tearDownModule() -> None:
    _ISOLATED_STATE.cleanup()


class TestCli(unittest.TestCase):
    def setUp(self) -> None:
        self._state_patch = patch(
            "cli._command_state_dir",
            lambda: Path(_ISOLATED_STATE.name),
        )
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)
    def test_get_and_save_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market_review.sqlite3"
            with patch("sys.stdin", io.StringIO(json.dumps({"pe_sh": 17.0}))):
                rc = cli.main(
                    [
                        "--backend",
                        "sqlite",
                        "--db",
                        str(db_path),
                        "save-review",
                        "--date",
                        "2026-08-21",
                        "--input",
                        "-",
                    ]
                )
            self.assertEqual(rc, 0)

            buffer = io.StringIO()
            with patch("sys.stdout", buffer):
                rc = cli.main(["--backend", "sqlite", "--db", str(db_path), "get", "--date", "2026-08-21"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["review"]["pe_sh"], 17.0)
            self.assertEqual(
                payload["data"]["ladder"],
                {
                    "groups": [],
                    "broken_limit_up": [],
                    "opened_limit_down": [],
                    "closed_limit_down": [],
                },
            )

    def test_save_events_rejects_invalid_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market_review.sqlite3"
            event = {
                "market": "xx",
                "code": "oops",
                "name": "无效",
                "direction": "sideways",
                "closed_at_limit": True,
                "limit_rate_bp": 1000,
                "streak_height": -1,
            }
            with patch("sys.stdin", io.StringIO(json.dumps({"events": [event]}))):
                buffer = io.StringIO()
                with patch("sys.stdout", buffer):
                    rc = cli.main(
                        [
                            "--backend",
                            "sqlite",
                            "--db",
                            str(db_path),
                            "save-events",
                            "--date",
                            "2026-08-21",
                            "--input",
                            "-",
                        ]
                    )
            self.assertEqual(rc, 1)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])

    def test_save_events_rejects_fullwidth_digit_code(self) -> None:
        event = {
            "market": "sz",
            "code": "１２３４５６",
            "name": "全角代码",
            "direction": "up",
            "closed_at_limit": True,
            "limit_rate_bp": 1000,
            "streak_height": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market_review.sqlite3"
            buffer = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps({"events": [event]}))):
                with patch("sys.stdout", buffer):
                    rc = cli.main(
                        [
                            "--backend",
                            "sqlite",
                            "--db",
                            str(db_path),
                            "save-events",
                            "--date",
                            "2026-08-21",
                            "--input",
                            "-",
                        ]
                    )
            self.assertEqual(rc, 1)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])

            get_buffer = io.StringIO()
            with patch("sys.stdout", get_buffer):
                rc = cli.main(["--backend", "sqlite", "--db", str(db_path), "get", "--date", "2026-08-21"])
            self.assertEqual(rc, 0)
            get_payload = json.loads(get_buffer.getvalue())
            self.assertTrue(get_payload["ok"])
            self.assertEqual(get_payload["data"]["events"], [])

    def test_save_events_accepts_code_not_in_any_master(self) -> None:
        event = {
            "market": "sz",
            "code": "001232",
            "name": "嘉立创",
            "direction": "up",
            "closed_at_limit": True,
            "limit_rate_bp": 1000,
            "streak_height": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market_review.sqlite3"
            buffer = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps({"events": [event]}))):
                with patch("sys.stdout", buffer):
                    rc = cli.main(
                        [
                            "--backend",
                            "sqlite",
                            "--db",
                            str(db_path),
                            "save-events",
                            "--date",
                            "2026-08-21",
                            "--input",
                            "-",
                        ]
                    )
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["saved_count"], 1)


    def test_save_event_details_and_get_ladder(self) -> None:
        event = {
            "market": "sh",
            "code": "600519",
            "name": "贵州茅台",
            "direction": "up",
            "closed_at_limit": True,
            "limit_rate_bp": 1000,
            "streak_height": 4,
        }
        detail = {
            "market": "sh",
            "code": "600519",
            "direction": "up",
            "sectors": ["白酒", "消费"],
            "limit_up_reasons": ["业绩增长"],
            "previous_close": 1520.5,
            "open_price": 1568.0,
            "is_leader": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market_review.sqlite3"
            with patch("sys.stdin", io.StringIO(json.dumps({"events": [event]}))):
                rc = cli.main(
                    ["--backend", "sqlite", "--db", str(db_path), "save-events", "--date", "2026-08-21", "--input", "-"]
                )
            self.assertEqual(rc, 0)

            buffer = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps({"details": [detail]}))):
                with patch("sys.stdout", buffer):
                    rc = cli.main(
                        [
                            "--backend",
                            "sqlite",
                            "--db",
                            str(db_path),
                            "save-event-details",
                            "--date",
                            "2026-08-21",
                            "--input",
                            "-",
                        ]
                    )
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["saved_count"], 1)

            get_buffer = io.StringIO()
            with patch("sys.stdout", get_buffer):
                rc = cli.main(["--backend", "sqlite", "--db", str(db_path), "get", "--date", "2026-08-21"])
            self.assertEqual(rc, 0)
            get_payload = json.loads(get_buffer.getvalue())
            self.assertTrue(get_payload["ok"])
            events = get_payload["data"]["events"]
            self.assertEqual(set(events[0]), {
                "trade_date",
                "market",
                "code",
                "name",
                "direction",
                "closed_at_limit",
                "limit_rate_bp",
                "streak_height",
            })
            ladder = get_payload["data"]["ladder"]
            self.assertEqual(ladder["groups"][0]["streak_height"], 4)
            stock = ladder["groups"][0]["stocks"][0]
            self.assertEqual(stock["sectors"], ["白酒", "消费"])
            self.assertIs(stock["is_leader"], True)
            self.assertAlmostEqual(stock["open_change"], 1568.0 / 1520.5 - 1)

    def test_get_streak_rate_uses_previous_trading_day_effective_limit_up(self) -> None:
        previous_events = [
            {
                "market": "sh",
                "code": "600519",
                "name": "贵州茅台",
                "direction": "up",
                "closed_at_limit": True,
                "limit_rate_bp": 1000,
                "streak_height": 1,
            },
            {
                "market": "sz",
                "code": "000858",
                "name": "五粮液",
                "direction": "up",
                "closed_at_limit": True,
                "limit_rate_bp": 1000,
                "streak_height": 1,
            },
        ]
        today_events = [
            {
                "market": "sh",
                "code": "600519",
                "name": "贵州茅台",
                "direction": "up",
                "closed_at_limit": True,
                "limit_rate_bp": 1000,
                "streak_height": 2,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market_review.sqlite3"
            with patch("sys.stdin", io.StringIO(json.dumps({"events": previous_events}))):
                rc = cli.main(
                    ["--backend", "sqlite", "--db", str(db_path), "save-events", "--date", "2026-08-20", "--input", "-"]
                )
            self.assertEqual(rc, 0)
            with patch("sys.stdin", io.StringIO(json.dumps({"events": today_events}))):
                rc = cli.main(
                    ["--backend", "sqlite", "--db", str(db_path), "save-events", "--date", "2026-08-21", "--input", "-"]
                )
            self.assertEqual(rc, 0)

            get_buffer = io.StringIO()
            with patch("sys.stdout", get_buffer):
                rc = cli.main(["--backend", "sqlite", "--db", str(db_path), "get", "--date", "2026-08-21"])
            self.assertEqual(rc, 0)
            payload = json.loads(get_buffer.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["summary"]["streak_board_count"], 1)
            self.assertEqual(payload["data"]["summary"]["streak_rate_pct"], 50.0)

    def test_save_event_details_rejects_reasons_on_down_event(self) -> None:
        event = {
            "market": "sh",
            "code": "600519",
            "name": "贵州茅台",
            "direction": "down",
            "closed_at_limit": True,
            "limit_rate_bp": 1000,
            "streak_height": 0,
        }
        detail = {
            "market": "sh",
            "code": "600519",
            "direction": "down",
            "limit_up_reasons": ["误标"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market_review.sqlite3"
            with patch("sys.stdin", io.StringIO(json.dumps({"events": [event]}))):
                rc = cli.main(
                    ["--backend", "sqlite", "--db", str(db_path), "save-events", "--date", "2026-08-21", "--input", "-"]
                )
            self.assertEqual(rc, 0)
            buffer = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps({"details": [detail]}))):
                with patch("sys.stdout", buffer):
                    rc = cli.main(
                        [
                            "--backend",
                            "sqlite",
                            "--db",
                            str(db_path),
                            "save-event-details",
                            "--date",
                            "2026-08-21",
                            "--input",
                            "-",
                        ]
                    )
            self.assertEqual(rc, 1)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIn("limit_up_reasons", payload["error"]["message"])

    def test_get_reports_db_unavailable_when_path_cannot_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "not_a_database"
            db_path.mkdir()
            buffer = io.StringIO()
            with patch("sys.stdout", buffer):
                rc = cli.main(["--backend", "sqlite", "--db", str(db_path), "get", "--date", "2026-08-21"])
            self.assertEqual(rc, 1)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "DB_UNAVAILABLE")
            self.assertIn("状态未知", payload["error"]["message"])

    def test_get_reports_db_unavailable_when_wal_directory_not_writable(self) -> None:
        import os

        from marketreview.sqlite_schema import init_db

        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp) / "waldir"
            db_dir.mkdir()
            db_path = db_dir / "market_review.sqlite3"
            conn = init_db(db_path)
            conn.close()
            os.chmod(db_dir, 0o555)
            try:
                buffer = io.StringIO()
                with patch("sys.stdout", buffer):
                    rc = cli.main(["--backend", "sqlite", "--db", str(db_path), "get", "--date", "2026-08-21"])
                self.assertEqual(rc, 1)
                payload = json.loads(buffer.getvalue())
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error"]["code"], "DB_UNAVAILABLE")
                self.assertIn("readonly", payload["error"]["message"].lower())
            finally:
                os.chmod(db_dir, 0o755)

    def test_get_reports_db_unavailable_when_sqlite_file_is_readonly(self) -> None:
        import os
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "readonly.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE t(x INTEGER)")
            conn.commit()
            conn.close()
            os.chmod(db_path, 0o444)
            try:
                buffer = io.StringIO()
                with patch("sys.stdout", buffer):
                    rc = cli.main(["--backend", "sqlite", "--db", str(db_path), "get", "--date", "2026-08-21"])
                self.assertEqual(rc, 1)
                payload = json.loads(buffer.getvalue())
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error"]["code"], "DB_UNAVAILABLE")
                self.assertNotEqual(payload["error"]["code"], "SQLITE_JOURNAL_MODE")
                self.assertIn("readonly", payload["error"]["message"].lower())
            finally:
                os.chmod(db_path, 0o644)

    def test_missing_cloud_config_stays_stopped_on_repeat(self) -> None:
        import os

        from marketreview.storage import MarketReviewRepository

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            config_dir.mkdir()
            opened: list[object] = []
            real_init = MarketReviewRepository.__init__

            def counting_init(repo, *args, **kwargs):
                opened.append(repo)
                return real_init(repo, *args, **kwargs)

            with patch.dict(os.environ, {"MARKETREVIEW_CONFIG_DIR": str(config_dir)}):
                with patch.object(MarketReviewRepository, "__init__", counting_init):
                    for _ in range(2):
                        buffer = io.StringIO()
                        with patch("sys.stdout", buffer):
                            rc = cli.main(["get", "--date", "2026-08-21"])
                        self.assertEqual(rc, 1)
                        payload = json.loads(buffer.getvalue())
                        self.assertEqual(payload["error"]["code"], "CONFIG_MISSING")
                        self.assertIn("不会改用本地数据库", payload["error"]["message"])
            self.assertEqual(opened, [])
            self.assertFalse((Path(tmp) / "market_review.sqlite3").exists())

    def test_supabase_backend_rejects_db_before_opening_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market_review.sqlite3"
            buffer = io.StringIO()
            with patch("sys.stdout", buffer):
                rc = cli.main(
                    ["--backend", "supabase", "--db", str(db_path), "get", "--date", "2026-08-21"]
                )
            self.assertEqual(rc, 1)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["error"]["code"], "BACKEND_CONFLICT")
            self.assertFalse(db_path.exists())

    def test_verify_pending_on_sqlite_does_not_call_cloud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market_review.sqlite3"
            buffer = io.StringIO()
            with patch("sys.stdout", buffer):
                rc = cli.main(["--backend", "sqlite", "--db", str(db_path), "verify-pending"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["data"]["status"], "sqlite")


if __name__ == "__main__":
    unittest.main()
