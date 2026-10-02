#!/usr/bin/env python3
"""Create an online-backup consistency snapshot of a local market_review SQLite DB.

Does not modify the source DB. Writes:
  <out>/market_review.sqlite3.consistent
  <out>/inventory.json
  <out>/CHECKSUMS.json

Usage:
  python3 scripts/acceptance_sqlite_consistency_backup.py \\
    --role m1 \\
    --source ~/.marketreview/market_review.sqlite3 \\
    --out ~/.marketreview/acceptance-evidence/<id>/sqlite_consistency_m1

If the source does not exist, exits with FACT_NO_SOURCE so the contract can be
adjusted rather than substituting a drill ledger.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BUSINESS_TABLES = (
    "daily_market_review",
    "daily_price_limit_event",
    "daily_price_limit_event_detail",
    "daily_price_limit_event_reason",
    "daily_price_limit_event_sector",
)
SYNC_TABLES = (
    "sync_ledger_singleton",
    "sync_baseline",
    "sync_coverage",
    "sync_authorization",
    "sync_operation",
)
MANUAL_BACKUP_POLICY = {
    "schedule": [
        "每个有写入的交易日结束后手动导出",
        "长假前额外导出",
        "迁移前额外导出",
        "schema 升级前额外导出",
    ],
    "retention": {
        "keep_recent": 30,
        "keep_each_month_last": True,
        "migration_snapshot": "长期保存，修剪时不删除",
        "failed_export": "不覆盖上一份有效备份",
    },
    "scheduler": "没有定时任务，必须手动执行",
    "cloud_command": "python3 scripts/pg_backup.py backup --database <db> [--keep-long-term]",
    "sqlite_pre_migration": "两机原库一致性备份单独登记，不放进云端备份的修剪目录，不覆盖已有文件",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def machine_meta(role: str) -> dict[str, Any]:
    model = ""
    try:
        model = (
            subprocess.check_output(["sysctl", "-n", "hw.model"], text=True, stderr=subprocess.DEVNULL).strip()
        )
    except (OSError, subprocess.CalledProcessError):
        model = platform.machine()
    return {
        "hostname": platform.node(),
        "model": model,
        "chip": platform.processor() or platform.machine(),
        "machine": platform.machine(),
        "system": platform.system(),
        "role": role,
        "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def online_backup(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if dest.exists():
        dest.unlink()
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def sync_census(conn: sqlite3.Connection) -> dict[str, Any]:
    """Count sync metadata only. Absence is recorded; it is not a confirmed-empty baseline."""
    present = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    tables: dict[str, dict[str, Any]] = {}
    for name in SYNC_TABLES:
        if name not in present:
            tables[name] = {"present": False, "rows": 0}
            continue
        rows = int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
        tables[name] = {"present": True, "rows": rows}
    identity = tables["sync_ledger_singleton"]
    return {
        "tables": tables,
        "ledger_identity_present": bool(identity["present"] and identity["rows"]),
        "note": "只统计同步表是否存在和行数，不读取业务行或载荷。没有这些表表示原库尚未建立同步身份。",
    }


def inventory(source: Path, backup: Path, role: str) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = [dict(r) for r in conn.execute("PRAGMA foreign_key_check")]
        sync_metadata = sync_census(conn)
        row_counts = {}
        for table in BUSINESS_TABLES:
            row_counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        review_dates = [
            r[0]
            for r in conn.execute("SELECT trade_date FROM daily_market_review ORDER BY trade_date")
        ]
        event_bounds = conn.execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM daily_price_limit_event"
        ).fetchone()
    finally:
        conn.close()

    wal = Path(str(source) + "-wal")
    shm = Path(str(source) + "-shm")
    return {
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "machine": machine_meta(role),
        "source_path": str(source.resolve()),
        "source_size_bytes": source.stat().st_size,
        "source_mtime": datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "source_sha256": sha256_file(source),
        "backup_path": str(backup.resolve()),
        "backup_size_bytes": backup.stat().st_size,
        "backup_sha256": sha256_file(backup),
        "wal_present": wal.exists(),
        "shm_present": shm.exists(),
        "wal_size": wal.stat().st_size if wal.exists() else None,
        "tables": list(BUSINESS_TABLES),
        "row_counts": row_counts,
        "sync_metadata": sync_metadata,
        "integrity_check": integrity,
        "foreign_key_check": fk,
        "samples": {
            "review_dates": review_dates,
            "event_date_min": event_bounds[0],
            "event_date_max": event_bounds[1],
        },
        "method": "sqlite3.Connection.backup (online backup API)",
        "note": "Does not modify source. Not a substitute for the other machine's original DB.",
    }


def write_checksums(out: Path, files: list[str]) -> dict[str, str]:
    checksums = {name: sha256_file(out / name) for name in files}
    (out / "CHECKSUMS.json").write_text(
        json.dumps(checksums, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return checksums


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, help="machine role label, e.g. m3 / m1")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    source = args.source.expanduser()
    out = args.out.expanduser()
    out.mkdir(parents=True, exist_ok=True, mode=0o700)

    if not source.is_file():
        payload = {
            "ok": False,
            "code": "FACT_NO_SOURCE",
            "role": args.role,
            "machine": machine_meta(args.role),
            "source": str(source),
            "message": "源库不存在。合同要求两份原 SQLite；若本机确无原库，须用户确认后调整合同，不能用演练账本替代。",
        }
        (out / "NO_SOURCE.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    backup = out / "market_review.sqlite3.consistent"
    online_backup(source, backup)
    inv = inventory(source, backup, args.role)
    (out / "inventory.json").write_text(
        json.dumps(inv, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # Recompute backup hash into inventory after inventory write is separate;
    # CHECKSUMS bind backup + inventory.
    checksums = write_checksums(out, ["market_review.sqlite3.consistent", "inventory.json"])
    inv["checksums"] = checksums
    (out / "inventory.json").write_text(
        json.dumps(inv, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    checksums = write_checksums(out, ["market_review.sqlite3.consistent", "inventory.json"])

    ok = inv["integrity_check"] == "ok" and not inv["foreign_key_check"]
    result = {
        "ok": ok,
        "role": args.role,
        "out": str(out.resolve()),
        "backup_sha256": inv["backup_sha256"],
        "row_counts": inv["row_counts"],
        "integrity_check": inv["integrity_check"],
        "foreign_key_check_empty": not inv["foreign_key_check"],
        "checksums": checksums,
        "machine": inv["machine"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
