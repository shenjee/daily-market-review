"""JSON CLI for daily market review persistence."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Any

from marketreview.errors import MarketReviewError
from marketreview.paths import resolve_db_path
from marketreview.repository import MarketReviewRepository
from marketreview.service import missing_atomic_fields
from marketreview.summary import compute_summary, events_to_dict, review_to_dict
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


def cmd_get(args: argparse.Namespace) -> int:
    trade_date = normalize_trade_date(args.date)
    with MarketReviewRepository(args.db) as repo:
        review = repo.get_review(trade_date)
        events = repo.get_price_limit_events(trade_date)
        _success(
            {
                "trade_date": trade_date,
                "review": review_to_dict(review),
                "events": events_to_dict(events),
                "summary": compute_summary(review, events),
                "missing_fields": missing_atomic_fields(repo, trade_date),
            }
        )
    return 0


def cmd_save_review(args: argparse.Namespace) -> int:
    trade_date = normalize_trade_date(args.date)
    payload = _read_json_input(args.input)
    fields = payload.get("fields", payload)
    if not isinstance(fields, dict):
        _failure("save-review 输入必须是 JSON 对象或包含 fields 的对象")
        return 1
    with MarketReviewRepository(args.db) as repo:
        repo.save_review(trade_date, fields)
    _success({"trade_date": trade_date, "saved_fields": sorted(fields)})
    return 0


def cmd_save_events(args: argparse.Namespace) -> int:
    trade_date = normalize_trade_date(args.date)
    payload = _read_json_input(args.input)
    if isinstance(payload, dict) and "events" in payload:
        events = payload["events"]
    else:
        events = payload
    if not isinstance(events, list):
        _failure("save-events 输入必须是事件数组或包含 events 的对象")
        return 1
    with MarketReviewRepository(args.db) as repo:
        repo.save_price_limit_events(trade_date, events)
    _success({"trade_date": trade_date, "saved_count": len(events)})
    return 0


def cmd_delete_event(args: argparse.Namespace) -> int:
    trade_date = normalize_trade_date(args.date)
    with MarketReviewRepository(args.db) as repo:
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
    trade_date = normalize_trade_date(args.date)
    payload = _read_json_input(args.input)
    event = payload.get("event", payload)
    if not isinstance(event, dict):
        _failure("replace-direction 输入必须是事件对象或包含 event 的对象")
        return 1
    with MarketReviewRepository(args.db) as repo:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Daily market review CLI")
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite path override (mainly for tests)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    get_parser = subparsers.add_parser("get", help="Read review, events, summary")
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.db = resolve_db_path(args.db)
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
