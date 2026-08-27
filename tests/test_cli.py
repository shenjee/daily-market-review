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


class TestCli(unittest.TestCase):
    def test_get_and_save_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market_review.sqlite3"
            with patch("sys.stdin", io.StringIO(json.dumps({"pe_sh": 17.0}))):
                rc = cli.main(
                    [
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
                rc = cli.main(["--db", str(db_path), "get", "--date", "2026-08-21"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["review"]["pe_sh"], 17.0)

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
                rc = cli.main(["--db", str(db_path), "get", "--date", "2026-08-21"])
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


if __name__ == "__main__":
    unittest.main()
