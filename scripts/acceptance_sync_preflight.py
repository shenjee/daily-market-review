#!/usr/bin/env python3
"""只读同步预检：拉取完整云端快照并 classify，绝不调用 sync commit / push。

正式迁移窗口内用本脚本（或等价步骤）替代「无选择 push」。无选择的
``sync push`` 仍会提交全部 local_only，不能当作只读预检。

本脚本对 ``--source`` 使用 SQLite ``mode=ro``（**不**使用 ``immutable=1``，
以免漏读 WAL），并在发现非空 ``*-wal`` 时拒绝——预检只接受无待应用 WAL
的独立完整快照／一致性备份。**不会**调用 ``ensure_sync_schema`` /
``connect()``（后者会改 journal_mode）。

默认按「首次迁移」分类：源库若已出现任一同步表或已有身份／基线／coverage／auth／
operation 行，直接拒绝。需要已有同步状态时，显式传
``--with-existing-sync-state`` 并读取真实 B／coverage／auth。

``--out`` 不得与 ``--source``／``--snapshot`` 同路径或同 inode（硬链接／符号链接别名）。

示例：

  python3 scripts/acceptance_sync_preflight.py \\
    --source /path/to/isolated_or_protected_copy.sqlite3 \\
    --out ~/.marketreview/acceptance-evidence/<id>/preflight_m3.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from marketreview.backend import config_dir_from_env, load_supabase_settings  # noqa: E402
from marketreview.errors import MarketReviewError  # noqa: E402
from marketreview.supabase_store import UrllibRpcTransport  # noqa: E402
from marketreview.sync_engine import _fetch_snapshot  # noqa: E402
from marketreview.sync_groups import (  # noqa: E402
    classify_ledger,
    digest_of,
    groups_from_snapshot,
    read_local_groups,
    snapshot_history,
)
from marketreview.sync_ledger import (  # noqa: E402
    has_coverage,
    read_authorizations,
    read_baselines,
)

SYNC_TABLES = (
    "sync_ledger_singleton",
    "sync_baseline",
    "sync_coverage",
    "sync_authorization",
    "sync_operation",
)


class PreflightError(MarketReviewError):
    def __init__(self, message: str, *, code: str = "PREFLIGHT_REFUSED") -> None:
        super().__init__(code=code, message=message)


def _require_first_join(meta: dict[str, Any]) -> None:
    if meta["any_sync_table_present"] or meta["ledger_identity_present"] or meta["any_sync_rows"]:
        raise PreflightError(
            "源库已出现同步表或同步身份／基线／coverage／auth／operation，"
            "不能按首次迁移预检。请换未初始化副本，或显式使用 "
            "--with-existing-sync-state 读取真实三方状态。",
            code="PREFLIGHT_NOT_FIRST_JOIN",
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_path(path: Path) -> Path:
    expanded = path.expanduser()
    try:
        return expanded.resolve()
    except OSError:
        return expanded.absolute()


def same_filesystem_file(left: Path, right: Path) -> bool:
    """True when paths name the same inode (hard link) or resolve to the same file."""
    left_exp = left.expanduser()
    right_exp = right.expanduser()
    if _resolve_path(left_exp) == _resolve_path(right_exp):
        return True
    if not (left_exp.exists() and right_exp.exists()):
        # Dangling symlink: still compare resolved targets when possible.
        if left_exp.is_symlink() or right_exp.is_symlink():
            return _resolve_path(left_exp) == _resolve_path(right_exp)
        return False
    try:
        return os.path.samefile(left_exp, right_exp)
    except OSError:
        return False


def assert_output_distinct(
    *,
    source: Path,
    out: Path,
    snapshot: Path | None = None,
) -> None:
    """Refuse before any write when --out aliases source or snapshot inputs."""
    guarded: list[tuple[str, Path]] = [("source", source)]
    if snapshot is not None:
        guarded.append(("snapshot", snapshot))
    for label, target in guarded:
        if same_filesystem_file(out, target):
            raise PreflightError(
                f"--out 与 --{label} 是同一文件或别名（同路径／符号链接／硬链接），"
                "拒绝写入以免把 SQLite／审查输入覆盖成 JSON。",
                code="PREFLIGHT_OUTPUT_ALIASES_INPUT",
            )


def wal_paths_for(sqlite_path: Path) -> list[Path]:
    bases = [sqlite_path.expanduser()]
    resolved = _resolve_path(sqlite_path)
    if resolved not in bases:
        bases.append(resolved)
    found: list[Path] = []
    seen: set[str] = set()
    for base in bases:
        wal = Path(str(base) + "-wal")
        key = str(wal)
        if key in seen:
            continue
        seen.add(key)
        found.append(wal)
    return found


def assert_no_pending_wal(sqlite_path: Path) -> dict[str, Any]:
    """Consistency-backup preflight must not skip committed-but-uncheckpointed WAL state."""
    pending: list[dict[str, Any]] = []
    for wal in wal_paths_for(sqlite_path):
        if wal.is_file() and wal.stat().st_size > 0:
            pending.append({"path": str(wal), "size": wal.stat().st_size})
    if pending:
        detail = ", ".join(f"{item['path']} ({item['size']} bytes)" for item in pending)
        raise PreflightError(
            "源库存在非空 WAL，预检拒绝读取，以免漏掉已提交但未 checkpoint 的"
            f"身份／基线／删除：{detail}。"
            "请对无待应用 WAL 的一致性备份或独立完整快照做预检。",
            code="PREFLIGHT_WAL_PENDING",
        )
    return {"pending_wal": False, "checked": [str(path) for path in wal_paths_for(sqlite_path)]}


def connect_readonly(sqlite_path: Path) -> sqlite3.Connection:
    """Open source DB read-only without immutable=1 (which would ignore WAL).

    Callers must run ``assert_no_pending_wal`` first so mode=ro sees the full
    committed state on a checkpointed main file.
    """
    resolved = _resolve_path(sqlite_path)
    query = f"{resolved.as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(query, uri=True)
    except sqlite3.Error as exc:
        raise PreflightError(f"无法以只读方式打开源库：{resolved} ({exc})") from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        pass
    return conn


def inspect_sync_metadata(conn: sqlite3.Connection) -> dict[str, Any]:
    tables: dict[str, dict[str, Any]] = {}
    for name in SYNC_TABLES:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        if present is None:
            tables[name] = {"present": False, "rows": 0}
            continue
        rows = int(conn.execute(f"SELECT count(*) AS n FROM {name}").fetchone()["n"])
        tables[name] = {"present": True, "rows": rows}
    ledger_id = None
    singleton = tables["sync_ledger_singleton"]
    if singleton["present"] and singleton["rows"] > 0:
        row = conn.execute(
            "SELECT ledger_id FROM sync_ledger_singleton WHERE id = 1"
        ).fetchone()
        if row is not None and row["ledger_id"] is not None:
            ledger_id = str(row["ledger_id"])
    any_table = any(item["present"] for item in tables.values())
    any_rows = any(item["rows"] > 0 for item in tables.values())
    return {
        "tables": tables,
        "ledger_id": ledger_id,
        "any_sync_table_present": any_table,
        "any_sync_rows": any_rows,
        "ledger_identity_present": ledger_id is not None,
    }


def _project_id_from_settings() -> str:
    settings = load_supabase_settings(config_dir_from_env())
    host = urlparse(settings.url).hostname or ""
    project_id = host.split(".")[0]
    if not project_id:
        raise PreflightError("无法从 supabase_url 解析 project_id。")
    return project_id


def _read_existing_sync_state(
    conn: sqlite3.Connection,
    meta: dict[str, Any],
    *,
    project_id: str,
) -> tuple[dict[str, Any], bool, dict[str, Any], str]:
    if not meta["ledger_identity_present"] or not meta["ledger_id"]:
        raise PreflightError(
            "已要求读取现有同步状态，但源库没有可用的 ledger 身份；"
            "不会降级为首次接入。",
            code="PREFLIGHT_SYNC_STATE_MISSING",
        )
    # Corrupt／未知基线必须原样抛出，不能降成 first-join。
    ledger_id = str(meta["ledger_id"])
    baselines = read_baselines(conn, project_id=project_id, ledger_id=ledger_id)
    covered = has_coverage(conn, project_id=project_id, ledger_id=ledger_id)
    authorizations = read_authorizations(conn, project_id=project_id, ledger_id=ledger_id)
    return baselines, covered, authorizations, project_id


def build_report(
    *,
    sqlite_path: Path,
    snapshot: dict,
    with_existing_sync_state: bool = False,
    project_id: str | None = None,
) -> dict:
    path = _resolve_path(sqlite_path)
    wal_meta = assert_no_pending_wal(sqlite_path)
    before_sha = sha256_file(path)
    conn = connect_readonly(path)
    try:
        meta = inspect_sync_metadata(conn)
        if with_existing_sync_state:
            resolved_project = project_id if project_id is not None else _project_id_from_settings()
            baselines, covered, authorizations, project_id = _read_existing_sync_state(
                conn, meta, project_id=resolved_project
            )
            mode = "existing_sync_state"
        else:
            _require_first_join(meta)
            baselines, covered, authorizations, project_id = {}, False, {}, None
            mode = "first_join"
        local_groups = read_local_groups(conn)
    finally:
        conn.close()
    after_sha = sha256_file(path)
    if after_sha != before_sha:
        raise PreflightError(
            f"只读预检后源库哈希发生变化：before={before_sha} after={after_sha}",
            code="PREFLIGHT_SOURCE_MUTATED",
        )

    cloud_groups = groups_from_snapshot(snapshot)
    history = snapshot_history(snapshot)
    classified, invalid_auth = classify_ledger(
        local_groups=local_groups,
        cloud_groups=cloud_groups,
        baselines=baselines,
        covered=covered,
        history=history,
        authorizations=authorizations,
    )
    by_cat: dict[str, list[str]] = {}
    for item in classified:
        by_cat.setdefault(item.category, []).append(item.ref)
    categories = dict(Counter(item.category for item in classified))
    blockers = sorted(
        set(by_cat.get("conflict", []))
        | set(by_cat.get("local_delete", []))
        | set(by_cat.get("local_change", []))
    )
    return {
        "kind": "sync_preflight_readonly",
        "mode": mode,
        "sqlite_path": str(path),
        "source_sha256_before": before_sha,
        "source_sha256_after": after_sha,
        "source_unchanged": before_sha == after_sha,
        "wal_check": wal_meta,
        "sync_metadata": meta,
        "project_id": project_id,
        "cloud_revision": snapshot.get("revision"),
        "cloud_complete": snapshot.get("complete"),
        "cloud_counts": snapshot.get("counts"),
        "cloud_snapshot_digest": digest_of(snapshot),
        "local_group_n": len(local_groups),
        "cloud_group_n": len(cloud_groups),
        "history_n": len(history),
        "baseline_n": len(baselines),
        "covered": covered,
        "authorization_n": len(authorizations),
        "categories": categories,
        "refs_by_category": {key: sorted(refs) for key, refs in sorted(by_cat.items())},
        "blockers_needing_choice": blockers,
        "invalid_authorizations": invalid_auth,
        "ready_for_authorized_push": not blockers and not invalid_auth,
        "note": (
            "本报告由只读 snapshot + classify 生成；源库以 mode=ro 打开（无 immutable），"
            "且要求无非空 WAL；未初始化同步表。"
            "不得用无选择 sync push 代替本预检；push 会提交全部 local_only。"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读同步预检（不 push）")
    parser.add_argument("--source", required=True, type=Path, help="本地 SQLite（隔离或受保护副本优先）")
    parser.add_argument("--out", required=True, type=Path, help="预检 JSON 输出路径（不得别名源库／snapshot）")
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help="可选：已保存的完整 snapshot JSON（跳过网络）；须含完整 history/results",
    )
    parser.add_argument(
        "--with-existing-sync-state",
        action="store_true",
        default=False,
        help="读取并核验已有 ledger／基线／coverage／auth；损坏时失败，不降级为首次接入",
    )
    args = parser.parse_args(argv)
    if not args.source.is_file():
        raise SystemExit(f"源库不存在：{args.source}")

    try:
        assert_output_distinct(source=args.source, out=args.out, snapshot=args.snapshot)
    except PreflightError as exc:
        payload = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        print(json.dumps(payload, ensure_ascii=False))
        return 1

    if args.snapshot is not None:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    else:
        settings = load_supabase_settings(config_dir_from_env())
        snapshot = _fetch_snapshot(UrllibRpcTransport(settings))

    try:
        report = build_report(
            sqlite_path=args.source,
            snapshot=snapshot,
            with_existing_sync_state=args.with_existing_sync_state,
        )
    except PreflightError as exc:
        payload = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        print(json.dumps(payload, ensure_ascii=False))
        return 1

    # Re-check immediately before write (TOCTOU / alias safety).
    try:
        assert_output_distinct(source=args.source, out=args.out, snapshot=args.snapshot)
    except PreflightError as exc:
        payload = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        print(json.dumps(payload, ensure_ascii=False))
        return 1

    source_sha_before_write = report["source_sha256_after"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    source_sha_after_write = sha256_file(_resolve_path(args.source))
    if source_sha_after_write != source_sha_before_write:
        payload = {
            "ok": False,
            "error": {
                "code": "PREFLIGHT_SOURCE_MUTATED",
                "message": (
                    "写出报告后源库哈希变化（可能 --out 别名覆盖了源库）："
                    f"before={source_sha_before_write} after={source_sha_after_write}"
                ),
            },
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "out": str(args.out),
                "ready": report["ready_for_authorized_push"],
                "categories": report["categories"],
                "source_unchanged": report["source_unchanged"],
                "mode": report["mode"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["ready_for_authorized_push"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
