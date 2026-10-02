"""JSON CLI for daily market review persistence."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from marketreview.business_validation import default_write_guard
from marketreview.calendar import CalendarUnavailableError, TradingCalendar
from marketreview.errors import MarketReviewError
from marketreview.ladder import build_ladder, ladder_to_dict
from marketreview.service import missing_atomic_fields_of
from marketreview.paths import production_cloud_state_dir, resolve_db_path
from marketreview.storage import open_repository
from marketreview.summary import compute_summary, events_to_dict, review_to_dict
from marketreview.sync_groups import parse_group_ref
from marketreview.validation import normalize_trade_date


def _emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def _success(data: Any) -> None:
    _emit({"ok": True, "data": data, "error": None})


def _failure(error: str, *, code: str = "ERROR") -> None:
    _emit({"ok": False, "data": None, "error": {"code": code, "message": error}})


def _read_json_input(path: str) -> Any:
    if path == "-":
        raw = sys.stdin.read()
    else:
        raw = open(path, encoding="utf-8").read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def _previous_trading_day(trade_date: str) -> str | None:
    try:
        return TradingCalendar().previous_trading_day(trade_date)
    except CalendarUnavailableError:
        return None


def cmd_get(args: argparse.Namespace) -> int:
    trade_date = normalize_trade_date(args.date)
    with open_repository(
        backend=args.backend,
        db_path=args.db,
        state_dir=getattr(args, "state_dir", None),
    ) as repo:
        day = repo.read_day(trade_date, _previous_trading_day(trade_date))
        _success(
            {
                "trade_date": trade_date,
                "review": review_to_dict(day.review),
                "events": events_to_dict(day.events),
                "summary": compute_summary(day.review, day.events, day.previous_events),
                "missing_fields": missing_atomic_fields_of(day.review),
                "ladder": ladder_to_dict(build_ladder(day.events, day.details)),
            }
        )
    return 0


def cmd_save_review(args: argparse.Namespace) -> int:
    guard = default_write_guard()
    trade_date = guard.validate_write_trade_date(args.date)
    payload = _read_json_input(args.input)
    fields = payload.get("fields", payload)
    if not isinstance(fields, dict):
        _failure("save-review 输入必须是 JSON 对象或包含 fields 的对象")
        return 1
    with open_repository(
        backend=args.backend,
        db_path=args.db,
        state_dir=getattr(args, "state_dir", None),
    ) as repo:
        repo.save_review(trade_date, fields)
    _success({"trade_date": trade_date, "saved_fields": sorted(fields)})
    return 0


def cmd_save_events(args: argparse.Namespace) -> int:
    guard = default_write_guard()
    trade_date = guard.validate_write_trade_date(args.date)
    payload = _read_json_input(args.input)
    if isinstance(payload, dict) and "events" in payload:
        events = payload["events"]
    else:
        events = payload
    if not isinstance(events, list):
        _failure("save-events 输入必须是事件数组或包含 events 的对象")
        return 1
    for event in events:
        if not isinstance(event, dict):
            _failure("save-events 中每个事件必须是 JSON 对象")
            return 1
        guard.validate_price_limit_event(event)
    with open_repository(
        backend=args.backend,
        db_path=args.db,
        state_dir=getattr(args, "state_dir", None),
    ) as repo:
        repo.save_price_limit_events(trade_date, events)
    _success({"trade_date": trade_date, "saved_count": len(events)})
    return 0


def cmd_save_event_details(args: argparse.Namespace) -> int:
    guard = default_write_guard()
    trade_date = guard.validate_write_trade_date(args.date)
    payload = _read_json_input(args.input)
    if isinstance(payload, dict) and "details" in payload:
        details = payload["details"]
    else:
        details = payload
    if not isinstance(details, list):
        _failure("save-event-details 输入必须是明细数组或包含 details 的对象")
        return 1
    for detail in details:
        if not isinstance(detail, dict):
            _failure("save-event-details 中每条明细必须是 JSON 对象")
            return 1
        guard.validate_event_detail(detail)
    with open_repository(
        backend=args.backend,
        db_path=args.db,
        state_dir=getattr(args, "state_dir", None),
    ) as repo:
        repo.save_price_limit_event_details(trade_date, details)
    _success({"trade_date": trade_date, "saved_count": len(details)})
    return 0


def cmd_delete_event(args: argparse.Namespace) -> int:
    guard = default_write_guard()
    trade_date = guard.validate_write_trade_date(args.date)
    guard.validate_event_identity(args.market, args.code, args.direction)
    with open_repository(
        backend=args.backend,
        db_path=args.db,
        state_dir=getattr(args, "state_dir", None),
    ) as repo:
        repo.delete_price_limit_event(trade_date, args.market, args.code, args.direction)
    _success(
        {
            "trade_date": trade_date,
            "market": args.market,
            "code": args.code,
            "direction": args.direction,
        }
    )
    return 0


def cmd_replace_direction(args: argparse.Namespace) -> int:
    guard = default_write_guard()
    trade_date = guard.validate_write_trade_date(args.date)
    guard.validate_event_identity(args.market, args.code, args.old_direction)
    payload = _read_json_input(args.input)
    event = payload.get("event", payload)
    if not isinstance(event, dict):
        _failure("replace-direction 输入必须是事件对象或包含 event 的对象")
        return 1
    guard.validate_price_limit_event(event)
    with open_repository(
        backend=args.backend,
        db_path=args.db,
        state_dir=getattr(args, "state_dir", None),
    ) as repo:
        repo.replace_price_limit_event_direction(
            trade_date,
            args.market,
            args.code,
            args.old_direction,
            event,
        )
    _success(
        {
            "trade_date": trade_date,
            "market": args.market,
            "code": args.code,
            "old_direction": args.old_direction,
            "new_direction": event.get("direction"),
        }
    )
    return 0


def _command_state_dir():
    return production_cloud_state_dir()


def _sync_choices(args: argparse.Namespace):
    from marketreview.sync_engine import GroupChoice

    choices = []
    for action, values in (
        ("keep_cloud", args.keep_cloud),
        ("adopt_local", args.adopt_local),
        ("restore_cloud", args.restore_cloud),
        ("delete_on_cloud", args.delete_on_cloud),
    ):
        for ref in values or []:
            kind, key = parse_group_ref(ref)
            choices.append(GroupChoice(kind, key, action))
    return choices


def _sync_project(settings) -> str:
    from urllib.parse import urlparse

    host = urlparse(settings.url).hostname or ""
    return host.split(".")[0]


def _run_sync(args: argparse.Namespace, *, download: bool) -> int:
    from marketreview.backend import config_dir_from_env, load_supabase_settings
    from marketreview.supabase_store import UrllibRpcTransport
    from marketreview.sync_engine import pull_sync, push_sync

    settings = load_supabase_settings(config_dir_from_env())
    project_id = _sync_project(settings)
    explicit = args.target if download else args.source
    sqlite_path = resolve_db_path(explicit if explicit is not None else args.db)
    report = (pull_sync if download else push_sync)(
        sqlite_path=sqlite_path,
        transport=UrllibRpcTransport(settings),
        project_id=project_id,
        state_dir=args.state_dir,
        choices=_sync_choices(args),
    )
    _success(report)
    if report.get("status") in {"completed", "partial", "needs_resolution"}:
        return 0
    return 1


def cmd_sync_new_identity(args: argparse.Namespace) -> int:
    from marketreview.sqlite_schema import connect, init_db
    from marketreview.sync_ledger import ensure_sync_schema, fork_ledger_identity

    sqlite_path = resolve_db_path(args.source if args.source is not None else args.db)
    conn = connect(sqlite_path)
    try:
        ensure_sync_schema(conn)
        init_db(conn)
        ledger_id = fork_ledger_identity(conn)
    finally:
        conn.close()
    _success({"sqlite_path": str(sqlite_path), "ledger_id": ledger_id})
    return 0


def cmd_sync_push(args: argparse.Namespace) -> int:
    return _run_sync(args, download=False)


def cmd_sync_pull(args: argparse.Namespace) -> int:
    return _run_sync(args, download=True)


def cmd_verify_pending(args: argparse.Namespace) -> int:
    with open_repository(
        backend=args.backend,
        db_path=args.db,
        state_dir=getattr(args, "state_dir", None),
    ) as repo:
        verify = getattr(repo, "verify_pending_replace", None)
        if verify is None:
            _success({"status": "sqlite"})
            return 0
        verify()
        _success({"status": "clear"})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Daily market review CLI")
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite path override. Rejected when the selected backend is supabase.",
    )
    parser.add_argument(
        "--backend",
        default=None,
        help="sqlite or supabase. Overrides local config; omitted uses config, then the process default.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    get_parser = subparsers.add_parser("get", help="Read review, events, summary, and ladder")
    get_parser.add_argument("--date", required=True)
    get_parser.set_defaults(func=cmd_get)

    save_review_parser = subparsers.add_parser("save-review", help="Save or patch review fields")
    save_review_parser.add_argument("--date", required=True)
    save_review_parser.add_argument("--input", default="-")
    save_review_parser.set_defaults(func=cmd_save_review)

    save_events_parser = subparsers.add_parser("save-events", help="Save price-limit events")
    save_events_parser.add_argument("--date", required=True)
    save_events_parser.add_argument("--input", default="-")
    save_events_parser.set_defaults(func=cmd_save_events)

    save_event_details_parser = subparsers.add_parser(
        "save-event-details",
        help="Save or patch price-limit event details",
    )
    save_event_details_parser.add_argument("--date", required=True)
    save_event_details_parser.add_argument("--input", default="-")
    save_event_details_parser.set_defaults(func=cmd_save_event_details)

    delete_event_parser = subparsers.add_parser("delete-event", help="Delete one event")
    delete_event_parser.add_argument("--date", required=True)
    delete_event_parser.add_argument("--market", required=True)
    delete_event_parser.add_argument("--code", required=True)
    delete_event_parser.add_argument("--direction", required=True)
    delete_event_parser.set_defaults(func=cmd_delete_event)

    replace_direction_parser = subparsers.add_parser(
        "replace-direction",
        help="Atomically replace an event direction",
    )
    replace_direction_parser.add_argument("--date", required=True)
    replace_direction_parser.add_argument("--market", required=True)
    replace_direction_parser.add_argument("--code", required=True)
    replace_direction_parser.add_argument("--old-direction", required=True)
    replace_direction_parser.add_argument("--input", default="-")
    replace_direction_parser.set_defaults(func=cmd_replace_direction)

    verify_pending_parser = subparsers.add_parser(
        "verify-pending",
        help="Verify an open cloud direction replacement; resend only after the in-flight window",
    )
    verify_pending_parser.set_defaults(func=cmd_verify_pending)

    sync_parser = subparsers.add_parser("sync", help="Upload merge or full download")
    sync_sub = sync_parser.add_subparsers(dest="sync_command", required=True)
    for name, help_text, func in (
        ("push", "Merge the local SQLite ledger into the cloud", cmd_sync_push),
        ("pull", "Download the cloud ledger into a local SQLite file", cmd_sync_pull),
    ):
        command = sync_sub.add_parser(name, help=help_text)
        command.add_argument("--source" if name == "push" else "--target", default=None)
        command.add_argument("--keep-cloud", action="append", default=[])
        command.add_argument("--adopt-local", action="append", default=[])
        command.add_argument("--restore-cloud", action="append", default=[])
        command.add_argument("--delete-on-cloud", action="append", default=[])
        command.set_defaults(func=func)
    identity_parser = sync_sub.add_parser(
        "new-identity",
        help="Assign a new ledger id to an independent copy and recheck its baselines",
    )
    identity_parser.add_argument("--source", default=None)
    identity_parser.set_defaults(func=cmd_sync_new_identity)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.state_dir = _command_state_dir()
    try:
        return args.func(args)
    except MarketReviewError as exc:
        _failure(str(exc), code=exc.code)
        return 1
    except json.JSONDecodeError as exc:
        _failure(f"JSON 解析失败：{exc}")
        return 1
    except Exception as exc:
        _failure(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
