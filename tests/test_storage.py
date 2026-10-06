"""Storage adapter tests. They use a temp config directory and a fake RPC transport."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import patch

import _bootstrap  # noqa: F401

from marketreview.backend import (
    CLOUD_DEFAULT_ENABLED,
    CONTRACT_DEFAULT_BACKEND,
    effective_default_backend,
    load_supabase_settings,
    resolve_backend_name,
)
from marketreview.errors import BackendSelectionError, InvalidFieldValueError, RemoteStoreError
from marketreview.schema import PriceLimitEventInput
from marketreview.storage import open_repository
from marketreview.supabase_store import (
    MAX_BODY_BYTES,
    SupabaseRepository,
    _RefuseRedirect,
    _http_failure,
    _read_limited,
    _transport_failure,
)
from marketreview.write_gate import read_pending, same_json_value


def _envelope(**extra: Any) -> dict[str, Any]:
    payload = {"format_version": 1, "schema_version": 1, "complete": True}
    payload.update(extra)
    return payload


def _review(trade_date: str = "2026-08-21", **fields: Any) -> dict[str, Any]:
    from marketreview.schema import ATOMIC_FIELD_NAMES

    payload: dict[str, Any] = {"trade_date": trade_date, "created_at": "t", "updated_at": "t"}
    for name in ATOMIC_FIELD_NAMES:
        payload[name] = fields.get(name)
    return payload


def _event(**overrides: Any) -> dict[str, Any]:
    payload = {
        "trade_date": "2026-08-21",
        "market": "sh",
        "code": "600519",
        "name": "贵州茅台",
        "direction": "up",
        "closed_at_limit": True,
        "limit_rate_bp": 1000,
        "streak_height": 4,
        "created_at": "t",
        "updated_at": "t",
    }
    payload.update(overrides)
    return payload


BATCH = "2026-08-21T00:00:00+00:00"


def _detail(
    *,
    trade_date: str = "2026-08-21",
    market: str = "sh",
    code: str = "600519",
    direction: str = "up",
    **overrides: Any,
) -> dict[str, Any]:
    payload = {
        "trade_date": trade_date,
        "market": market,
        "code": code,
        "direction": direction,
        "previous_turnover_amount": None,
        "auction_amount": None,
        "previous_close": None,
        "open_price": None,
        "turnover_amount": None,
        "turnover_rate": None,
        "is_leader": None,
        "note": None,
        "created_at": "2026-08-21T07:00:00+00:00",
        "updated_at": "2026-08-21T07:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def _direction_present(direction: str = "up", **overrides: Any) -> dict[str, Any]:
    detail = overrides.pop("detail", None)
    detail_exists = overrides.pop("detail_exists", detail is not None)
    event = _event(
        direction=direction,
        created_at=overrides.pop("created_at", "2026-08-21T07:00:00+00:00"),
        updated_at=overrides.pop("updated_at", "2026-08-21T07:00:00+00:00"),
        **overrides,
    )
    return {
        "exists": True,
        "event": event,
        "detail_exists": detail_exists,
        "detail": detail,
        "sectors": ["白酒"],
        "reasons": [] if direction == "down" else ["业绩"],
    }


def _direction_absent() -> dict[str, Any]:
    return {
        "exists": False,
        "event": None,
        "detail_exists": False,
        "detail": None,
        "sectors": [],
        "reasons": [],
    }


def _preimage(**extra: Any) -> dict[str, Any]:
    payload = _envelope(
        revision=4,
        trade_date="2026-08-21",
        market="sh",
        code="600519",
        old_direction="up",
        new_direction="down",
        old=_direction_present(),
        new=_direction_absent(),
    )
    payload.update(extra)
    return payload


def _open_replace_record(preimage: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "format_version": 1,
        "operation_id": "replace-1",
        "kind": "replace-direction",
        "project_ref": "example.supabase.co",
        "schema_version": 1,
        "started_at": BATCH,
        "payload": {
            "schema_version": 1,
            "trade_date": "2026-08-21",
            "batch_time": BATCH,
            "market": "sh",
            "code": "600519",
            "old_direction": "up",
            "event": {
                "market": "sh",
                "code": "600519",
                "name": "贵州茅台",
                "direction": "down",
                "closed_at_limit": False,
                "limit_rate_bp": 1000,
                "streak_height": 0,
            },
        },
        "preimage": preimage or _preimage(),
    }


class FakeTransport:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any], bool]] = []

    def call(self, function: str, request: dict[str, Any], *, write: bool) -> dict[str, Any]:
        self.calls.append((function, dict(request), write))
        response = self.responses[function]
        if isinstance(response, Exception):
            raise response
        return response


def _cloud_repo(transport: FakeTransport, root: Path) -> SupabaseRepository:
    return SupabaseRepository(
        transport,
        clock=lambda: "2026-08-21T00:00:00+00:00",
        state_dir=root / "state",
        project_ref="example.supabase.co",
    )


class TestBackendSelection(unittest.TestCase):
    def test_contract_default_is_supabase_and_daily_switch_is_on(self) -> None:
        self.assertEqual(CONTRACT_DEFAULT_BACKEND, "supabase")
        self.assertTrue(CLOUD_DEFAULT_ENABLED)
        self.assertEqual(effective_default_backend(), "supabase")

    def test_explicit_backend_overrides_config_and_default(self) -> None:
        self.assertEqual(
            resolve_backend_name(explicit="sqlite", configured="supabase", default="supabase"),
            "sqlite",
        )
        self.assertEqual(
            resolve_backend_name(explicit=None, configured="supabase", default="sqlite"),
            "supabase",
        )
        self.assertEqual(
            resolve_backend_name(explicit=None, configured=None, default="supabase"),
            "supabase",
        )

    def test_invalid_backend_is_rejected(self) -> None:
        with self.assertRaises(BackendSelectionError):
            resolve_backend_name(explicit="postgres", configured=None, default="sqlite")

    def test_marketreview_home_does_not_select_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "supabase.config"
            config.write_text("backend=sqlite\n", encoding="utf-8")
            db_path = Path(tmp) / "local.sqlite3"
            with patch.dict(os.environ, {"MARKETREVIEW_HOME": tmp, "MARKETREVIEW_CONFIG_DIR": tmp}):
                with open_repository(backend=None, db_path=str(db_path)) as repo:
                    repo.save_review("2026-08-21", {"pe_sh": 1})
                    self.assertEqual(repo.get_review("2026-08-21").pe_sh, 1)

    def test_supabase_with_db_path_does_not_call_transport_or_require_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport({})
            with self.assertRaises(BackendSelectionError) as ctx:
                open_repository(
                    backend="supabase",
                    db_path=str(Path(tmp) / "local.sqlite3"),
                    config_dir=Path(tmp),
                    transport=transport,
                )
            self.assertEqual(ctx.exception.code, "BACKEND_CONFLICT")
            self.assertEqual(transport.calls, [])

    def test_missing_cloud_settings_do_not_open_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BackendSelectionError) as ctx:
                open_repository(backend="supabase", db_path=None, config_dir=Path(tmp))
            self.assertEqual(ctx.exception.code, "CONFIG_MISSING")
            self.assertFalse((Path(tmp) / "market_review.sqlite3").exists())
            # Templates are materialized so the user has files to edit.
            self.assertTrue((Path(tmp) / "supabase.config").is_file())
            self.assertTrue((Path(tmp) / "supabase.secret").is_file())
            self.assertIn("已自动创建模板", str(ctx.exception))
            self.assertIn("请编辑", str(ctx.exception))

    def test_ensure_templates_never_overwrite_existing_files(self) -> None:
        from marketreview.backend import ensure_cloud_config_templates

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            existing_config = "backend=sqlite\nsupabase_url=https://keep.example.co\n"
            existing_secret = "sb_secret_keep_me\n"
            (home / "supabase.config").write_text(existing_config, encoding="utf-8")
            (home / "supabase.secret").write_text(existing_secret, encoding="utf-8")
            created = ensure_cloud_config_templates(home)
            self.assertEqual(created, [])
            self.assertEqual((home / "supabase.config").read_text(encoding="utf-8"), existing_config)
            self.assertEqual((home / "supabase.secret").read_text(encoding="utf-8"), existing_secret)

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "secret-in-config"
            home.mkdir()
            existing_config = (
                "backend=supabase\n"
                "supabase_url=https://keep.example.co\n"
                "supabase_secret_key=sb_secret_do_not_use\n"
            )
            (home / "supabase.config").write_text(existing_config, encoding="utf-8")
            created = ensure_cloud_config_templates(home)
            self.assertEqual(created, [str(home / "supabase.secret")])
            self.assertEqual((home / "supabase.config").read_text(encoding="utf-8"), existing_config)
            with self.assertRaises(BackendSelectionError) as ctx:
                load_supabase_settings(home)
            self.assertEqual(ctx.exception.code, "CONFIG_MISSING")
            self.assertNotIn("sb_secret_do_not_use", str(ctx.exception))
            self.assertIn("不能包含 Secret Key", str(ctx.exception))

    def test_exclusive_create_skips_when_peer_wins_race(self) -> None:
        from marketreview.backend import _write_new_file_exclusive, ensure_cloud_config_templates

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / "supabase.config"
            winner = "backend=sqlite\nsupabase_url=https://peer.example.co\n"
            target.write_text(winner, encoding="utf-8")
            created = _write_new_file_exclusive(target, b"SHOULD_NOT_WRITE\n", mode=0o644)
            self.assertFalse(created)
            self.assertEqual(target.read_text(encoding="utf-8"), winner)

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            # Peer fills secret after we would have decided to create: O_EXCL must skip.
            secret = home / "supabase.secret"
            secret.write_text("sb_secret_peer_filled\n", encoding="utf-8")
            os.chmod(secret, 0o600)
            created = ensure_cloud_config_templates(home)
            self.assertNotIn(str(secret), created)
            self.assertEqual(secret.read_text(encoding="utf-8"), "sb_secret_peer_filled\n")

    def test_secret_created_with_0600_and_chmod_failure_raises(self) -> None:
        from marketreview.backend import _write_new_file_exclusive, ensure_cloud_config_templates

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            created = ensure_cloud_config_templates(home)
            secret = home / "supabase.secret"
            self.assertIn(str(secret), created)
            self.assertEqual(secret.stat().st_mode & 0o777, 0o600)

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / "supabase.secret"
            real_open = os.open

            def open_then_fail_fchmod(path, flags, mode=0o777):  # noqa: ANN001
                fd = real_open(path, flags, mode)
                return fd

            with patch("marketreview.backend.os.open", side_effect=open_then_fail_fchmod):
                with patch("marketreview.backend.os.fchmod", side_effect=OSError(1, "EPERM")):
                    with self.assertRaises(BackendSelectionError) as ctx:
                        _write_new_file_exclusive(target, b"sb_secret_x\n", mode=0o600)
            self.assertEqual(ctx.exception.code, "CONFIG_MISSING")
            self.assertIn("权限", str(ctx.exception))
            self.assertFalse(target.exists())

    def test_secret_in_supabase_config_is_rejected_and_not_logged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secret = "sb_secret_test_value"
            (Path(tmp) / "supabase.config").write_text(
                f"supabase_url=https://example.supabase.co\nsupabase_secret_key={secret}\n",
                encoding="utf-8",
            )
            (Path(tmp) / "supabase.secret").write_text("sb_secret_other\n", encoding="utf-8")
            with self.assertRaises(BackendSelectionError) as ctx:
                load_supabase_settings(Path(tmp))
            self.assertEqual(ctx.exception.code, "CONFIG_MISSING")
            self.assertNotIn(secret, str(ctx.exception))
            from marketreview.backend import redact_secret

            self.assertNotIn(secret, redact_secret(f"failed {secret}", secret))

    def test_config_template_fields_load_url_secret_and_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "supabase.config").write_text(
                "\n".join(
                    [
                        "backend=supabase",
                        "supabase_url=https://example.supabase.co",
                        "supabase_publishable_key=sb_publishable_test",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "supabase.secret").write_text("sb_secret_test_value\n", encoding="utf-8")
            settings = load_supabase_settings(root)
            self.assertEqual(settings.url, "https://example.supabase.co")
            self.assertEqual(settings.secret_key, "sb_secret_test_value")
            self.assertEqual(settings.publishable_key, "sb_publishable_test")

    def test_empty_secret_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "supabase.config").write_text(
                "supabase_url=https://example.supabase.co\n",
                encoding="utf-8",
            )
            (root / "supabase.secret").write_text("SUPABASE_SECRET_KEY=\n", encoding="utf-8")
            with self.assertRaises(BackendSelectionError) as ctx:
                load_supabase_settings(root)
            self.assertEqual(ctx.exception.code, "CONFIG_MISSING")
            self.assertIn("没有有效密钥", str(ctx.exception))

    def test_install_commands_do_not_overwrite_existing_files(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "skill"
            home = Path(tmp) / "home"
            (skill / "config").mkdir(parents=True)
            home.mkdir()
            example_config = (Path(__file__).resolve().parents[1] / "config" / "supabase.config.example").read_text(
                encoding="utf-8"
            )
            example_secret = (Path(__file__).resolve().parents[1] / "config" / "supabase.secret.example").read_text(
                encoding="utf-8"
            )
            (skill / "config" / "supabase.config.example").write_text(example_config, encoding="utf-8")
            (skill / "config" / "supabase.secret.example").write_text(example_secret, encoding="utf-8")
            existing_config = "backend=supabase\nsupabase_url=https://keep.example.co\n"
            existing_secret = "sb_secret_keep_me\n"
            (home / "supabase.config").write_text(existing_config, encoding="utf-8")
            (home / "supabase.secret").write_text(existing_secret, encoding="utf-8")
            script = f"""
