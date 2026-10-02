#!/usr/bin/env python3
"""Step-4 acceptance gap fill: full dual-machine compare + unknown-result recovery.

Does not change CLOUD_DEFAULT_ENABLED. Uses sentinel trade dates only.
Shares the product write gate at ~/.marketreview/supabase-state/ (documented).

Evidence root:
  ~/.marketreview/acceptance-evidence/20261002T080000Z_step4_gap_fill/

Commands:
  export-full-snapshot --db PATH --label m3_after_pull
  compare-snapshots --left PATH.json --right PATH.json
  unknown-result-drill --role m3|m1
  cleanup-unknown-sentinel
  write-m1-runbook
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from marketreview.backend import config_dir_from_env, load_supabase_settings  # noqa: E402
from marketreview.errors import RemoteStoreError  # noqa: E402
from marketreview.paths import production_cloud_state_dir  # noqa: E402
from marketreview.repository import MarketReviewRepository  # noqa: E402
from marketreview.schema import PriceLimitEventInput  # noqa: E402
from marketreview.sqlite_schema import connect  # noqa: E402
from marketreview.supabase_store import UrllibRpcTransport  # noqa: E402
from marketreview.sync_engine import push_sync  # noqa: E402
from marketreview.sync_groups import SCHEMA_VERSION, canonical_json, read_local_groups  # noqa: E402
from marketreview.write_gate import read_pending, same_json_value  # noqa: E402

DRILL_ID = "20261002T080000Z_step4_gap_fill"
# Distinct from prior dual-physical sentinels 2099-10-01..04; one day per machine
DAY_UNKNOWN_BY_ROLE = {"m3": "2099-10-05", "m1": "2099-10-06"}
UNKNOWN_DAYS = tuple(DAY_UNKNOWN_BY_ROLE.values())
FORBIDDEN_PREFIXES = ("2099-08-", "2099-09-")
BUSINESS_TABLES = (
    "daily_market_review",
    "daily_price_limit_event",
    "daily_price_limit_event_detail",
    "daily_price_limit_event_reason",
    "daily_price_limit_event_sector",
)


def evid_dir() -> Path:
    path = Path.home() / ".marketreview" / "acceptance-evidence" / DRILL_ID
    path.mkdir(parents=True, exist_ok=True)
    (path / "reports").mkdir(exist_ok=True)
    (path / "snapshots").mkdir(exist_ok=True)
    return path


def machine_meta() -> dict[str, Any]:
    return {
        "hostname": platform.node(),
        "machine": platform.machine(),
        "system": platform.system(),
        "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def save_report(name: str, payload: Any) -> Path:
    path = evid_dir() / "reports" / f"{name}.json"
    wrapper = {"meta": machine_meta(), "payload": payload}
    path.write_text(json.dumps(wrapper, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"saved": str(path), "meta": wrapper["meta"]}, ensure_ascii=False))
    return path


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dump_five_tables(db: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        tables: dict[str, list[dict[str, Any]]] = {}
        for table in BUSINESS_TABLES:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            order = ", ".join(cols)
            rows = []
            for row in conn.execute(f"SELECT {order} FROM {table} ORDER BY {order}"):
                item = {c: row[c] for c in cols}
                rows.append(item)
            tables[table] = rows
        return tables
    finally:
        conn.close()


def export_full_snapshot(db: Path, label: str) -> dict[str, Any]:
    conn = connect(db)
    try:
        groups = read_local_groups(conn)
    finally:
        conn.close()
    tables = dump_five_tables(db)
    groups_canon = canonical_json(groups)
    tables_canon = canonical_json(tables)
    payload = {
        "label": label,
        "db": str(db.resolve()),
        "machine": machine_meta(),
        "group_refs": sorted(groups),
        "groups": json.loads(groups_canon),
        "groups_sha256": sha256_text(groups_canon),
        "tables": json.loads(tables_canon),
        "tables_sha256": sha256_text(tables_canon),
        "row_counts": {name: len(rows) for name, rows in tables.items()},
        "note": "Full business groups + five-table ordered dumps; includes times, nulls, list order.",
    }
    out = evid_dir() / "snapshots" / f"{label}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    save_report(f"export_{label}", {"path": str(out), "groups_sha256": payload["groups_sha256"], "tables_sha256": payload["tables_sha256"], "row_counts": payload["row_counts"]})
    return payload


def cmd_export_full_snapshot(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser()
    if not db.is_file():
        print(json.dumps({"ok": False, "error": f"missing db {db}"}, ensure_ascii=False))
        return 1
    payload = export_full_snapshot(db, args.label)
    print(
        json.dumps(
            {
                "ok": True,
                "label": args.label,
                "groups_sha256": payload["groups_sha256"],
                "tables_sha256": payload["tables_sha256"],
                "row_counts": payload["row_counts"],
                "group_refs": payload["group_refs"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _diff_keys(left: Any, right: Any, path: str = "") -> list[str]:
    diffs: list[str] = []
    if type(left) is not type(right) and not (
        isinstance(left, (int, float)) and isinstance(right, (int, float))
    ):
        diffs.append(f"{path}: type {type(left).__name__}!={type(right).__name__}")
        return diffs
    if isinstance(left, dict):
        keys = sorted(set(left) | set(right))
        for key in keys:
            p = f"{path}.{key}" if path else key
            if key not in left:
                diffs.append(f"{p}: missing on left")
            elif key not in right:
                diffs.append(f"{p}: missing on right")
            else:
                diffs.extend(_diff_keys(left[key], right[key], p))
        return diffs
    if isinstance(left, list):
        if len(left) != len(right):
            diffs.append(f"{path}: list len {len(left)}!={len(right)}")
            return diffs
        for i, (a, b) in enumerate(zip(left, right)):
            diffs.extend(_diff_keys(a, b, f"{path}[{i}]"))
        return diffs
    if not same_json_value(left, right):
        diffs.append(f"{path}: {left!r} != {right!r}")
    return diffs


def cmd_compare_snapshots(args: argparse.Namespace) -> int:
    left = json.loads(Path(args.left).expanduser().read_text(encoding="utf-8"))
    right = json.loads(Path(args.right).expanduser().read_text(encoding="utf-8"))
    group_ok = same_json_value(left.get("groups"), right.get("groups"))
    tables_ok = same_json_value(left.get("tables"), right.get("tables"))
    group_diffs = [] if group_ok else _diff_keys(left.get("groups"), right.get("groups"), "groups")[:50]
    table_diffs = [] if tables_ok else _diff_keys(left.get("tables"), right.get("tables"), "tables")[:50]
    payload = {
        "left_label": left.get("label"),
        "right_label": right.get("label"),
        "left_machine": left.get("machine"),
        "right_machine": right.get("machine"),
        "groups_sha256_left": left.get("groups_sha256"),
        "groups_sha256_right": right.get("groups_sha256"),
        "tables_sha256_left": left.get("tables_sha256"),
        "tables_sha256_right": right.get("tables_sha256"),
        "groups_equal": group_ok,
        "tables_equal": tables_ok,
        "group_diff_sample": group_diffs,
        "table_diff_sample": table_diffs,
        "ok": group_ok and tables_ok,
    }
    save_report("compare_snapshots", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


class DropCommitResponseOnce:
    """Simulates: cloud commit succeeds, local loses the HTTP response body."""

    def __init__(self, inner: UrllibRpcTransport) -> None:
        self.inner = inner
        self.dropped = False
        self.captured_result: dict[str, Any] | None = None
        self.captured_operation_id: str | None = None
        self.captured_digest: str | None = None

    def call(self, name: str, payload: MappingLike, *, write: bool = False) -> Any:
        result = self.inner.call(name, payload, write=write)
        if name == "marketreview_sync_commit" and write and not self.dropped:
            self.dropped = True
            self.captured_result = result if isinstance(result, dict) else {"raw": result}
            self.captured_operation_id = payload.get("operation_id") if isinstance(payload, dict) else None
            self.captured_digest = payload.get("request_digest") if isinstance(payload, dict) else None
            raise RemoteStoreError(
                "acceptance: simulated lost response after successful cloud commit",
                code="NETWORK_LOST",
            )
        return result


# typing helper without importing Mapping everywhere for runtime
MappingLike = Any


def _project_id(settings: Any) -> str:
    host = urlparse(settings.url).hostname or ""
    return host.split(".")[0]


def _assert_no_open_pending(state_dir: Path) -> None:
    pending = read_pending(state_dir)
    if pending is not None and pending.get("status") == "open":
        raise RuntimeError(
            f"refusing to start unknown-result drill: open pending {pending.get('kind')} "
            f"operation_id={pending.get('operation_id')}"
        )


def cmd_unknown_result_drill(args: argparse.Namespace) -> int:
    role = args.role
    state_dir = production_cloud_state_dir()
    _assert_no_open_pending(state_dir)

    db = evid_dir() / f"unknown_{role}.sqlite3"
    if db.exists():
        db.unlink()

    settings = load_supabase_settings(config_dir_from_env())
    project_id = _project_id(settings)
    real = UrllibRpcTransport(settings)

    probe_before = real.call("marketreview_probe", {"schema_version": SCHEMA_VERSION}, write=False)
    revision_before = probe_before.get("revision")

    day = DAY_UNKNOWN_BY_ROLE[role]
    pe = 55.0 if role == "m3" else 66.0
    code = "600000" if role == "m3" else "600001"
    with MarketReviewRepository(db) as repo:
        repo.save_review(day, {"pe_sh": pe, "advancing_count": int(pe)})
        repo.save_price_limit_events(
            day,
            [
                PriceLimitEventInput(
                    market="sh",
                    code=code,
                    name="未知结果演练",
                    direction="up",
                    closed_at_limit=True,
                    limit_rate_bp=1000,
                    streak_height=1,
                )
            ],
        )

    dropper = DropCommitResponseOnce(real)
    unknown_report = push_sync(
        sqlite_path=db,
        transport=dropper,
        project_id=project_id,
        state_dir=state_dir,
    )
    pending_after_drop = read_pending(state_dir)
    probe_mid = real.call("marketreview_probe", {"schema_version": SCHEMA_VERSION}, write=False)
    revision_after_commit = probe_mid.get("revision")

    stored = None
    if dropper.captured_operation_id:
        stored = real.call(
            "marketreview_sync_result",
            {"schema_version": SCHEMA_VERSION, "operation_id": dropper.captured_operation_id},
            write=False,
        )

    resume_report = push_sync(
        sqlite_path=db,
        transport=real,
        project_id=project_id,
        state_dir=state_dir,
    )
    pending_after_resume = read_pending(state_dir)
    probe_after = real.call("marketreview_probe", {"schema_version": SCHEMA_VERSION}, write=False)
    revision_after_resume = probe_after.get("revision")

    # Second resume/push should not advance further for same content
    noop_or_stable = push_sync(
        sqlite_path=db,
        transport=real,
        project_id=project_id,
        state_dir=state_dir,
    )
    probe_final = real.call("marketreview_probe", {"schema_version": SCHEMA_VERSION}, write=False)

    stored_groups = None if not isinstance(stored, dict) else stored.get("groups")
    has_full_cloud_result = isinstance(stored_groups, list) and len(stored_groups) > 0

    ok = (
        unknown_report.get("status") == "unknown"
        and pending_after_drop is not None
        and pending_after_drop.get("status") == "open"
        and dropper.captured_operation_id
        and stored is not None
        and stored.get("operation_id") == dropper.captured_operation_id
        and stored.get("request_digest") == dropper.captured_digest
        and has_full_cloud_result
        and revision_after_commit == (revision_before + 1 if isinstance(revision_before, int) else revision_after_commit)
        and resume_report.get("status") in {"completed", "partial"}
        and resume_report.get("resumed") is True
        and (pending_after_resume is None or pending_after_resume.get("status") == "closed")
        and revision_after_resume == revision_after_commit
        and probe_final.get("revision") == revision_after_commit
    )

    payload = {
        "role": role,
        "day": day,
        "db": str(db),
        "state_dir": str(state_dir),
        "shared_gate_note": "Uses product fixed ~/.marketreview/supabase-state/; not isolated.",
        "revision_before": revision_before,
        "revision_after_lost_response_commit": revision_after_commit,
        "revision_after_resume": revision_after_resume,
        "revision_after_stable_push": probe_final.get("revision"),
        "operation_id": dropper.captured_operation_id,
        "request_digest": dropper.captured_digest,
        "cloud_stored_result": {
            "operation_id": None if stored is None else stored.get("operation_id"),
            "request_digest": None if stored is None else stored.get("request_digest"),
            "committed_revision": None if stored is None else stored.get("committed_revision"),
            "expected_revision": None if stored is None else stored.get("expected_revision"),
            "groups": stored_groups,
            "has_full_groups": has_full_cloud_result,
            "group_count": 0 if not isinstance(stored_groups, list) else len(stored_groups),
        },
        "unknown_report": {
            "status": unknown_report.get("status"),
            "detail": unknown_report.get("detail"),
        },
        "pending_after_drop": {
            "status": None if pending_after_drop is None else pending_after_drop.get("status"),
            "operation_id": None if pending_after_drop is None else pending_after_drop.get("operation_id"),
            "request_digest": None if pending_after_drop is None else pending_after_drop.get("request_digest"),
        },
        "resume_report": {
            "status": resume_report.get("status"),
            "resumed": resume_report.get("resumed"),
            "revision": resume_report.get("revision"),
            "committed": resume_report.get("committed"),
        },
        "stable_push": {
            "status": noop_or_stable.get("status"),
            "revision": noop_or_stable.get("revision"),
            "committed": noop_or_stable.get("committed"),
        },
        "ok": ok,
    }
    save_report(f"unknown_result_{role}", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cmd_cleanup_unknown_sentinel(_: argparse.Namespace) -> int:
    settings = load_supabase_settings(config_dir_from_env())
    t = UrllibRpcTransport(settings)
    batch_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    day_results = []
    all_clear = True
    for day in UNKNOWN_DAYS:
        before = t.call(
            "marketreview_get_day",
            {"schema_version": SCHEMA_VERSION, "trade_date": day, "previous_trade_date": day},
            write=False,
        )
        for ev in before.get("events") or []:
            t.call(
                "marketreview_delete_event",
                {
                    "schema_version": SCHEMA_VERSION,
                    "trade_date": day,
                    "batch_time": batch_time,
                    "market": ev["market"],
                    "code": ev["code"],
                    "direction": ev["direction"],
                },
                write=True,
            )
        if before.get("review") is not None:
            t.call(
                "marketreview_delete_review",
                {"schema_version": SCHEMA_VERSION, "trade_date": day, "batch_time": batch_time},
                write=True,
            )
        t.call(
            "marketreview_delete_price_limit_events",
            {"schema_version": SCHEMA_VERSION, "trade_date": day, "batch_time": batch_time},
            write=True,
        )
        after = t.call(
            "marketreview_get_day",
            {"schema_version": SCHEMA_VERSION, "trade_date": day, "previous_trade_date": day},
            write=False,
        )
        clear = after.get("review") is None and not (after.get("events") or [])
        all_clear = all_clear and clear
        day_results.append(
            {
                "day": day,
                "after_review": after.get("review") is not None,
                "after_events": len(after.get("events") or []),
                "clear": clear,
            }
        )
    sample = t.call(
        "marketreview_get_day",
        {"schema_version": SCHEMA_VERSION, "trade_date": "2099-09-01", "previous_trade_date": "2099-09-01"},
        write=False,
    )
    probe = t.call("marketreview_probe", {"schema_version": SCHEMA_VERSION}, write=False)
    ok = all_clear and sample.get("review") is not None
    payload = {
        "days": day_results,
        "backup_sample_ok": sample.get("review") is not None,
        "probe_revision": probe.get("revision"),
        "ok": ok,
    }
    save_report("cleanup_unknown_sentinel", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cmd_write_m1_runbook(_: argparse.Namespace) -> int:
    text = f"""第 4 项缺口补齐 — M1 操作清单
