#!/usr/bin/env python3
"""Register long-term pre-migration snapshots without rewriting them.

Reads existing cloud backup manifests and SQLite consistency copies.
The SQLite census counts sync tables only. It does not open the live
production database and does not change files that an earlier review hashed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from acceptance_sqlite_consistency_backup import (
    MANUAL_BACKUP_POLICY,
    sha256_file,
    sync_census,
)


def cloud_snapshot(path: Path) -> dict[str, Any]:
    directory = path.expanduser().resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    retention = manifest.get("retention")
    return {
        "path": str(directory),
        "retention": retention,
        "long_term": retention == "migration-snapshot",
        "created_at": manifest.get("created_at"),
        "revision": manifest.get("revision"),
        "backup_ok": (directory / "BACKUP_OK").is_file(),
        "checksums_sha256": sha256_file(directory / "CHECKSUMS"),
    }


def sqlite_snapshot(path: Path) -> dict[str, Any]:
    directory = path.expanduser().resolve()
    inventory_path = directory / "inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    backup = directory / "market_review.sqlite3.consistent"
    actual = sha256_file(backup)
    expected = inventory.get("backup_sha256")
    if actual != expected:
        raise SystemExit(f"一致性备份校验和不匹配：{backup}")
    conn = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        census = sync_census(conn)
    finally:
        conn.close()
    machine = inventory.get("machine") if isinstance(inventory.get("machine"), dict) else {}
    return {
        "role": machine.get("role"),
        "backup_path": str(backup),
        "backup_sha256": actual,
        "matches_existing_inventory": True,
        "existing_inventory": str(inventory_path),
        "business_row_counts": inventory.get("row_counts"),
        "sync_metadata": census,
        "note": "未改写已复核的清单。同步表是本次附加普查。",
    }


def build_register(sqlite_dirs: list[Path], cloud_dirs: list[Path]) -> dict[str, Any]:
    sqlite_rows = [sqlite_snapshot(path) for path in sqlite_dirs]
    cloud_rows = [cloud_snapshot(path) for path in cloud_dirs]
    if any(not row["long_term"] for row in cloud_rows):
        raise SystemExit("云端备份不是 migration-snapshot，不能登记为长期迁移前快照。")
    if len({row["role"] for row in sqlite_rows}) != len(sqlite_rows):
        raise SystemExit("SQLite 快照角色重复或缺失。")
    return {
        "kind": "pre-migration-long-term",
        "policy": MANUAL_BACKUP_POLICY,
        "sqlite_snapshots": sqlite_rows,
        "cloud_snapshots": cloud_rows,
        "unchanged": "已有一致性备份、CHECKSUMS 和云端备份目录都没有改写。",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sqlite", type=Path, action="append", required=True)
    parser.add_argument("--cloud", type=Path, action="append", required=True)
    args = parser.parse_args(argv)
    out = args.out.expanduser()
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = out / "migration_register.json"
    if target.exists():
        raise SystemExit(f"登记文件已存在，未覆盖：{target}")
    payload = build_register(args.sqlite, args.cloud)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    digest = sha256_file(target)
    (out / "CHECKSUMS.json").write_text(
        json.dumps({"migration_register.json": digest}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "out": str(out.resolve()), "sha256": digest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