set -e
mkdir -p "{home}"
[ -e "{home}/supabase.config" ] || cp "{skill}/config/supabase.config.example" "{home}/supabase.config"
[ -e "{home}/supabase.secret" ] || cp "{skill}/config/supabase.secret.example" "{home}/supabase.secret"
chmod 600 "{home}/supabase.secret"
"""
            subprocess.run(["bash", "-c", script], check=True)
            self.assertEqual((home / "supabase.config").read_text(encoding="utf-8"), existing_config)
            self.assertEqual((home / "supabase.secret").read_text(encoding="utf-8"), existing_secret)


class TestSupabaseRepository(unittest.TestCase):
    def test_read_day_checks_counts_and_restores_python_types(self) -> None:
        transport = FakeTransport(
            {
                "marketreview_get_day": _envelope(
                    review=_review(pe_sh=17.5, advancing_count=10),
                    events=[_event()],
                    details=[],
                    sectors=[
                        {
                            "trade_date": "2026-08-21",
                            "market": "sh",
                            "code": "600519",
                            "direction": "up",
                            "position": 0,
                            "value": "白酒",
                        }
                    ],
                    reasons=[],
                    previous_events=[],
                    counts={
                        "reviews": 1,
                        "events": 1,
                        "details": 0,
                        "sectors": 1,
                        "reasons": 0,
                        "previous_events": 0,
                    },
                )
            }
        )
        repo = SupabaseRepository(transport)
        day = repo.read_day("2026-08-21", "2026-08-20")
        self.assertEqual(day.review.pe_sh, 17.5)
        self.assertEqual(day.review.advancing_count, 10)
        self.assertTrue(day.events[0].closed_at_limit)
        self.assertEqual(day.details[0].sectors, ["白酒"])
        self.assertEqual(day.details[0].previous_turnover_amount, None)
        self.assertEqual(transport.calls[0][0], "marketreview_get_day")
        self.assertEqual(transport.calls[0][1]["previous_trade_date"], "2026-08-20")

    def test_json_whole_floats_restore_as_python_float_on_daily_reads(self) -> None:
        # PG jsonb emits whole double precision as JSON int; SQLite keeps float.
        transport = FakeTransport(
            {
                "marketreview_get_day": _envelope(
                    review=_review(
                        pe_sh=11,
                        margin_balance_sh=1318769000000,
                        turnover_amount_sh=804543700000,
                        advancing_count=898,
                    ),
                    events=[_event()],
                    details=[
                        _detail(
                            previous_turnover_amount=100,
                            auction_amount=0,
                            previous_close=10,
                            open_price=11,
                            turnover_amount=200,
                            turnover_rate=1,
                            is_leader=True,
                        )
                    ],
                    sectors=[],
                    reasons=[],
                    previous_events=[],
                    counts={
                        "reviews": 1,
                        "events": 1,
                        "details": 1,
                        "sectors": 0,
                        "reasons": 0,
                        "previous_events": 0,
                    },
                ),
                "marketreview_list_reviews": _envelope(
                    reviews=[
                        _review(
                            pe_sh=11,
                            margin_balance_sh=1318769000000,
                            advancing_count=898,
                        )
                    ],
                    counts={"reviews": 1},
                ),
            }
        )
        repo = SupabaseRepository(transport)
        day = repo.read_day("2026-08-21", "2026-08-20")
        self.assertIs(type(day.review.pe_sh), float)
        self.assertEqual(day.review.pe_sh, 11.0)
        self.assertIs(type(day.review.margin_balance_sh), float)
        self.assertIs(type(day.review.turnover_amount_sh), float)
        self.assertIs(type(day.review.advancing_count), int)
        self.assertEqual(day.review.advancing_count, 898)
        detail = day.details[0]
        self.assertIs(type(detail.previous_turnover_amount), float)
        self.assertIs(type(detail.auction_amount), float)
        self.assertIs(type(detail.turnover_rate), float)
        self.assertIs(detail.is_leader, True)
        reviews = repo.list_reviews()
        self.assertIs(type(reviews[0].pe_sh), float)
        self.assertEqual(reviews[0].pe_sh, 11.0)

    def test_count_mismatch_is_an_error(self) -> None:
        transport = FakeTransport(
            {
                "marketreview_list_events": _envelope(
                    events=[_event()],
                    counts={"events": 0},
                )
            }
        )
        with self.assertRaises(RemoteStoreError) as ctx:
            SupabaseRepository(transport).list_price_limit_events()
        self.assertEqual(ctx.exception.code, "INCOMPLETE_RESPONSE")

    def test_empty_writes_make_no_http_calls(self) -> None:
        transport = FakeTransport({})
        repo = SupabaseRepository(transport, clock=lambda: "2026-08-21T00:00:00+00:00")
        repo.save_review("2026-08-21", {})
        repo.save_price_limit_events("2026-08-21", [])
        repo.save_price_limit_event_details("2026-08-21", [])
        self.assertEqual(transport.calls, [])

    def test_save_review_probes_then_writes_one_batch_time(self) -> None:
        transport = FakeTransport(
            {
                "marketreview_probe": _envelope(revision=1, ledger_key="main"),
                "marketreview_save_review": _envelope(noop=False, revision=2),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = _cloud_repo(transport, Path(tmp))
            repo.save_review("2026-08-21", {"pe_sh": 0, "advancing_count": 0})
        self.assertEqual([call[0] for call in transport.calls], ["marketreview_probe", "marketreview_save_review"])
        request = transport.calls[1][1]
        self.assertEqual(request["batch_time"], "2026-08-21T00:00:00+00:00")
        self.assertEqual(request["fields"]["pe_sh"], 0)
        self.assertTrue(transport.calls[1][2])
        self.assertFalse(transport.calls[0][2])

    def test_replace_direction_uses_preimage_and_skips_probe(self) -> None:
        preimage = _preimage()
        transport = FakeTransport(
            {
                "marketreview_replace_direction_preimage": preimage,
                "marketreview_replace_direction": _envelope(noop=False, revision=3),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _cloud_repo(transport, root)
            repo.replace_price_limit_event_direction(
                "2026-08-21",
                "sh",
                "600519",
                "up",
                PriceLimitEventInput("sh", "600519", "贵州茅台", "down", False, 1000, 0),
            )
            record = read_pending(root / "state")
        self.assertEqual(
            [call[0] for call in transport.calls],
            ["marketreview_replace_direction_preimage", "marketreview_replace_direction"],
        )
        self.assertFalse(transport.calls[0][2])
        self.assertTrue(transport.calls[1][2])
        self.assertEqual(transport.calls[1][1]["event"]["closed_at_limit"], False)
        assert record is not None
        self.assertEqual(record["status"], "closed")
        self.assertEqual(record["result"], "confirmed")
        self.assertEqual(record["project_ref"], "example.supabase.co")
        self.assertNotIn("secret", json.dumps(record))

    def test_replace_direction_does_not_write_when_preimage_fails(self) -> None:
        transport = FakeTransport(
            {
                "marketreview_replace_direction_preimage": RemoteStoreError(
                    "被替换事件不存在",
                    code="REPLACE_SOURCE_MISSING",
                )
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _cloud_repo(transport, root)
            with self.assertRaises(RemoteStoreError):
                repo.replace_price_limit_event_direction(
                    "2026-08-21",
                    "sh",
                    "600519",
                    "up",
                    PriceLimitEventInput("sh", "600519", "贵州茅台", "down", True, 1000, 0),
                )
            self.assertIsNone(read_pending(root / "state"))
        self.assertEqual(len(transport.calls), 1)

    def test_unknown_review_field_is_rejected_before_http(self) -> None:
        transport = FakeTransport({})
        with self.assertRaises(InvalidFieldValueError):
            SupabaseRepository(transport).save_review("2026-08-21", {"nope": 1})
        self.assertEqual(transport.calls, [])

    def test_list_ranges_omit_absent_bounds(self) -> None:
        transport = FakeTransport(
            {
                "marketreview_list_trade_dates": _envelope(
                    trade_dates=["2026-08-20", "2026-08-21"],
                    counts={"trade_dates": 2},
                )
            }
        )
        dates = SupabaseRepository(transport).list_trade_dates(start_date="2026-08-20")
        self.assertEqual(dates, ["2026-08-20", "2026-08-21"])
        self.assertEqual(transport.calls[0][1], {"schema_version": 1, "start_date": "2026-08-20"})

    def test_http_errors_keep_business_codes_and_hide_secrets(self) -> None:
        secret = "sb_secret_test_value"
        business = _http_failure(
            400,
            '{"code":"P0001","hint":"PARENT_EVENT_MISSING","message":"[PARENT_EVENT_MISSING] 父事件不存在"}',
            write=True,
        )
        self.assertEqual(business.code, "PARENT_EVENT_MISSING")
        forbidden = _http_failure(401, '{"message":"bad key ' + secret + '"}', write=False)
        self.assertEqual(forbidden.code, "REMOTE_FORBIDDEN")
        self.assertNotIn(secret, str(forbidden))
        limited_write = _http_failure(429, "{}", write=True)
        self.assertEqual(limited_write.code, "REMOTE_RESULT_UNKNOWN")
        self.assertIn("不能视为已回滚", str(limited_write))
        limited_read = _http_failure(429, "{}", write=False)
        self.assertEqual(limited_read.code, "REMOTE_UNAVAILABLE")
        unknown = _transport_failure(
            urllib.error.URLError(f"timed out {secret}"),
            secret=secret,
            write=True,
        )
        self.assertEqual(unknown.code, "REMOTE_RESULT_UNKNOWN")
        self.assertNotIn(secret, str(unknown))
        self.assertIn("不能视为已回滚", str(unknown))

    def test_redirect_to_another_host_is_refused(self) -> None:
        handler = _RefuseRedirect("example.supabase.co", "sb_secret_test_value")
        with self.assertRaises(RemoteStoreError) as ctx:
            handler.redirect_request(
                None,
                None,
                302,
                "Found",
                {},
                "https://evil.example/rest/v1/rpc/marketreview_probe",
            )
        self.assertEqual(ctx.exception.code, "REMOTE_REDIRECT")
        self.assertNotIn("sb_secret_test_value", str(ctx.exception))

    def test_cloud_write_without_state_dir_sends_nothing(self) -> None:
        transport = FakeTransport(
            {"marketreview_probe": _envelope(revision=1), "marketreview_save_review": _envelope(noop=False, revision=2)}
        )
        repo = SupabaseRepository(transport, clock=lambda: "2026-08-21T00:00:00+00:00")
        with self.assertRaises(RemoteStoreError) as ctx:
            repo.save_review("2026-08-21", {"pe_sh": 1})
        self.assertEqual(ctx.exception.code, "PENDING_UNREADABLE")
        self.assertEqual(transport.calls, [])

    def test_failed_preimage_save_does_not_send_the_write(self) -> None:
        transport = FakeTransport(
            {
                "marketreview_replace_direction_preimage": _preimage(),
                "marketreview_replace_direction": _envelope(noop=False, revision=3),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _cloud_repo(transport, root)
            with patch(
                "marketreview.supabase_store.save_open_pending",
                side_effect=RemoteStoreError("disk", code="PENDING_UNREADABLE"),
            ):
                with self.assertRaises(RemoteStoreError) as ctx:
                    repo.replace_price_limit_event_direction(
                        "2026-08-21",
                        "sh",
                        "600519",
                        "up",
                        PriceLimitEventInput("sh", "600519", "贵州茅台", "down", True, 1000, 0),
                    )
            self.assertEqual(ctx.exception.code, "PENDING_UNREADABLE")
            self.assertIsNone(read_pending(root / "state"))
        self.assertEqual([call[0] for call in transport.calls], ["marketreview_replace_direction_preimage"])

    def test_open_or_corrupt_pending_blocks_later_writes_but_not_reads(self) -> None:
        preimage = _preimage()
        transport = FakeTransport(
            {
                "marketreview_get_day": _envelope(
                    review=None,
                    events=[],
                    details=[],
                    sectors=[],
                    reasons=[],
                    previous_events=[],
                    counts={
                        "reviews": 0,
                        "events": 0,
                        "details": 0,
                        "sectors": 0,
                        "reasons": 0,
                        "previous_events": 0,
                    },
                ),
                "marketreview_replace_direction_preimage": preimage,
                "marketreview_replace_direction": RemoteStoreError("timed out", code="REMOTE_RESULT_UNKNOWN"),
                "marketreview_probe": _envelope(revision=1),
                "marketreview_save_review": _envelope(noop=False, revision=2),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _cloud_repo(transport, root)
            with self.assertRaises(RemoteStoreError) as timed_out:
                repo.replace_price_limit_event_direction(
                    "2026-08-21",
                    "sh",
                    "600519",
                    "up",
                    PriceLimitEventInput("sh", "600519", "贵州茅台", "down", True, 1000, 0),
                )
            self.assertEqual(timed_out.exception.code, "REMOTE_RESULT_UNKNOWN")
            record = read_pending(root / "state")
            assert record is not None
            self.assertEqual(record["status"], "open")
            calls_after_timeout = len(transport.calls)
            with self.assertRaises(RemoteStoreError) as blocked:
                repo.save_review("2026-08-21", {"pe_sh": 1})
            self.assertEqual(blocked.exception.code, "PENDING_WRITE")
            self.assertEqual(len(transport.calls), calls_after_timeout)
            repo.read_day("2026-08-21", "2026-08-20")
            self.assertEqual(transport.calls[-1][0], "marketreview_get_day")
            (root / "state" / "pending-write.json").write_text("{", encoding="utf-8")
            with self.assertRaises(RemoteStoreError) as corrupt:
                repo.delete_review("2026-08-21")
            self.assertEqual(corrupt.exception.code, "PENDING_UNREADABLE")
            self.assertEqual(transport.calls[-1][0], "marketreview_get_day")

    def test_definite_rejection_closes_the_pending_record(self) -> None:
        transport = FakeTransport(
            {
                "marketreview_replace_direction_preimage": _preimage(),
                "marketreview_replace_direction": RemoteStoreError(
                    "目标方向已存在",
                    code="REPLACE_TARGET_EXISTS",
                ),
                "marketreview_probe": _envelope(revision=4),
                "marketreview_save_review": _envelope(noop=False, revision=5),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _cloud_repo(transport, root)
            with self.assertRaises(RemoteStoreError) as ctx:
                repo.replace_price_limit_event_direction(
                    "2026-08-21",
                    "sh",
                    "600519",
                    "up",
                    PriceLimitEventInput("sh", "600519", "贵州茅台", "down", False, 1000, 0),
                )
            self.assertEqual(ctx.exception.code, "REPLACE_TARGET_EXISTS")
            record = read_pending(root / "state")
            assert record is not None
            self.assertEqual(record["status"], "closed")
            self.assertEqual(record["result"], "rejected")
            repo.save_review("2026-08-21", {"pe_sh": 1})
        self.assertIn("marketreview_save_review", [call[0] for call in transport.calls])

    def test_verify_matching_preimage_within_window_does_not_retry_or_close(self) -> None:
        preimage = _preimage()
        transport = FakeTransport({"marketreview_replace_direction_preimage": preimage})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _cloud_repo(transport, root)
            (root / "state").mkdir()
            from marketreview.write_gate import save_open_pending

            save_open_pending(root / "state", _open_replace_record(preimage))
            with self.assertRaises(RemoteStoreError) as ctx:
                repo.verify_pending_replace()
            self.assertEqual(ctx.exception.code, "REMOTE_RESULT_UNKNOWN")
            self.assertIn("不能排除原请求仍在途", str(ctx.exception))
            self.assertIn("不能重发", str(ctx.exception))
            record = read_pending(root / "state")
            assert record is not None
            self.assertEqual(record["status"], "open")
        self.assertEqual(len(transport.calls), 1)
        self.assertFalse(transport.calls[0][2])

    def test_verify_matching_preimage_after_timeout_window_resends(self) -> None:
        preimage = _preimage()
        transport = FakeTransport(
            {
                "marketreview_replace_direction_preimage": preimage,
                "marketreview_replace_direction": _envelope(noop=False, revision=5),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # started_at = BATCH; clock is 9s later → past the 8s Data API bound.
            repo = SupabaseRepository(
                transport,
                clock=lambda: "2026-08-21T00:00:09+00:00",
                state_dir=root / "state",
                project_ref="example.supabase.co",
            )
            (root / "state").mkdir()
            from marketreview.write_gate import save_open_pending

            save_open_pending(root / "state", _open_replace_record(preimage))
            repo.verify_pending_replace()
            record = read_pending(root / "state")
            assert record is not None
            self.assertEqual(record["status"], "closed")
            self.assertEqual(record["result"], "confirmed")
            history = [item["result"] for item in record["verification_history"]]
            self.assertEqual(history, ["not_executed", "confirmed"])
            self.assertEqual(record["payload"]["batch_time"], BATCH)
            self.assertEqual(record["started_at"], "2026-08-21T00:00:09+00:00")
            self.assertTrue(same_json_value(record["preimage"], preimage))
        self.assertEqual(
            [call[0] for call in transport.calls],
            ["marketreview_replace_direction_preimage", "marketreview_replace_direction"],
        )
        self.assertFalse(transport.calls[0][2])
        self.assertTrue(transport.calls[1][2])
        self.assertEqual(transport.calls[1][1]["batch_time"], BATCH)

    def test_verify_resend_close_failure_does_not_send(self) -> None:
        preimage = _preimage()
        transport = FakeTransport(
            {
                "marketreview_replace_direction_preimage": preimage,
                "marketreview_replace_direction": _envelope(noop=False, revision=5),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = SupabaseRepository(
                transport,
                clock=lambda: "2026-08-21T00:00:09+00:00",
                state_dir=root / "state",
                project_ref="example.supabase.co",
            )
            (root / "state").mkdir()
            from marketreview.write_gate import save_open_pending

            save_open_pending(root / "state", _open_replace_record(preimage))
            with patch(
                "marketreview.supabase_store.close_pending",
                side_effect=RemoteStoreError("disk", code="PENDING_UNREADABLE"),
            ):
                with self.assertRaises(RemoteStoreError) as ctx:
                    repo.verify_pending_replace()
            self.assertEqual(ctx.exception.code, "PENDING_UNREADABLE")
            self.assertIn("未能关闭", str(ctx.exception))
            record = read_pending(root / "state")
            assert record is not None
            self.assertEqual(record["status"], "open")
        self.assertEqual(
            [call[0] for call in transport.calls],
            ["marketreview_replace_direction_preimage"],
        )

    def test_verify_resend_reopen_failure_does_not_send(self) -> None:
        preimage = _preimage()
        transport = FakeTransport(
            {
                "marketreview_replace_direction_preimage": preimage,
                "marketreview_replace_direction": _envelope(noop=False, revision=5),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = SupabaseRepository(
                transport,
                clock=lambda: "2026-08-21T00:00:09+00:00",
                state_dir=root / "state",
                project_ref="example.supabase.co",
            )
            (root / "state").mkdir()
            from marketreview.write_gate import save_open_pending

            save_open_pending(root / "state", _open_replace_record(preimage))
            real_close = __import__("marketreview.write_gate", fromlist=["close_pending"]).close_pending
            calls = {"n": 0}

            def close_once(state_dir: Path, *, result: str) -> None:
                calls["n"] += 1
                if calls["n"] == 1:
                    real_close(state_dir, result=result)
                    return
                raise AssertionError("unexpected second close before send")

            with patch("marketreview.supabase_store.close_pending", side_effect=close_once):
                with patch(
                    "marketreview.supabase_store.save_open_pending",
                    side_effect=RemoteStoreError("disk", code="PENDING_UNREADABLE"),
                ):
                    with self.assertRaises(RemoteStoreError) as ctx:
                        repo.verify_pending_replace()
            self.assertEqual(ctx.exception.code, "PENDING_UNREADABLE")
            self.assertIn("重新持久化未关闭状态失败", str(ctx.exception))
            record = read_pending(root / "state")
            assert record is not None
            self.assertEqual(record["status"], "closed")
            self.assertEqual(record["result"], "not_executed")
        self.assertEqual(
            [call[0] for call in transport.calls],
            ["marketreview_replace_direction_preimage"],
        )

    def test_write_lock_keeps_a_second_write_waiting(self) -> None:
        release = threading.Event()
        entered = threading.Event()

        class BlockingTransport(FakeTransport):
            def call(self, function: str, request: dict[str, Any], *, write: bool) -> dict[str, Any]:
                if function == "marketreview_replace_direction_preimage":
                    entered.set()
                    self.assert_release()
                return super().call(function, request, write=write)

            def assert_release(self) -> None:
                release.wait(2)

        transport = BlockingTransport(
            {
                "marketreview_replace_direction_preimage": _preimage(),
                "marketreview_replace_direction": _envelope(noop=False, revision=3),
                "marketreview_probe": _envelope(revision=1),
                "marketreview_save_review": _envelope(noop=False, revision=2),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _cloud_repo(transport, root)
            second = _cloud_repo(transport, root)
            holder: list[BaseException | str] = []

            def run_replace() -> None:
                try:
                    first.replace_price_limit_event_direction(
                        "2026-08-21",
                        "sh",
                        "600519",
                        "up",
                        PriceLimitEventInput("sh", "600519", "贵州茅台", "down", False, 1000, 0),
                    )
                    holder.append("replaced")
                except BaseException as exc:  # noqa: BLE001
                    holder.append(exc)

            thread = threading.Thread(target=run_replace, daemon=True)
            thread.start()
            self.assertTrue(entered.wait(2))
            blocked: list[BaseException | str] = []

            def run_save() -> None:
                try:
                    second.save_review("2026-08-21", {"pe_sh": 1})
                    blocked.append("saved")
                except BaseException as exc:  # noqa: BLE001
                    blocked.append(exc)

            other = threading.Thread(target=run_save, daemon=True)
            try:
                other.start()
                other.join(0.3)
                self.assertTrue(other.is_alive())
            finally:
                release.set()
                thread.join(2)
                other.join(2)
            self.assertEqual(holder, ["replaced"])
            self.assertEqual(blocked, ["saved"])

    def test_incomplete_preimage_does_not_send_the_write(self) -> None:
        transport = FakeTransport(
            {
                "marketreview_replace_direction_preimage": _envelope(
                    revision=1,
                    old={"exists": True},
                    new={"exists": False},
                ),
                "marketreview_replace_direction": _envelope(noop=False, revision=3),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _cloud_repo(transport, root)
            with self.assertRaises(RemoteStoreError) as ctx:
                repo.replace_price_limit_event_direction(
                    "2026-08-21",
                    "sh",
                    "600519",
                    "up",
                    PriceLimitEventInput("sh", "600519", "贵州茅台", "down", False, 1000, 0),
                )
            self.assertEqual(ctx.exception.code, "INCOMPLETE_RESPONSE")
            self.assertIsNone(read_pending(root / "state"))
        self.assertEqual([call[0] for call in transport.calls], ["marketreview_replace_direction_preimage"])

    def test_version_mismatch_response_keeps_the_gate_closed_for_new_writes(self) -> None:
        transport = FakeTransport(
            {
                "marketreview_replace_direction_preimage": _preimage(),
                "marketreview_replace_direction": _envelope(schema_version=2, noop=False, revision=9),
                "marketreview_probe": _envelope(revision=1),
                "marketreview_save_review": _envelope(noop=False, revision=2),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _cloud_repo(transport, root)
            with self.assertRaises(RemoteStoreError) as ctx:
                repo.replace_price_limit_event_direction(
                    "2026-08-21",
                    "sh",
                    "600519",
                    "up",
                    PriceLimitEventInput("sh", "600519", "贵州茅台", "down", False, 1000, 0),
                )
            self.assertEqual(ctx.exception.code, "REMOTE_RESULT_UNKNOWN")
            record = read_pending(root / "state")
            assert record is not None
            self.assertEqual(record["status"], "open")
            with self.assertRaises(RemoteStoreError) as blocked:
                repo.save_review("2026-08-22", {"pe_sh": 1})
            self.assertEqual(blocked.exception.code, "PENDING_WRITE")
        self.assertNotIn("marketreview_save_review", [call[0] for call in transport.calls])

    def test_verified_success_closes_pending_without_resending(self) -> None:
        saved = _preimage()
        current = _preimage(
            revision=5,
            old=_direction_absent(),
            new=_direction_present(
                "down",
                closed_at_limit=False,
                limit_rate_bp=1000,
                streak_height=0,
                created_at=BATCH,
                updated_at=BATCH,
            ),
        )
        transport = FakeTransport(
            {
                "marketreview_replace_direction_preimage": current,
                "marketreview_probe": _envelope(revision=5),
                "marketreview_save_review": _envelope(noop=False, revision=6),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _cloud_repo(transport, root)
            (root / "state").mkdir()
            from marketreview.write_gate import save_open_pending

            save_open_pending(root / "state", _open_replace_record(saved))
            repo.verify_pending_replace()
            record = read_pending(root / "state")
            assert record is not None
            self.assertEqual(record["status"], "closed")
            self.assertEqual(record["result"], "confirmed")
            repo.save_review("2026-08-22", {"pe_sh": 1})
        self.assertEqual(
            [call[0] for call in transport.calls],
            ["marketreview_replace_direction_preimage", "marketreview_probe", "marketreview_save_review"],
        )
        self.assertFalse(transport.calls[0][2])

    def test_orphan_sector_and_gapped_position_are_rejected(self) -> None:
        transport = FakeTransport(
            {
                "marketreview_get_day": _envelope(
                    review=None,
                    events=[],
                    details=[],
                    sectors=[
                        {
                            "trade_date": "2026-08-21",
                            "market": "sh",
                            "code": "600519",
                            "direction": "up",
                            "position": 5,
                            "value": "白酒",
                        }
                    ],
                    reasons=[],
                    previous_events=[],
                    counts={
                        "reviews": 0,
                        "events": 0,
                        "details": 0,
                        "sectors": 1,
                        "reasons": 0,
                        "previous_events": 0,
                    },
                )
            }
        )
        with self.assertRaises(RemoteStoreError) as ctx:
            SupabaseRepository(transport).read_day("2026-08-21", "2026-08-20")
        self.assertEqual(ctx.exception.code, "INCOMPLETE_RESPONSE")
        self.assertIn("父事件", str(ctx.exception))

    def test_write_response_over_limit_is_unknown(self) -> None:
        class _Body:
            def read(self, _size: int) -> bytes:
                return b"x" * (MAX_BODY_BYTES + 1)

        with self.assertRaises(RemoteStoreError) as ctx:
            _read_limited(_Body(), "secret", write=True)
        self.assertEqual(ctx.exception.code, "REMOTE_RESULT_UNKNOWN")

    def test_closed_stub_cannot_bypass_the_gate(self) -> None:
        transport = FakeTransport(
            {
                "marketreview_probe": _envelope(revision=1),
                "marketreview_save_review": _envelope(noop=False, revision=2),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            (state / "pending-write.json").write_text('{"status":"closed"}', encoding="utf-8")
            repo = _cloud_repo(transport, root)
            with self.assertRaises(RemoteStoreError) as ctx:
                repo.save_review("2026-08-21", {"pe_sh": 1})
            self.assertEqual(ctx.exception.code, "PENDING_UNREADABLE")
        self.assertEqual(transport.calls, [])

    def test_closed_record_with_empty_preimage_blocks_writes(self) -> None:
        transport = FakeTransport(
            {
                "marketreview_probe": _envelope(revision=1),
                "marketreview_save_review": _envelope(noop=False, revision=2),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            record = _open_replace_record()
            record["status"] = "closed"
            record["result"] = "confirmed"
            record["verification_history"] = [{"result": "confirmed"}]
            record["preimage"] = {}
            (state / "pending-write.json").write_text(
                json.dumps(record, ensure_ascii=False),
                encoding="utf-8",
            )
            repo = _cloud_repo(transport, root)
            with self.assertRaises(RemoteStoreError) as ctx:
                repo.save_review("2026-08-21", {"pe_sh": 1})
            self.assertEqual(ctx.exception.code, "PENDING_UNREADABLE")
        self.assertEqual(transport.calls, [])

    def test_closed_record_with_empty_nested_states_blocks_writes(self) -> None:
        transport = FakeTransport(
            {
                "marketreview_probe": _envelope(revision=1),
                "marketreview_save_review": _envelope(noop=False, revision=2),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            record = _open_replace_record()
            record["status"] = "closed"
            record["result"] = "confirmed"
            record["verification_history"] = [{"result": "confirmed"}]
            record["preimage"]["old"] = {}
            record["preimage"]["new"] = {}
            (state / "pending-write.json").write_text(
                json.dumps(record, ensure_ascii=False),
                encoding="utf-8",
            )
            repo = _cloud_repo(transport, root)
            with self.assertRaises(RemoteStoreError) as ctx:
                repo.save_review("2026-08-21", {"pe_sh": 1})
            self.assertEqual(ctx.exception.code, "PENDING_UNREADABLE")
        self.assertEqual(transport.calls, [])

    def test_verify_on_other_project_does_not_close_pending(self) -> None:
        current = _preimage(
            revision=5,
            old=_direction_absent(),
            new=_direction_present(
                "down",
                closed_at_limit=False,
                limit_rate_bp=1000,
                streak_height=0,
                created_at=BATCH,
                updated_at=BATCH,
            ),
        )
        transport = FakeTransport({"marketreview_replace_direction_preimage": current})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state").mkdir()
            from marketreview.write_gate import save_open_pending

            foreign = _open_replace_record()
            foreign["project_ref"] = "other.supabase.co"
            save_open_pending(root / "state", foreign)
            repo = _cloud_repo(transport, root)
            with self.assertRaises(RemoteStoreError) as ctx:
                repo.verify_pending_replace()
            self.assertEqual(ctx.exception.code, "IDENTITY_MISMATCH")
            record = read_pending(root / "state")
            assert record is not None
            self.assertEqual(record["status"], "open")
            self.assertEqual(record["project_ref"], "other.supabase.co")
        self.assertEqual(transport.calls, [])

    def test_preimage_identity_mismatch_does_not_send_the_write(self) -> None:
        transport = FakeTransport(
            {
                "marketreview_replace_direction_preimage": _preimage(
                    trade_date="2026-08-20",
                    market="sz",
                    code="000001",
                    old=_direction_present(trade_date="2026-08-20", market="sz", code="000001"),
                ),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _cloud_repo(transport, root)
            with self.assertRaises(RemoteStoreError) as ctx:
                repo.replace_price_limit_event_direction(
                    "2026-08-21",
                    "sh",
                    "600519",
                    "up",
                    {
                        "market": "sh",
                        "code": "600519",
                        "name": "贵州茅台",
                        "direction": "down",
                        "closed_at_limit": False,
                        "limit_rate_bp": 1000,
                        "streak_height": 0,
                    },
                )
            self.assertEqual(ctx.exception.code, "INCOMPLETE_RESPONSE")
            self.assertIsNone(read_pending(root / "state"))
        self.assertEqual(
            [call[0] for call in transport.calls],
            ["marketreview_replace_direction_preimage"],
        )
        self.assertFalse(transport.calls[0][2])

    def test_preimage_detail_identity_mismatch_does_not_send_the_write(self) -> None:
        transport = FakeTransport(
            {
                "marketreview_replace_direction_preimage": _preimage(
                    old=_direction_present(
                        detail=_detail(
                            trade_date="2026-08-20",
                            market="sz",
                            code="000001",
                            direction="down",
                        ),
                    ),
                ),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _cloud_repo(transport, root)
            with self.assertRaises(RemoteStoreError) as ctx:
                repo.replace_price_limit_event_direction(
                    "2026-08-21",
                    "sh",
                    "600519",
                    "up",
                    {
                        "market": "sh",
                        "code": "600519",
                        "name": "贵州茅台",
                        "direction": "down",
                        "closed_at_limit": False,
                        "limit_rate_bp": 1000,
                        "streak_height": 0,
                    },
                )
            self.assertEqual(ctx.exception.code, "INCOMPLETE_RESPONSE")
            self.assertIsNone(read_pending(root / "state"))
        self.assertEqual(
            [call[0] for call in transport.calls],
            ["marketreview_replace_direction_preimage"],
        )
        self.assertFalse(transport.calls[0][2])

    def test_float_tolerance_does_not_relax_integers(self) -> None:
        self.assertTrue(same_json_value(1.0, 1.0 + 1e-15))
        self.assertFalse(same_json_value(1, 1.0))
        self.assertFalse(same_json_value(True, 1))


_SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")

_HOLD_LOCK = """
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, os.environ["MR_SCRIPTS"])
from marketreview.write_gate import close_pending, exclusive_write, save_open_pending
state, ready, release, record_path = map(Path, sys.argv[1:])
record = json.loads(record_path.read_text(encoding="utf-8"))
with exclusive_write(state):
    save_open_pending(state, record)
    close_pending(state, result="rejected")
    ready.write_text("ready", encoding="utf-8")
    while not release.exists():
        time.sleep(0.02)
    save_open_pending(state, record)
