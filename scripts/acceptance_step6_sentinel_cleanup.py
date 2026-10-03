#!/usr/bin/env python3
"""合同第 6 项 B 分支：仅清理云端 2099-09-* 验收哨兵（精确白名单）。

不可复用：
  - acceptance_capacity_data_api.py（仅 2099-08-01）
  - acceptance_dual_machine_sync.py / acceptance_step4_gap_fill.py（明确禁止碰 2099-09-*）

产品删除走日常写 RPC（delete_event / delete_review），追加 history 并 bump revision；
不 TRUNCATE、不重置 sync_results、不删除白名单外任何组。

用法：
  # 只读计划（默认）
  python3 scripts/acceptance_step6_sentinel_cleanup.py plan \\
    [--snapshot PATH] [--out plan.json]

  # 隔离或生产执行（须显式授权开关；生产另需用户书面授权）
  python3 scripts/acceptance_step6_sentinel_cleanup.py execute \\
    --i-authorize-sentinel-cleanup-2099-09 \\
    --pre-backup-dir ~/.marketreview/backups/supabase/<pre> \\
    [--state-dir DIR] [--out result.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from marketreview.backend import config_dir_from_env, load_supabase_settings  # noqa: E402
from marketreview.errors import MarketReviewError  # noqa: E402
from marketreview.paths import production_cloud_state_dir  # noqa: E402
from marketreview.pg_backup import PgBackupError, _verify_checksums  # noqa: E402
from marketreview.supabase_store import (  # noqa: E402
    SupabaseRepository,
    UrllibRpcTransport,
)
from marketreview.sync_engine import _fetch_snapshot  # noqa: E402
from marketreview.sync_groups import digest_of, groups_from_snapshot  # noqa: E402

# Exact refs observed in cloud revision=35 / #9 backup samples. Nothing else.
ALLOWED_TRADE_DATES = frozenset({"2099-09-01", "2099-09-02"})
ALLOWED_EVENT_KEYS = frozenset(
    {
        ("2099-09-01", "sh", "600519", "up"),
        ("2099-09-01", "sz", "000001", "down"),
        ("2099-09-02", "bj", "830799", "up"),
    }
)
ALLOWED_REVIEW_DATES = frozenset({"2099-09-01", "2099-09-02"})
ALLOWED_REFS = frozenset(
    {
        "review:2099-09-01",
        "review:2099-09-02",
        "event:2099-09-01:sh:600519",
        "event:2099-09-01:sz:000001",
        "event:2099-09-02:bj:830799",
    }
)


class SentinelCleanupError(MarketReviewError):
    def __init__(self, message: str, *, code: str = "SENTINEL_CLEANUP_REFUSED") -> None:
        super().__init__(code=code, message=message)


def _event_ref(trade_date: str, market: str, code: str) -> str:
    return f"event:{trade_date}:{market}:{code}"


def _review_ref(trade_date: str) -> str:
    return f"review:{trade_date}"


def extract_sentinel_inventory(snapshot: dict[str, Any]) -> dict[str, Any]:
    groups = groups_from_snapshot(snapshot)
    sentinel_refs = sorted(ref for ref in groups if ":2099-09-" in ref or ref.startswith("review:2099-09-"))
    by_date_refs = []
    for ref, group in groups.items():
        key = group.get("group_key") or {}
        trade_date = key.get("trade_date")
        if isinstance(trade_date, str) and trade_date.startswith("2099-09-"):
            by_date_refs.append(ref)
    sentinel_refs = sorted(set(sentinel_refs) | set(by_date_refs))
    events = []
    reviews = []
    for ref in sentinel_refs:
        group = groups[ref]
        key = group["group_key"]
        if group["group_kind"] == "review":
            reviews.append({"ref": ref, "trade_date": key["trade_date"]})
        else:
            event_rows = group.get("events") or []
            for row in event_rows:
                events.append(
                    {
                        "ref": ref,
                        "trade_date": key["trade_date"],
                        "market": key["market"],
                        "code": key["code"],
                        "direction": row.get("direction"),
                    }
                )
    return {
        "revision": snapshot.get("revision"),
        "counts": snapshot.get("counts"),
        "sentinel_refs": sentinel_refs,
        "reviews": reviews,
        "events": events,
        "non_sentinel_group_n": sum(
            1
            for ref, group in groups.items()
            if not str((group.get("group_key") or {}).get("trade_date", "")).startswith("2099-09-")
        ),
    }


def assert_exact_whitelist(inventory: dict[str, Any]) -> None:
    """Refuse unless cloud 2099-09-* set equals the frozen sample keys."""
    refs = set(inventory["sentinel_refs"])
    if refs != ALLOWED_REFS:
        raise SentinelCleanupError(
            "云端 2099-09-* 组集合与白名单不符，拒绝清理："
            f" unexpected={sorted(refs - ALLOWED_REFS)}"
            f" missing={sorted(ALLOWED_REFS - refs)}"
        )
    event_keys = set()
    for row in inventory["events"]:
        key = (row["trade_date"], row["market"], row["code"], row["direction"])
        if row["trade_date"] not in ALLOWED_TRADE_DATES:
            raise SentinelCleanupError(f"拒绝非白名单交易日事件：{key!r}")
        event_keys.add(key)
    if event_keys != ALLOWED_EVENT_KEYS:
        raise SentinelCleanupError(
            "事件键与白名单不符："
            f" unexpected={sorted(event_keys - ALLOWED_EVENT_KEYS)}"
            f" missing={sorted(ALLOWED_EVENT_KEYS - event_keys)}"
        )
    review_dates = {row["trade_date"] for row in inventory["reviews"]}
    if review_dates != ALLOWED_REVIEW_DATES:
        raise SentinelCleanupError(f"复盘日与白名单不符：{sorted(review_dates)}")


def assert_trade_date_allowed(trade_date: str) -> str:
    if trade_date not in ALLOWED_TRADE_DATES:
        raise SentinelCleanupError(f"仅允许 {sorted(ALLOWED_TRADE_DATES)}，拒绝：{trade_date!r}")
    return trade_date


def assert_event_key_allowed(trade_date: str, market: str, code: str, direction: str) -> None:
    assert_trade_date_allowed(trade_date)
    key = (trade_date, market, code, direction)
    if key not in ALLOWED_EVENT_KEYS:
        raise SentinelCleanupError(f"事件键不在白名单：{key!r}")


def build_plan(snapshot: dict[str, Any]) -> dict[str, Any]:
    inventory = extract_sentinel_inventory(snapshot)
    assert_exact_whitelist(inventory)
    steps = []
    for trade_date, market, code, direction in sorted(ALLOWED_EVENT_KEYS):
        steps.append(
            {
                "rpc": "marketreview_delete_event",
                "trade_date": trade_date,
                "market": market,
                "code": code,
                "direction": direction,
            }
        )
    for trade_date in sorted(ALLOWED_REVIEW_DATES):
        steps.append({"rpc": "marketreview_delete_review", "trade_date": trade_date})
    return {
        "kind": "step6_sentinel_cleanup_plan",
        "mode": "plan",
        "whitelist_refs": sorted(ALLOWED_REFS),
        "inventory": inventory,
        "steps": steps,
        "effects": {
            "appends_history": True,
            "bumps_revision": True,
            "resets_sync_results": False,
            "touches_non_whitelist": False,
        },
        "prerequisites": [
            "两机已停写",
            "已完成迁移前云端长期备份（--pre-backup-dir 须通过产品 CHECKSUMS／manifest／revision／snapshot 闸门）",
            "用户书面选择 B 并授权 execute",
        ],
        "note": "plan 只读；execute 才调用写 RPC。",
    }


def project_ref_from_settings(settings: Any) -> str:
    ref = urlparse(settings.url).hostname or settings.url
    if not ref:
        raise SentinelCleanupError("无法从 supabase_url 解析 project_ref。")
    return str(ref)


def require_pre_backup(
    pre_backup_dir: Path,
    *,
    expected_revision: int,
    expected_snapshot_digest: str,
    expected_project_ref: str,
) -> dict[str, Any]:
    """Validate a product backup that covers the cleanup-before cloud state."""
    if not pre_backup_dir.is_dir():
        raise SentinelCleanupError(f"迁移前备份目录不存在：{pre_backup_dir}")
    if not (pre_backup_dir / "BACKUP_OK").is_file():
        raise SentinelCleanupError("迁移前备份缺少 BACKUP_OK，拒绝清理。")
    try:
        _verify_checksums(pre_backup_dir)
    except PgBackupError as exc:
        raise SentinelCleanupError(f"迁移前备份校验失败，拒绝清理：{exc}") from exc
    except OSError as exc:
        raise SentinelCleanupError(f"迁移前备份无法读取，拒绝清理：{exc}") from exc

    manifest_path = pre_backup_dir / "manifest.json"
    snapshot_path = pre_backup_dir / "snapshot.json"
    if not manifest_path.is_file() or not snapshot_path.is_file():
        raise SentinelCleanupError("迁移前备份缺少 manifest.json 或 snapshot.json。")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        backup_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SentinelCleanupError("迁移前备份 manifest／snapshot 无法解析。") from exc

    if manifest.get("retention") != "migration-snapshot":
        raise SentinelCleanupError(
            f"迁移前备份 retention 必须是 migration-snapshot，实际为 {manifest.get('retention')!r}。"
        )
    try:
        backup_revision = int(manifest["revision"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SentinelCleanupError("迁移前备份 manifest 缺少合法 revision。") from exc
    if backup_revision != int(expected_revision):
        raise SentinelCleanupError(
            "迁移前备份 revision 与清理前云端不一致，拒绝使用旧／错备份："
            f" backup={backup_revision} cloud={expected_revision}"
        )
    if backup_snapshot.get("revision") != expected_revision:
        raise SentinelCleanupError(
            "备份内 snapshot.json revision 与清理前云端不一致，拒绝清理。"
        )
    backup_digest = digest_of(backup_snapshot)
    if backup_digest != expected_snapshot_digest:
        raise SentinelCleanupError(
            "备份 snapshot 与清理前云端快照 digest 不一致，拒绝清理（可能是另一项目或已过时备份）。"
        )
    if not expected_project_ref:
        raise SentinelCleanupError("缺少 expected_project_ref，拒绝清理。")
    return {
        "pre_backup_dir": str(pre_backup_dir.resolve()),
        "manifest_revision": backup_revision,
        "manifest_retention": manifest.get("retention"),
        "backup_snapshot_digest": backup_digest,
        "expected_project_ref": expected_project_ref,
    }


def execute_cleanup(
    *,
    repo: SupabaseRepository,
    transport: Any,
    pre_backup_dir: Path,
    expected_project_ref: str,
) -> dict[str, Any]:
    if getattr(repo, "_state_dir", None) is None or not getattr(repo, "_project_ref", None):
        raise SentinelCleanupError(
            "SupabaseRepository 缺少 state_dir／project_ref，未发送写请求。",
            code="PENDING_UNREADABLE",
        )
    if str(repo._project_ref) != str(expected_project_ref):
        raise SentinelCleanupError(
            "仓库 project_ref 与预期项目不一致，拒绝清理："
            f" repo={repo._project_ref!r} expected={expected_project_ref!r}"
        )

    before = _fetch_snapshot(transport)
    before_digest = digest_of(before)
    backup_meta = require_pre_backup(
        pre_backup_dir,
        expected_revision=int(before["revision"]),
        expected_snapshot_digest=before_digest,
        expected_project_ref=expected_project_ref,
    )
    plan = build_plan(before)
    deleted_events = []
    for trade_date, market, code, direction in sorted(ALLOWED_EVENT_KEYS):
        assert_event_key_allowed(trade_date, market, code, direction)
        repo.delete_price_limit_event(trade_date, market, code, direction)
        deleted_events.append(
            {"trade_date": trade_date, "market": market, "code": code, "direction": direction}
        )
    deleted_reviews = []
    for trade_date in sorted(ALLOWED_REVIEW_DATES):
        assert_trade_date_allowed(trade_date)
        repo.delete_review(trade_date)
        deleted_reviews.append({"trade_date": trade_date})
    after = _fetch_snapshot(transport)
    after_inv = extract_sentinel_inventory(after)
    if after_inv["sentinel_refs"]:
        raise SentinelCleanupError(
            f"清理后仍残留 2099-09-* 组：{after_inv['sentinel_refs']}",
            code="SENTINEL_CLEANUP_INCOMPLETE",
        )
    return {
        "kind": "step6_sentinel_cleanup_result",
        "mode": "execute",
        "pre_backup_dir": str(pre_backup_dir),
        "backup_gate": backup_meta,
        "project_ref": expected_project_ref,
        "state_dir": str(repo._state_dir),
        "before_revision": before.get("revision"),
        "after_revision": after.get("revision"),
        "before_counts": before.get("counts"),
        "after_counts": after.get("counts"),
        "before_snapshot_digest": before_digest,
        "after_snapshot_digest": digest_of(after),
        "deleted_events": deleted_events,
        "deleted_reviews": deleted_reviews,
        "after_sentinel_refs": after_inv["sentinel_refs"],
        "history_before": (before.get("counts") or {}).get("history"),
        "history_after": (after.get("counts") or {}).get("history"),
        "sync_results_before": (before.get("counts") or {}).get("sync_results"),
        "sync_results_after": (after.get("counts") or {}).get("sync_results"),
        "plan_whitelist_refs": plan["whitelist_refs"],
    }


def build_repository(
    *,
    transport: Any,
    state_dir: Path | None = None,
    project_ref: str | None = None,
    settings: Any | None = None,
) -> tuple[SupabaseRepository, str]:
    loaded = settings if settings is not None else load_supabase_settings(config_dir_from_env())
    ref = project_ref if project_ref is not None else project_ref_from_settings(loaded)
    directory = state_dir if state_dir is not None else production_cloud_state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return (
        SupabaseRepository(transport, state_dir=directory, project_ref=ref),
        ref,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第6项 B 分支：2099-09 哨兵精确清理")
    sub = parser.add_subparsers(dest="command", required=True)

    plan_p = sub.add_parser("plan", help="只读：核验白名单并输出删除步骤")
    plan_p.add_argument("--snapshot", type=Path, default=None)
    plan_p.add_argument("--out", type=Path, default=None)

    exec_p = sub.add_parser("execute", help="执行清理（须授权开关 + 迁移前备份目录）")
    exec_p.add_argument(
        "--i-authorize-sentinel-cleanup-2099-09",
        action="store_true",
        default=False,
        help="显式确认仅清理白名单 2099-09 哨兵",
    )
    exec_p.add_argument("--pre-backup-dir", type=Path, required=True)
    exec_p.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="本机写入状态目录；默认生产共享 supabase-state（隔离测试请注入临时目录）",
    )
    exec_p.add_argument("--out", type=Path, default=None)

    args = parser.parse_args(argv)

    if args.command == "plan":
        if args.snapshot is not None:
            snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        else:
            settings = load_supabase_settings(config_dir_from_env())
            snapshot = _fetch_snapshot(UrllibRpcTransport(settings))
        try:
            plan = build_plan(snapshot)
        except SentinelCleanupError as exc:
            payload = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
            print(json.dumps(payload, ensure_ascii=False))
            return 1
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"ok": True, "plan": plan if args.out is None else {"out": str(args.out)}}, ensure_ascii=False))
        return 0

    if not args.i_authorize_sentinel_cleanup_2099_09:
        raise SystemExit("缺少 --i-authorize-sentinel-cleanup-2099-09")
    settings = load_supabase_settings(config_dir_from_env())
    transport = UrllibRpcTransport(settings)
    repo, project_ref = build_repository(
        transport=transport,
        state_dir=args.state_dir.expanduser().resolve() if args.state_dir is not None else None,
        settings=settings,
    )
    try:
        result = execute_cleanup(
            repo=repo,
            transport=transport,
            pre_backup_dir=args.pre_backup_dir,
            expected_project_ref=project_ref,
        )
    except SentinelCleanupError as exc:
        payload = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        print(json.dumps(payload, ensure_ascii=False))
        return 1
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "result": result if args.out is None else {"out": str(args.out)}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