================================
证据目录（两边相同相对路径）:
  ~/.marketreview/acceptance-evidence/{DRILL_ID}/

说明：验收脚本可用 AirDrop / scp 拷到 M1；若 Skill 已更新并含本脚本更佳。
产品写闸门固定 ~/.marketreview/supabase-state/（共享，不随 --db 隔离）。

------------------------------------------------
A. M1 原 SQLite 一致性备份（合同两份原库）
------------------------------------------------
若日常库存在：
  python3 scripts/acceptance_sqlite_consistency_backup.py \\
    --role m1 \\
    --source ~/.marketreview/market_review.sqlite3 \\
    --out ~/.marketreview/acceptance-evidence/{DRILL_ID}/sqlite_consistency_m1

若源库不存在：脚本会写 NO_SOURCE.json（FACT_NO_SOURCE）。把该文件拷回 M3；
不要用演练 m1.sqlite3 冒充原库。需用户确认后才能改合同口径。

------------------------------------------------
B. 收集上一轮双机原始报告（若仍在）
------------------------------------------------
把下列文件整体拷到 M3 同路径 reports/（保留文件名）：
  ~/.marketreview/acceptance-evidence/20261002T000200Z_step4_dual_physical/reports/m1_*.json
  ~/.marketreview/acceptance-evidence/20261002T000200Z_step4_dual_physical/reports/verify_m1.json