"""

_ENTER_LOCK = """
import os, sys
from pathlib import Path
sys.path.insert(0, os.environ["MR_SCRIPTS"])
from marketreview.errors import RemoteStoreError
from marketreview.write_gate import assert_no_open_pending, exclusive_write
state, entered, result = map(Path, sys.argv[1:])
try:
    with exclusive_write(state):
        entered.write_text("in", encoding="utf-8")
        assert_no_open_pending(state)
        result.write_text("sent", encoding="utf-8")
except RemoteStoreError as exc:
    result.write_text(exc.code, encoding="utf-8")
"""

_EXIT_HOLDING_LOCK = """
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["MR_SCRIPTS"])
from marketreview.write_gate import exclusive_write, save_open_pending
state, record_path = map(Path, sys.argv[1:])
record = json.loads(record_path.read_text(encoding="utf-8"))
with exclusive_write(state):
    save_open_pending(state, record)
    os._exit(0)
"""


class TestWriteGateProcesses(unittest.TestCase):
    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["MR_SCRIPTS"] = _SCRIPTS
        return env

    def test_second_process_cannot_write_between_close_and_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            ready = root / "ready"
            release = root / "release"
            entered = root / "entered"
            result = root / "result"
            record_path = root / "record.json"
            record_path.write_text(json.dumps(_open_replace_record()), encoding="utf-8")
            holder = subprocess.Popen(
                [sys.executable, "-c", _HOLD_LOCK, str(state), str(ready), str(release), str(record_path)],
                env=self._env(),
                stderr=subprocess.PIPE,
                text=True,
            )
            checker: subprocess.Popen[str] | None = None
            try:
                deadline = time.time() + 5
                while not ready.exists():
                    if holder.poll() is not None:
                        detail = holder.stderr.read() if holder.stderr else ""
                        self.fail(detail or "holder exited")
                    if time.time() > deadline:
                        self.fail("holder did not reach the close/reopen sync point")
                    time.sleep(0.02)
                checker = subprocess.Popen(
                    [sys.executable, "-c", _ENTER_LOCK, str(state), str(entered), str(result)],
                    env=self._env(),
                    stderr=subprocess.PIPE,
                    text=True,
                )
                time.sleep(0.4)
                self.assertFalse(entered.exists())
                release.write_text("go", encoding="utf-8")
                self.assertEqual(holder.wait(timeout=5), 0)
                self.assertEqual(checker.wait(timeout=5), 0)
            finally:
                if holder.poll() is None:
                    holder.kill()
                if holder.stderr is not None:
                    holder.stderr.close()
                if checker is not None and checker.stderr is not None:
                    checker.stderr.close()
            self.assertEqual(result.read_text(encoding="utf-8"), "PENDING_WRITE")
            reopened = read_pending(state)
            assert reopened is not None
            self.assertEqual(reopened["status"], "open")
            self.assertEqual(reopened["operation_id"], "replace-1")

    def test_process_exit_releases_the_lock_and_keeps_the_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            record_path = root / "record.json"
            result = root / "result"
            entered = root / "entered"
            record_path.write_text(json.dumps(_open_replace_record()), encoding="utf-8")
            exited = subprocess.run(
                [sys.executable, "-c", _EXIT_HOLDING_LOCK, str(state), str(record_path)],
                env=self._env(),
                check=False,
                timeout=5,
            )
            self.assertEqual(exited.returncode, 0)
            started = time.time()
            checker = subprocess.run(
                [sys.executable, "-c", _ENTER_LOCK, str(state), str(entered), str(result)],
                env=self._env(),
                check=False,
                timeout=5,
            )
            self.assertLess(time.time() - started, 4)
            self.assertEqual(checker.returncode, 0)
            self.assertEqual(result.read_text(encoding="utf-8"), "PENDING_WRITE")
            record = read_pending(state)
            assert record is not None
            self.assertEqual(record["status"], "open")
            self.assertEqual(record["operation_id"], "replace-1")


if __name__ == "__main__":
    unittest.main()
