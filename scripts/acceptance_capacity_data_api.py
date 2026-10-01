#!/usr/bin/env python3
"""#8 线上容量验收：隔离 ≥1201 事件经真实 Data API + 产品适配器读取。

写入走管理连接（Session pooler / psql），避免 Data API 8s 写超时；
验收读取必须走 UrllibRpcTransport + SupabaseRepository。

哨兵日固定为 2099-08-01；写入／清理在触库前拒绝任何其他日期。
完整脱敏证据写入 ~/.marketreview/acceptance-evidence/<stamp>_issue8_capacity/。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from marketreview.backend import default_config_dir, load_supabase_settings  # noqa: E402
from marketreview.pg_backup import PgBackupError, connection_from_env, resolve_pg_bin  # noqa: E402
from marketreview.supabase_store import (  # noqa: E402
    SCHEMA_VERSION,
    SupabaseRepository,
    UrllibRpcTransport,
)

CAPACITY_DAY = "2099-08-01"
EXPECTED_N = 1201
DEFAULT_HOST = "aws-0-ap-southeast-1.pooler.supabase.com"
DEFAULT_USER = "postgres.nyscgdxrctwchbzclszt"


class CapacityDayError(ValueError):
    """Raised when a non-sentinel trade date would reach management SQL."""


def assert_capacity_day(trade_date: str) -> str:
    """Only CAPACITY_DAY may proceed to insert/delete. No DB I/O."""
    if trade_date != CAPACITY_DAY:
        raise CapacityDayError(
            f"容量验收只允许哨兵日 {CAPACITY_DAY}，拒绝：{trade_date!r}"
        )
    return CAPACITY_DAY


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Issue #8 Data API 容量验收")
    parser.add_argument("--count", type=int, default=EXPECTED_N)
    parser.add_argument(
        "--trade-date",
        default=CAPACITY_DAY,
        help=f"仅允许 {CAPACITY_DAY}；其他日期在触库前拒绝",
    )
    parser.add_argument("--keep", action="store_true", help="读完后不清理哨兵行")
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=None,
        help="证据输出目录；默认 ~/.marketreview/acceptance-evidence/<stamp>_issue8_capacity",
    )
    args = parser.parse_args(argv)
    if args.count < 1001:
        raise SystemExit("--count 必须大于 1000")
    try:
        trade_date = assert_capacity_day(args.trade_date)
    except CapacityDayError as exc:
        raise SystemExit(str(exc)) from exc

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = args.evidence_dir or (
        Path.home() / ".marketreview" / "acceptance-evidence" / f"{stamp}_issue8_capacity"
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)

    expected_keys = [
        [trade_date, "sh", f"{index:06d}", "up"] for index in range(1, args.count + 1)
    ]
    _write_json(evidence_dir / "expected_keys.json", expected_keys)

    inserted = _insert_events(trade_date, args.count)
    settings = load_supabase_settings(default_config_dir())
    transport = UrllibRpcTransport(settings, timeout=60.0)
    repo = SupabaseRepository(transport)

    t0 = time.perf_counter()
    raw = transport.call(
        "marketreview_list_events",
        {"schema_version": SCHEMA_VERSION},
        write=False,
    )
    raw_ms = (time.perf_counter() - t0) * 1000
    _write_json(evidence_dir / "raw_list_events.json", raw)

    t0 = time.perf_counter()
    listed = repo.list_price_limit_events()
    list_ms = (time.perf_counter() - t0) * 1000
    adapter_events = [_event_dict(item) for item in listed]
    _write_json(evidence_dir / "adapter_list_events.json", adapter_events)

    t0 = time.perf_counter()
    ranged = repo.list_price_limit_events(trade_date, trade_date)
    range_ms = (time.perf_counter() - t0) * 1000
    adapter_range = [_event_dict(item) for item in ranged]
    _write_json(evidence_dir / "adapter_list_events_range.json", adapter_range)

    t0 = time.perf_counter()
    day = repo.read_day(trade_date, None)
    day_ms = (time.perf_counter() - t0) * 1000
    adapter_day = {
        "trade_date": day.trade_date,
        "review": None if day.review is None else day.review.__dict__,
        "events": [_event_dict(item) for item in day.events],
        "details": [item.__dict__ for item in day.details],
        "previous_events": [_event_dict(item) for item in day.previous_events],
    }
    _write_json(evidence_dir / "adapter_get_day.json", adapter_day)

    raw_events = raw.get("events") if isinstance(raw.get("events"), list) else []
    raw_keys = [_key(item) for item in raw_events]
    adapter_keys = [
        [item["trade_date"], item["market"], item["code"], item["direction"]]
        for item in adapter_events
    ]
    day_keys = [
        [item["trade_date"], item["market"], item["code"], item["direction"]]
        for item in adapter_day["events"]
    ]
    _write_json(evidence_dir / "raw_business_keys.json", raw_keys)
    _write_json(evidence_dir / "adapter_business_keys.json", adapter_keys)

    capacity_only_raw = [key for key in raw_keys if key[0] == trade_date]
    capacity_only_adapter = [key for key in adapter_keys if key[0] == trade_date]

    checks = {
        "inserted": inserted,
        "raw_complete": raw.get("complete") is True,
        "raw_counts_events": (raw.get("counts") or {}).get("events"),
        "raw_len": len(raw_events),
        "raw_capacity_len": len(capacity_only_raw),
        "adapter_len": len(adapter_events),
        "adapter_capacity_len": len(capacity_only_adapter),
        "range_len": len(adapter_range),
        "get_day_len": len(adapter_day["events"]),
        "raw_keys_match_expected_capacity": capacity_only_raw == expected_keys,
        "adapter_keys_match_expected_capacity": capacity_only_adapter == expected_keys,
        "day_keys_match_expected": day_keys == expected_keys,
        "raw_sorted": raw_keys == sorted(raw_keys),
        "adapter_sorted": adapter_keys == sorted(adapter_keys),
        "raw_unique": len(raw_keys) == len({tuple(key) for key in raw_keys}),
        "adapter_unique": len(adapter_keys) == len({tuple(key) for key in adapter_keys}),
        "no_truncation_at_1000": len(capacity_only_adapter) == args.count and args.count > 1000,
        "elapsed_ms": {
            "raw_list_events": round(raw_ms, 2),
            "adapter_list_events": round(list_ms, 2),
            "adapter_list_events_range": round(range_ms, 2),
            "adapter_get_day": round(day_ms, 2),
        },
    }
    checks["overall_passed"] = all(
        [
            checks["raw_complete"],
            checks["raw_capacity_len"] == args.count,
            checks["adapter_capacity_len"] == args.count,
            checks["range_len"] == args.count,
            checks["get_day_len"] == args.count,
            checks["raw_keys_match_expected_capacity"],
            checks["adapter_keys_match_expected_capacity"],
            checks["day_keys_match_expected"],
            checks["raw_sorted"],
            checks["adapter_sorted"],
            checks["raw_unique"],
            checks["adapter_unique"],
            checks["no_truncation_at_1000"],
        ]
    )

    cleanup = {"skipped": True}
    if not args.keep:
        deleted = _delete_events(trade_date)
        post_raw = transport.call(
            "marketreview_list_events",
            {
                "schema_version": SCHEMA_VERSION,
                "start_date": trade_date,
                "end_date": trade_date,
            },
            write=False,
        )
        cleanup = {
            "skipped": False,
            "deleted": deleted,
            "post_raw_counts": post_raw.get("counts"),
            "post_raw_len": len(post_raw.get("events") or []),
        }

    meta = {
        "acceptance": "#8 Data API capacity",
        "project_ref": "nyscgdxrctwchbzclszt",
        "sentinel_trade_date": trade_date,
        "expected_events": args.count,
        "read_path": "UrllibRpcTransport + SupabaseRepository",
        "write_path": "Session pooler management SQL (not Data API)",
        "cloud_default_enabled_touched": False,
        "evidence_dir": str(evidence_dir),
        "checks": checks,
        "cleanup": cleanup,
        "note": "完整响应见 raw_list_events.json；完整业务键见 *_business_keys.json；"
        "审查可用 expected_keys.json 独立重算排序/唯一/完整性。",
    }
    _write_json(evidence_dir / "summary.json", meta)
    checksums = _checksums(evidence_dir)
    _write_json(evidence_dir / "CHECKSUMS.json", checksums)
    meta["files"] = sorted(checksums)
    meta["checksums_file"] = "CHECKSUMS.json"
    _write_json(evidence_dir / "summary.json", meta)
    # summary 变更后重算校验和，使 CHECKSUMS 与最终 summary 绑定。
    checksums = _checksums(evidence_dir)
    _write_json(evidence_dir / "CHECKSUMS.json", checksums)

    sys.stdout.write(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.write(f"EVIDENCE_DIR={evidence_dir}\n")
    return 0 if checks["overall_passed"] else 1


def _event_dict(item) -> dict:
    return {
        "trade_date": item.trade_date,
        "market": item.market,
        "code": item.code,
        "name": item.name,
        "direction": item.direction,
        "closed_at_limit": item.closed_at_limit,
        "limit_rate_bp": item.limit_rate_bp,
        "streak_height": item.streak_height,
    }


def _key(item: dict) -> list[str]:
    return [item["trade_date"], item["market"], item["code"], item["direction"]]


def _insert_events(trade_date: str, count: int) -> int:
    day = assert_capacity_day(trade_date)
    # SQL 只嵌入常量 CAPACITY_DAY，避免参数绕过后写进真实交易日。
    sql = f"""
    DELETE FROM marketreview.daily_price_limit_event WHERE trade_date = '{CAPACITY_DAY}';
    INSERT INTO marketreview.daily_price_limit_event (
      trade_date, market, code, name, direction, closed_at_limit,
      limit_rate_bp, streak_height, created_at, updated_at
    )
    SELECT
      '{CAPACITY_DAY}',
      'sh',
      lpad(gs::text, 6, '0'),
      '容量验收' || gs::text,
      'up',
      1,
      1000,
      1,
      '{CAPACITY_DAY}T00:00:00+00:00',
      '{CAPACITY_DAY}T00:00:00+00:00'
    FROM generate_series(1, {int(count)}) AS gs;
    SELECT count(*) FROM marketreview.daily_price_limit_event WHERE trade_date = '{CAPACITY_DAY}';
    """
    assert day == CAPACITY_DAY
    out = _psql(sql)
    return int(out.strip().splitlines()[-1])


def _delete_events(trade_date: str) -> int:
    day = assert_capacity_day(trade_date)
    sql = f"""
    WITH deleted AS (
      DELETE FROM marketreview.daily_price_limit_event
      WHERE trade_date = '{CAPACITY_DAY}'
      RETURNING 1
    )
    SELECT count(*) FROM deleted;
    """
    assert day == CAPACITY_DAY
    return int(_psql(sql).strip())


def _psql(sql: str) -> str:
    password = _db_password()
    # 管理写入固定走 Session pooler，避免继承本机 restore 留下的 PGHOST=127.0.0.1。
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    env["PGHOST"] = DEFAULT_HOST
    env["PGPORT"] = "5432"
    env["PGUSER"] = DEFAULT_USER
    env["PGDATABASE"] = "postgres"
    try:
        with _patched_env(env):
            conn = connection_from_env(database="postgres")
    except PgBackupError as exc:
        raise SystemExit(str(exc)) from exc
    tools = resolve_pg_bin()
    cmd = [
        str(tools / "psql"),
        "-v",
        "ON_ERROR_STOP=1",
        "-t",
        "-A",
        "-c",
        sql,
    ]
    run_env = os.environ.copy()
    run_env.update(conn.env())
    try:
        completed = subprocess.run(
            cmd, env=run_env, capture_output=True, text=True, timeout=120, check=False
        )
    finally:
        run_env.pop("PGPASSWORD", None)
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "psql failed").replace(password, "[redacted]")
        raise SystemExit(err[:500])
    return (completed.stdout or "").replace(password, "[redacted]")


class _patched_env:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self._old: dict[str, str | None] = {}

    def __enter__(self):
        for key, value in self._values.items():
            self._old[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for key, previous in self._old.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def _db_password() -> str:
    path = Path.home() / ".marketreview" / "db.password"
    if not path.is_file():
        raise SystemExit("缺少 ~/.marketreview/db.password")
    password = path.read_text(encoding="utf-8").strip()
    if not password or "\n" in password:
        raise SystemExit("数据库密码文件不合法")
    return password


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _checksums(directory: Path) -> dict[str, str]:
    import hashlib

    result: dict[str, str] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name == "CHECKSUMS.json":
            continue
        digest = hashlib.sha256()
        digest.update(path.read_bytes())
        result[path.name] = digest.hexdigest()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