若已丢失：本轮无法事后伪造；须在下一轮双机演练中重新生成并立刻回传。

------------------------------------------------
C. 第二份云端备份校验 — 保存机器原输出
------------------------------------------------
在仍有 20261001T133739Z 包的目录重跑 VERIFY_ON_M1.sh，并把完整终端输出
保存为（含 hostname）：
  ~/.marketreview/acceptance-evidence/{DRILL_ID}/reports/m1_second_backup_verify_raw.txt

可用：
  (hostname; date -u; sh VERIFY_ON_M1.sh) | tee m1_second_backup_verify_raw.txt

------------------------------------------------
D. 完整五表快照（与 M3 同阶段账本比较）
------------------------------------------------
若上一轮演练库仍在：
  python3 scripts/acceptance_step4_gap_fill.py export-full-snapshot \\
    --db ~/.marketreview/acceptance-evidence/20261002T000200Z_step4_dual_physical/m1.sqlite3 \\
    --label m1_after_final_pull

把 snapshots/m1_after_final_pull.json 拷回 M3，在 M3 上：
  python3 scripts/acceptance_step4_gap_fill.py compare-snapshots \\
    --left  .../snapshots/m3_after_final_pull.json \\
    --right .../snapshots/m1_after_final_pull.json

若演练库已删：须重跑迷你对齐 pull 后再导出（或新开一轮双机）。

------------------------------------------------
E. 未知结果恢复（真机）
------------------------------------------------
确认 supabase-state 无 open pending 后（M3 用 2099-10-05，M1 用 2099-10-06，互不冲突）：
  python3 scripts/acceptance_step4_gap_fill.py unknown-result-drill --role m1

把 reports/unknown_result_m1.json 拷回 M3。
两端都做完后，任选一机清理两日哨兵：
  python3 scripts/acceptance_step4_gap_fill.py cleanup-unknown-sentinel

------------------------------------------------
回传清单（必须带 machine meta / 原文件）
------------------------------------------------
- sqlite_consistency_m1/ 或 NO_SOURCE.json
- 上一轮 m1_*.json / verify_m1.json（若有）
- m1_second_backup_verify_raw.txt
- snapshots/m1_after_final_pull.json
- reports/unknown_result_m1.json
"""
    path = evid_dir() / "M1_GAP_FILL_RUNBOOK.txt"
    path.write_text(text, encoding="utf-8")
    # also copy scripts into evidence for offline transfer
    for name in (
        "acceptance_step4_gap_fill.py",
        "acceptance_sqlite_consistency_backup.py",
    ):
        src = SCRIPTS / name
        if src.is_file():
            (evid_dir() / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    print(json.dumps({"ok": True, "runbook": str(path)}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export-full-snapshot")
    e.add_argument("--db", required=True)
    e.add_argument("--label", required=True)
    e.set_defaults(func=cmd_export_full_snapshot)

    c = sub.add_parser("compare-snapshots")
    c.add_argument("--left", required=True)
    c.add_argument("--right", required=True)
    c.set_defaults(func=cmd_compare_snapshots)

    u = sub.add_parser("unknown-result-drill")
    u.add_argument("--role", required=True, choices=["m3", "m1"])
    u.set_defaults(func=cmd_unknown_result_drill)

    sub.add_parser("cleanup-unknown-sentinel").set_defaults(func=cmd_cleanup_unknown_sentinel)
    sub.add_parser("write-m1-runbook").set_defaults(func=cmd_write_m1_runbook)
    return p


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("MARKETREVIEW_HOME", str(Path.home() / ".marketreview"))
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
