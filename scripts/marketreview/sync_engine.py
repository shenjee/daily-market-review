"""Upload merge and full download. Sync does not change the daily backend default."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .errors import MarketReviewError, RemoteStoreError
from .sqlite_schema import connect
from .sync_groups import (
    SCHEMA_VERSION,
    ClassifiedGroup,
    canonical_json,
    commit_body,
    digest_of,
    group_ref,
    groups_from_snapshot,
    normalize_public_groups,
    read_local_groups,
    same_business,
    same_evidence,
    snapshot_history,
)
from .sync_ledger import (
    consume_authorizations,
    begin_immediate,
    ensure_ledger,
    ensure_sync_schema,
    get_operation,
    has_coverage,
    mark_authorizations_invalid,
    read_authorizations,
    read_baselines,
    replace_coverage,
    save_keep_cloud_authorizations,
    save_operation,
    write_baseline,
)
from .write_gate import (
    assert_no_open_pending,
    close_pending,
    exclusive_write,
    read_pending,
    same_json_value,
    save_open_pending,
)

MAX_BODY_BYTES = 8 * 1024 * 1024
DEFINITE_REJECT = frozenset(
    {
        "SCHEMA_VERSION_MISMATCH",
        "INVALID_REQUEST",
        "UNKNOWN_FIELD",
        "INVALID_TYPE",
        "DUPLICATE_IDENTITY",
        "PARENT_EVENT_MISSING",
        "REPLACE_SOURCE_MISSING",
        "REPLACE_TARGET_EXISTS",
        "REVISION_CONFLICT",
        "OPERATION_DIGEST_MISMATCH",
        "EMPTY_COMMIT",
        "REQUEST_TOO_LARGE",
        "LIST_NULL",
        "REMOTE_FORBIDDEN",
        "42501",
    }
)


@dataclass(frozen=True)
class GroupChoice:
    group_kind: str
    group_key: dict[str, str]
    action: str

    @property
    def ref(self) -> str:
        return group_ref(self.group_kind, self.group_key)


def push_sync(
    *,
    sqlite_path: Path,
    transport: Any,
    project_id: str,
    state_dir: Path,
    choices: Sequence[GroupChoice] = (),
    busy_timeout_ms: int = 5000,
) -> dict[str, Any]:
    _require_token(project_id, "project_id")
    with exclusive_write(state_dir):
        pending = _open_pending(state_dir)
        conn = _open_db(sqlite_path, busy_timeout_ms)
        try:
            ledger_id = ensure_ledger(conn)
            assert_ledger_file_identity(state_dir, ledger_id, sqlite_path, conn)
            if pending is not None:
                if pending.get("kind") != "sync-push":
                    raise MarketReviewError(
                        code="PENDING_WRITE",
                        message="存在未关闭的待核验记录，未开始新的同步。",
                    )
                return _resume_push(
                    conn,
                    transport,
                    pending,
                    state_dir=state_dir,
                    project_id=project_id,
                    ledger_id=ledger_id,
                    sqlite_path=sqlite_path,
                )
            return _push_new(
                conn,
                transport,
                sqlite_path=sqlite_path,
                project_id=project_id,
                ledger_id=ledger_id,
                state_dir=state_dir,
                choices=choices,
            )
        finally:
            conn.close()


def pull_sync(
    *,
    sqlite_path: Path,
    transport: Any,
    project_id: str,
    state_dir: Path,
    choices: Sequence[GroupChoice] = (),
    busy_timeout_ms: int = 5000,
    before_local_commit: Callable[[], None] | None = None,
) -> dict[str, Any]:
    _require_token(project_id, "project_id")
    real_path = assert_pull_target_allowed(sqlite_path, state_dir)
    with exclusive_write(state_dir):
        pending = _open_pending(state_dir)
        conn = _open_db(real_path, busy_timeout_ms)
        try:
            ledger_id = ensure_ledger(conn)
            assert_ledger_file_identity(state_dir, ledger_id, real_path, conn)
            if pending is not None:
                if pending.get("kind") != "sync-pull":
                    raise MarketReviewError(
                        code="PENDING_WRITE",
                        message="存在未关闭的待核验记录，未开始新的下载。",
                    )
                return _resume_pull(
                    conn,
                    pending,
                    state_dir=state_dir,
                    project_id=project_id,
                    ledger_id=ledger_id,
                    sqlite_path=real_path,
                    before_local_commit=before_local_commit,
                )
            return _pull_new(
                conn,
                transport,
                sqlite_path=real_path,
                project_id=project_id,
                ledger_id=ledger_id,
                state_dir=state_dir,
                choices=choices,
                before_local_commit=before_local_commit,
            )
        finally:
            conn.close()


def assert_ledger_file_identity(
    state_dir: Path,
    ledger_id: str,
    sqlite_path: Path,
    conn: sqlite3.Connection,
) -> None:
    """Keep identity across a path move. Stop when another live file still has it.

    The file binding lives in the machine state directory. The database also
    records which installation bound it, so copying only the database to another
    machine stops instead of continuing as the original ledger. A missing or
    unreadable binding stops the sync instead of being treated as a first join.
    """
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_same_installation(state_dir, conn)
    path = state_dir / "ledger-homes.json"
    homes = _load_ledger_homes(path)
    current = Path(os.path.realpath(sqlite_path))
    recorded = homes.get(ledger_id)
    if recorded is None:
        homes[ledger_id] = str(current)
        _write_ledger_homes(path, homes)
        return
    previous = Path(recorded)
    if previous.exists() and _same_file(previous, current):
        if str(previous) != str(current):
            homes[ledger_id] = str(current)
            _write_ledger_homes(path, homes)
        return
    if previous.exists() and _singleton_ledger_id(previous) == ledger_id:
        raise MarketReviewError(
            code="IDENTITY_MISMATCH",
            message="独立副本与原账本共用身份，已停止，不会按首次接入重建。",
        )
    homes[ledger_id] = str(current)
    _write_ledger_homes(path, homes)


def _require_same_installation(state_dir: Path, conn: sqlite3.Connection) -> None:
    current = _installation_id(state_dir)
    row = conn.execute(
        "SELECT bound_installation_id FROM sync_ledger_singleton WHERE id = 1"
    ).fetchone()
    bound = None if row is None or row[0] is None else str(row[0])
    if bound == current:
        return
    if bound is not None:
        raise MarketReviewError(
            code="IDENTITY_MISMATCH",
            message="独立副本与原账本共用身份，已停止，不会按首次接入重建。",
        )
    begin_immediate(conn)
    try:
        again = conn.execute(
            "SELECT bound_installation_id FROM sync_ledger_singleton WHERE id = 1"
        ).fetchone()
        existing = None if again is None or again[0] is None else str(again[0])
        if existing is None:
            conn.execute(
                """
                UPDATE sync_ledger_singleton
                SET bound_installation_id = ?
                WHERE id = 1
                """,
                (current,),
            )
        elif existing != current:
            raise MarketReviewError(
                code="IDENTITY_MISMATCH",
                message="独立副本与原账本共用身份，已停止，不会按首次接入重建。",
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _installation_id(state_dir: Path) -> str:
    path = state_dir / "installation-id"
    if path.exists():
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise MarketReviewError(
                code="IDENTITY_MISMATCH",
                message="本机安装身份无法读取，已停止，不会按首次接入重建。",
            ) from exc
        if not value:
            raise MarketReviewError(
                code="IDENTITY_MISMATCH",
                message="本机安装身份无法读取，已停止，不会按首次接入重建。",
            )
        return value
    value = "install-" + uuid.uuid4().hex
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _installation_id(state_dir)
    try:
        os.write(fd, (value + "\n").encode("utf-8"))
        os.fsync(fd)
    except OSError as exc:
        raise MarketReviewError(
            code="IDENTITY_MISMATCH",
            message="本机安装身份无法保存，已停止，不会按首次接入重建。",
        ) from exc
    finally:
        os.close(fd)
    return value


def _load_ledger_homes(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MarketReviewError(
            code="IDENTITY_MISMATCH",
            message="账本绑定记录无法读取，已停止，不会按首次接入重建。",
        ) from exc
    homes = payload.get("ledgers") if isinstance(payload, dict) else None
    if not isinstance(homes, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and key and value for key, value in homes.items()
    ):
        raise MarketReviewError(
            code="IDENTITY_MISMATCH",
            message="账本绑定记录无法读取，已停止，不会按首次接入重建。",
        )
    return dict(homes)


def _write_ledger_homes(path: Path, homes: Mapping[str, str]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    data = json.dumps({"ledgers": homes}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise MarketReviewError(
            code="IDENTITY_MISMATCH",
            message="账本绑定记录无法保存，已停止，不会按首次接入重建。",
        ) from exc


def _singleton_ledger_id(path: Path) -> str | None:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise MarketReviewError(
            code="IDENTITY_MISMATCH",
            message="无法核验原账本身份，已停止，不会按首次接入重建。",
        ) from exc
    try:
        row = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'sync_ledger_singleton'
            """
        ).fetchone()
        if row is None:
            return None
        found = conn.execute(
            "SELECT ledger_id FROM sync_ledger_singleton WHERE id = 1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise MarketReviewError(
            code="IDENTITY_MISMATCH",
            message="无法核验原账本身份，已停止，不会按首次接入重建。",
        ) from exc
    finally:
        conn.close()
    if found is None or found[0] is None:
        return None
    return str(found[0])


def _same_file(left: Path, right: Path) -> bool:
    try:
        return _inode(left) == _inode(right)
    except OSError as exc:
        raise MarketReviewError(
            code="IDENTITY_MISMATCH",
            message="无法核验原账本身份，已停止，不会按首次接入重建。",
        ) from exc


def protect_snapshot(state_dir: Path, sqlite_path: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state_dir / "protected-snapshots.json"
    current: list[str] = []
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("paths"), list):
            current = [str(item) for item in payload["paths"]]
    real = os.path.realpath(sqlite_path)
    if real not in current:
        current.append(real)
    path.write_text(json.dumps({"paths": current}), encoding="utf-8")
    os.chmod(path, 0o600)


def protect_registered_snapshots(state_dir: Path, sqlite_paths: Sequence[Path]) -> list[str]:
    """Record migration-register SQLite copies under the shared write gate."""
    protected: list[str] = []
    with exclusive_write(state_dir):
        for sqlite_path in sqlite_paths:
            protect_snapshot(state_dir, sqlite_path)
            protected.append(os.path.realpath(sqlite_path))
    return protected


def assign_new_ledger_identity(sqlite_path: Path, state_dir: Path) -> str:
    """Fork ledger id only while holding the shared gate and with no open pending."""
    from .sqlite_schema import init_db
    from .sync_ledger import fork_ledger_identity

    with exclusive_write(state_dir):
        assert_no_open_pending(state_dir)
        conn = connect(sqlite_path)
        try:
            ensure_sync_schema(conn)
            init_db(conn)
            return fork_ledger_identity(conn)
        finally:
            conn.close()


def assert_pull_target_allowed(sqlite_path: Path, state_dir: Path) -> Path:
    given = Path(sqlite_path).expanduser()
    real = Path(os.path.realpath(given))
    roots = [
        Path.home() / ".marketreview" / "backups",
        state_dir / "pull-backups",
    ]
    for candidate in (given if given.is_absolute() else given.resolve(), real):
        for root in roots:
            if candidate == root or root in candidate.parents:
                raise MarketReviewError(
                    code="TARGET_FORBIDDEN",
                    message="下载目标位于备份目录或已登记快照中，未修改目标。",
                )
    for protected in _protected_paths(state_dir):
        protected_real = Path(os.path.realpath(protected))
        if real == protected_real:
            raise MarketReviewError(
                code="TARGET_FORBIDDEN",
                message="下载目标是已登记的迁移前快照，未修改目标。",
            )
        if real.exists() and protected_real.exists() and _inode(real) == _inode(protected_real):
            raise MarketReviewError(
                code="TARGET_FORBIDDEN",
                message="下载目标与已登记快照是同一文件，未修改目标。",
            )
    return real


def _push_new(
    conn: sqlite3.Connection,
    transport: Any,
    *,
    sqlite_path: Path,
    project_id: str,
    ledger_id: str,
    state_dir: Path,
    choices: Sequence[GroupChoice],
) -> dict[str, Any]:
    local_groups, baselines, covered, authorizations = _read_state(conn, project_id, ledger_id)
    snapshot = _fetch_snapshot(transport)
    cloud_groups = groups_from_snapshot(snapshot)
    classified, invalid_refs = _classify(
        local_groups, cloud_groups, baselines, covered, snapshot, authorizations
    )
    mark_authorizations_invalid(
        conn,
        project_id=project_id,
        ledger_id=ledger_id,
        authorizations=authorizations,
        refs=invalid_refs,
    )
    commits, auth_items, _blocked = _resolve_choices(classified, choices, upload=True)
    save_keep_cloud_authorizations(
        conn,
        project_id=project_id,
        ledger_id=ledger_id,
        observed_revision=snapshot["revision"],
        items=[_auth_item(item) for item in auth_items],
    )
    same_items = [item for item in classified if item.category == "same"]
    if not commits:
        commit_local_push_success(
                conn,
                project_id=project_id,
                ledger_id=ledger_id,
                operation_id=None,
                committed_groups=[],
                committed_revision=snapshot["revision"],
                same_items=same_items,
                digest=None,
                backup_path=None,
            report=None,
        )
        return _push_report(
            sqlite_path=sqlite_path,
            project_id=project_id,
            ledger_id=ledger_id,
            revision=snapshot["revision"],
            classified=classified,
            auth_items=auth_items,
            committed=[],
            failed=False,
            unknown=False,
        )
    groups = [commit_body(group) for group in commits]
    groups.sort(key=lambda item: group_ref(item["group_kind"], item["group_key"]))
    expected = snapshot["revision"]
    request_digest = digest_of({"expected_revision": expected, "groups": groups})
    operation_id = "sync-" + uuid.uuid4().hex
    request = {
        "schema_version": SCHEMA_VERSION,
        "operation_id": operation_id,
        "project_id": project_id,
        "ledger_id": ledger_id,
        "request_digest": request_digest,
        "expected_revision": expected,
        "groups": groups,
    }
    _reject_if_too_large(request)
    pending = {
        "kind": "sync-push",
        "format_version": 1,
        "operation_id": operation_id,
        "request_digest": request_digest,
        "project_id": project_id,
        "ledger_id": ledger_id,
        "expected_revision": expected,
        "groups": groups,
        "same_groups": [_same_record(item) for item in same_items],
        "request": request,
        "categories": [_dump_classified(item) for item in classified],
        "auth_refs": [item.ref for item in auth_items],
    }
    save_open_pending(state_dir, pending)
    try:
        result = _call_commit(transport, request)
    except RemoteStoreError as exc:
        if exc.code in DEFINITE_REJECT:
            close_pending(state_dir, result="rejected")
            return _push_report(
                sqlite_path=sqlite_path,
                project_id=project_id,
                ledger_id=ledger_id,
                revision=expected,
                classified=classified,
                auth_items=auth_items,
                committed=[],
                failed=True,
                unknown=False,
                detail=str(exc),
            )
        return _unknown_report(
            command="push",
            sqlite_path=sqlite_path,
            project_id=project_id,
            ledger_id=ledger_id,
            detail=str(exc),
        )
    return _finish_push_result(
        conn,
        state_dir,
        pending,
        result,
        sqlite_path=sqlite_path,
        project_id=project_id,
        ledger_id=ledger_id,
        classified=classified,
        auth_items=auth_items,
    )


def _resume_push(
    conn: sqlite3.Connection,
    transport: Any,
    pending: Mapping[str, Any],
    *,
    state_dir: Path,
    project_id: str,
    ledger_id: str,
    sqlite_path: Path,
) -> dict[str, Any]:
    if pending.get("project_id") != project_id or pending.get("ledger_id") != ledger_id:
        raise MarketReviewError(
            code="IDENTITY_MISMATCH",
            message="待核验同步不属于当前项目或账本，已停止，不会另起一次提交。",
        )
    stored = _query_result(transport, str(pending.get("operation_id")))
    if stored is None:
        request = pending.get("request")
        if not isinstance(request, dict):
            return _unknown_report(
                command="push",
                sqlite_path=sqlite_path,
                project_id=project_id,
                ledger_id=ledger_id,
                detail="本机提交内容不完整，云端也没有可恢复的结果。",
            )
        try:
            stored = _call_commit(transport, request)
        except RemoteStoreError as exc:
            if exc.code in DEFINITE_REJECT:
                close_pending(state_dir, result="rejected")
                return _status_report(
                    command="push",
                    sqlite_path=sqlite_path,
                    project_id=project_id,
                    ledger_id=ledger_id,
                    status="failed",
                    revision=pending.get("expected_revision"),
                    detail=str(exc),
                )
            return _unknown_report(
                command="push",
                sqlite_path=sqlite_path,
                project_id=project_id,
                ledger_id=ledger_id,
                detail=str(exc),
            )
    return _finish_push_result(
        conn,
        state_dir,
        pending,
        stored,
        sqlite_path=sqlite_path,
        project_id=project_id,
        ledger_id=ledger_id,
        classified=[],
        auth_items=[],
        resumed=True,
    )


def _finish_push_result(
    conn: sqlite3.Connection,
    state_dir: Path,
    pending: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    sqlite_path: Path,
    project_id: str,
    ledger_id: str,
    classified: Sequence[ClassifiedGroup],
    auth_items: Sequence[ClassifiedGroup],
    resumed: bool = False,
) -> dict[str, Any]:
    submitted = pending.get("groups")
    returned_raw = result.get("groups") if isinstance(result, Mapping) else None
    # Local source evidence is usable only when it is a real list. Missing/null/damaged
    # groups fall through to cloud result_payload when that evidence is complete.
    local_ok = isinstance(submitted, list)
    cloud_ok = isinstance(returned_raw, list) and result.get("project_id") == project_id and result.get("ledger_id") == ledger_id
    cloud_ok = cloud_ok and result.get("request_digest") == pending.get("request_digest")
    cloud_ok = cloud_ok and type(result.get("committed_revision")) is int
    returned: list[dict[str, Any]] | None = None
    if cloud_ok:
        try:
            returned = normalize_public_groups(returned_raw)
        except RemoteStoreError:
            cloud_ok = False
            returned = None
    if local_ok and cloud_ok and returned is not None and not same_evidence(submitted, returned):
        return _unknown_report(
            command="push",
            sqlite_path=sqlite_path,
            project_id=project_id,
            ledger_id=ledger_id,
            detail="本机源快照与云端提交结果不一致，未推进基线。",
        )
    if not cloud_ok and not local_ok:
        return _unknown_report(
            command="push",
            sqlite_path=sqlite_path,
            project_id=project_id,
            ledger_id=ledger_id,
            detail="本机源快照和云端提交结果都不完整，未推进基线。",
        )
    if not cloud_ok and local_ok:
        return _unknown_report(
            command="push",
            sqlite_path=sqlite_path,
            project_id=project_id,
            ledger_id=ledger_id,
            detail="云端提交结果不完整，未推进基线。",
        )
    source_groups = list(submitted) if local_ok else list(returned or [])
    same_records = pending.get("same_groups")
    if not isinstance(same_records, list):
        same_records = []
    revision = result["committed_revision"]
    if not classified and isinstance(pending.get("categories"), list):
        classified = [_load_classified(row) for row in pending["categories"]]
    if not auth_items and isinstance(pending.get("auth_refs"), list):
        auth_refs = set(pending["auth_refs"])
        auth_items = [item for item in classified if item.ref in auth_refs]
    report = _push_report(
        sqlite_path=sqlite_path,
        project_id=project_id,
        ledger_id=ledger_id,
        revision=revision,
        classified=classified,
        auth_items=auth_items,
        committed=[group_ref(group["group_kind"], group["group_key"]) for group in source_groups],
        failed=False,
        unknown=False,
        resumed=resumed,
    )
    try:
        commit_local_push_success(
            conn,
            project_id=project_id,
            ledger_id=ledger_id,
            operation_id=str(pending["operation_id"]),
            committed_groups=list(source_groups),
            committed_revision=revision,
            same_items=same_records,
            digest=str(pending["request_digest"]),
            backup_path=None,
            report=report,
        )
    except Exception as exc:
        return _unknown_report(
            command="push",
            sqlite_path=sqlite_path,
            project_id=project_id,
            ledger_id=ledger_id,
            detail=f"云端已提交，但本地基线尚未写完：{exc}",
        )
    close_pending(state_dir, result="confirmed")
    return report


def commit_local_push_success(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    ledger_id: str,
    operation_id: str | None,
    committed_groups: Sequence[Mapping[str, Any]],
    committed_revision: int,
    same_items: Sequence[Any],
    digest: str | None,
    backup_path: str | None,
    report: Mapping[str, Any] | None,
) -> None:
    begin_immediate(conn)
    try:
        current = read_local_groups(conn)
        for group in committed_groups:
            write_baseline(
                conn,
                project_id=project_id,
                ledger_id=ledger_id,
                group=group,
                cloud_revision=committed_revision,
                operation_id=operation_id,
                present=group.get("exists") is True,
            )
        for item in same_items:
            record = item if isinstance(item, Mapping) and "local_group" in item else _same_record(item)
            ref = record["ref"]
            if ref not in current or not same_json_value(current[ref], record["local_group"]):
                continue
            cloud_group = record["cloud_group"]
            write_baseline(
                conn,
                project_id=project_id,
                ledger_id=ledger_id,
                group=cloud_group,
                cloud_revision=committed_revision,
                operation_id=operation_id,
                present=cloud_group.get("exists") is True,
            )
        if operation_id is not None and digest is not None and report is not None:
            save_operation(
                conn,
                operation_id=operation_id,
                project_id=project_id,
                ledger_id=ledger_id,
                kind="push",
                request_digest=digest,
                state="committed",
                result_payload=report,
                backup_path=backup_path,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _pull_new(
    conn: sqlite3.Connection,
    transport: Any,
    *,
    sqlite_path: Path,
    project_id: str,
    ledger_id: str,
    state_dir: Path,
    choices: Sequence[GroupChoice],
    before_local_commit: Callable[[], None] | None,
) -> dict[str, Any]:
    local_groups, baselines, covered, authorizations = _read_state(conn, project_id, ledger_id)
    snapshot = _fetch_snapshot(transport)
    cloud_groups = groups_from_snapshot(snapshot)
    classified, invalid_refs = _classify(
        local_groups, cloud_groups, baselines, covered, snapshot, authorizations
    )
    _commits, auth_items, blocked = _resolve_choices(classified, choices, upload=False)
    if invalid_refs or auth_items:
        mark_authorizations_invalid(
            conn,
            project_id=project_id,
            ledger_id=ledger_id,
            authorizations=authorizations,
            refs=invalid_refs,
        )
        save_keep_cloud_authorizations(
            conn,
            project_id=project_id,
            ledger_id=ledger_id,
            observed_revision=snapshot["revision"],
            items=[_auth_item(item) for item in auth_items],
        )
        authorizations = _reread_authorizations(conn, project_id, ledger_id)
        classified, _ignored = _classify(
            local_groups, cloud_groups, baselines, covered, snapshot, authorizations
        )
        blocked = [
            item
            for item in classified
            if item.category in {"local_change", "local_only", "conflict", "local_delete"}
        ]
    if blocked:
        return _pull_blocked_report(
            sqlite_path=sqlite_path,
            project_id=project_id,
            ledger_id=ledger_id,
            revision=snapshot["revision"],
            classified=classified,
        )
    operation_id = "pull-" + uuid.uuid4().hex
    digest = digest_of(snapshot)
    valid_auth_refs = [item.ref for item in classified if item.category == "authorized"]
    pending = {
        "kind": "sync-pull",
        "format_version": 1,
        "operation_id": operation_id,
        "request_digest": digest,
        "project_id": project_id,
        "ledger_id": ledger_id,
        "snapshot": json.loads(canonical_json(snapshot)),
        "local_groups": json.loads(canonical_json(local_groups)),
        "consume_refs": valid_auth_refs,
    }
    backup_path = _backup_database(sqlite_path, state_dir / "pull-backups" / f"{operation_id}.sqlite3")
    save_open_pending(state_dir, pending)
    try:
        report = _apply_saved_pull(
            conn,
            pending,
            sqlite_path=sqlite_path,
            project_id=project_id,
            ledger_id=ledger_id,
            backup_path=backup_path,
            before_local_commit=before_local_commit,
        )
    except MarketReviewError as exc:
        if exc.code == "SYNC_SOURCE_CHANGED":
            close_pending(state_dir, result="rejected")
        raise
    close_pending(state_dir, result="confirmed")
    return report


def _resume_pull(
    conn: sqlite3.Connection,
    pending: Mapping[str, Any],
    *,
    state_dir: Path,
    project_id: str,
    ledger_id: str,
    sqlite_path: Path,
    before_local_commit: Callable[[], None] | None,
) -> dict[str, Any]:
    if pending.get("project_id") != project_id or pending.get("ledger_id") != ledger_id:
        raise MarketReviewError(
            code="IDENTITY_MISMATCH",
            message="待核验下载不属于当前项目或账本，未覆盖本地数据。",
        )
    existing = get_operation(conn, str(pending.get("operation_id")))
    if existing is not None and existing["state"] == "committed":
        close_pending(state_dir, result="confirmed")
        return existing["result_payload"]
    local_groups, _baselines, _covered, _auths = _read_state(conn, project_id, ledger_id)
    if not same_json_value(local_groups, pending.get("local_groups")):
        return _unknown_report(
            command="pull",
            sqlite_path=sqlite_path,
            project_id=project_id,
            ledger_id=ledger_id,
            detail="本机内容与下载前不一致，未覆盖，待核验仍打开。",
        )
    backup_path = _backup_database(
        sqlite_path,
        state_dir / "pull-backups" / f"{pending['operation_id']}-resume.sqlite3",
    )
    try:
        report = _apply_saved_pull(
            conn,
            pending,
            sqlite_path=sqlite_path,
            project_id=project_id,
            ledger_id=ledger_id,
            backup_path=str(backup_path),
            before_local_commit=before_local_commit,
        )
    except MarketReviewError as exc:
        if exc.code == "SYNC_SOURCE_CHANGED":
            return _unknown_report(
                command="pull",
                sqlite_path=sqlite_path,
                project_id=project_id,
                ledger_id=ledger_id,
                detail=str(exc),
            )
        raise
    close_pending(state_dir, result="confirmed")
    return report


def _apply_saved_pull(
    conn: sqlite3.Connection,
    pending: Mapping[str, Any],
    *,
    sqlite_path: Path,
    project_id: str,
    ledger_id: str,
    backup_path: str,
    before_local_commit: Callable[[], None] | None,
) -> dict[str, Any]:
    snapshot = pending["snapshot"]
    try:
        begin_immediate(conn)
    except sqlite3.OperationalError as exc:
        raise _busy(exc) from exc
    try:
        current = read_local_groups(conn)
        if not same_json_value(current, pending["local_groups"]):
            raise MarketReviewError(
                code="SYNC_SOURCE_CHANGED",
                message="本地内容在下载前已变化，未覆盖。",
            )
        if before_local_commit is not None:
            before_local_commit()
        _replace_business(conn, snapshot)
        cloud_groups = groups_from_snapshot(snapshot)
        actual = read_local_groups(conn)
        if not same_json_value(actual, cloud_groups):
            raise MarketReviewError(
                code="INCOMPLETE_RESPONSE",
                message="写入后的本地内容与云端快照不一致，已回滚。",
            )
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise MarketReviewError(code="INCOMPLETE_RESPONSE", message="写入后的外键检查未通过，已回滚。")
        replace_coverage(
            conn,
            project_id=project_id,
            ledger_id=ledger_id,
            snapshot_revision=snapshot["revision"],
            pull_operation_id=str(pending["operation_id"]),
        )
        for group in cloud_groups.values():
            write_baseline(
                conn,
                project_id=project_id,
                ledger_id=ledger_id,
                group=group,
                cloud_revision=snapshot["revision"],
                operation_id=str(pending["operation_id"]),
                present=True,
            )
        consume_authorizations(
            conn,
            project_id=project_id,
            ledger_id=ledger_id,
            refs=list(pending.get("consume_refs") or []),
        )
        report = {
            "command": "pull",
            "sqlite_path": str(sqlite_path),
            "project_id": project_id,
            "ledger_id": ledger_id,
            "status": "completed",
            "revision": snapshot["revision"],
            "backup_path": backup_path,
            "operation_id": pending["operation_id"],
            "groups": sorted(cloud_groups),
        }
        save_operation(
            conn,
            operation_id=str(pending["operation_id"]),
            project_id=project_id,
            ledger_id=ledger_id,
            kind="pull",
            request_digest=str(pending["request_digest"]),
            state="committed",
            result_payload=report,
            backup_path=backup_path,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return report


def _replace_business(conn: sqlite3.Connection, snapshot: Mapping[str, Any]) -> None:
    for table in (
        "daily_price_limit_event_reason",
        "daily_price_limit_event_sector",
        "daily_price_limit_event_detail",
        "daily_price_limit_event",
        "daily_market_review",
    ):
        conn.execute(f"DELETE FROM {table}")
    for review in snapshot["reviews"]:
        columns = list(review.keys())
        conn.execute(
            f"INSERT INTO daily_market_review ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [_sql_value(review[name]) for name in columns],
        )
    for event in snapshot["events"]:
        conn.execute(
            """
            INSERT INTO daily_price_limit_event (
                trade_date, market, code, name, direction, closed_at_limit,
                limit_rate_bp, streak_height, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["trade_date"],
                event["market"],
                event["code"],
                event["name"],
                event["direction"],
                1 if event["closed_at_limit"] is True else 0,
                event["limit_rate_bp"],
                event["streak_height"],
                event["created_at"],
                event["updated_at"],
            ),
        )
    for detail in snapshot["details"]:
        conn.execute(
            """
            INSERT INTO daily_price_limit_event_detail (
                trade_date, market, code, direction,
                previous_turnover_amount, auction_amount, previous_close, open_price,
                turnover_amount, turnover_rate, is_leader, note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                detail["trade_date"],
                detail["market"],
                detail["code"],
                detail["direction"],
                _sql_float(detail["previous_turnover_amount"]),
                _sql_float(detail["auction_amount"]),
                _sql_float(detail["previous_close"]),
                _sql_float(detail["open_price"]),
                _sql_float(detail["turnover_amount"]),
                _sql_float(detail["turnover_rate"]),
                _sql_leader(detail["is_leader"]),
                detail["note"],
                detail["created_at"],
                detail["updated_at"],
            ),
        )
    for table, rows in (
        ("daily_price_limit_event_sector", snapshot["sectors"]),
        ("daily_price_limit_event_reason", snapshot["reasons"]),
    ):
        for row in rows:
            conn.execute(
                f"""
                INSERT INTO {table} (trade_date, market, code, direction, position, value)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row["trade_date"],
                    row["market"],
                    row["code"],
                    row["direction"],
                    row["position"],
                    row["value"],
                ),
            )


def _backup_database(source: Path, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_conn = sqlite3.connect(source)
    dest_conn = sqlite3.connect(dest)
    try:
        try:
            source_conn.backup(dest_conn)
        except sqlite3.Error as exc:
            raise MarketReviewError(code="BACKUP_FAILED", message=f"备份失败，未修改目标：{exc}") from exc
    finally:
        dest_conn.close()
        source_conn.close()
    check = sqlite3.connect(dest)
    try:
        row = check.execute("PRAGMA integrity_check").fetchone()
    finally:
        check.close()
    if row is None or row[0] != "ok":
        dest.unlink(missing_ok=True)
        raise MarketReviewError(code="BACKUP_FAILED", message="备份校验失败，未修改目标。")
    return str(dest)


def _resolve_choices(
    classified: Sequence[ClassifiedGroup],
    choices: Sequence[GroupChoice],
    *,
    upload: bool,
) -> tuple[list[dict[str, Any]], list[ClassifiedGroup], list[ClassifiedGroup]]:
    selected: dict[str, str] = {}
    for choice in choices:
        if choice.action not in {"keep_cloud", "adopt_local", "restore_cloud", "delete_on_cloud"}:
            raise MarketReviewError(code="INVALID_REQUEST", message=f"未知同步选择：{choice.action}")
        if choice.ref in selected:
            raise MarketReviewError(code="INVALID_REQUEST", message=f"同步组重复选择：{choice.ref}")
        selected[choice.ref] = choice.action
    commits: list[dict[str, Any]] = []
    authorizations: list[ClassifiedGroup] = []
    blocked: list[ClassifiedGroup] = []
    seen: set[str] = set()
    for item in classified:
        seen.add(item.ref)
        action = selected.get(item.ref)
        if item.category in {"local_change", "local_only"}:
            if action is None:
                if upload:
                    commits.append(commit_body(item.local_group))
                else:
                    blocked.append(item)
            elif action == "keep_cloud":
                authorizations.append(item)
            else:
                raise _bad_choice(item.ref, action)
        elif item.category == "conflict":
            if action is None:
                blocked.append(item)
            elif action == "keep_cloud":
                authorizations.append(item)
            elif action == "adopt_local" and upload:
                commits.append(commit_body(item.local_group))
            else:
                raise _bad_choice(item.ref, action)
        elif item.category == "local_delete":
            if action is None:
                blocked.append(item)
            elif action == "restore_cloud":
                authorizations.append(item)
            elif action == "delete_on_cloud" and upload:
                commits.append(commit_body(item.local_group))
            else:
                raise _bad_choice(item.ref, action)
        elif action is not None:
            raise _bad_choice(item.ref, action)
    unknown = [ref for ref in selected if ref not in seen]
    if unknown:
        raise MarketReviewError(
            code="INVALID_REQUEST",
            message="选择没有对应的同步组：" + ", ".join(unknown),
        )
    return commits, authorizations, blocked


def _bad_choice(ref: str, action: str) -> MarketReviewError:
    return MarketReviewError(code="INVALID_REQUEST", message=f"同步组 {ref} 不能使用选择 {action}")


def _auth_item(item: ClassifiedGroup) -> dict[str, Any]:
    return {
        "group_kind": item.group_kind,
        "group_key": item.group_key,
        "local_payload": item.local_group,
        "cloud_payload": item.cloud_group,
        "baseline_payload": item.baseline_view,
    }


def _dump_classified(item: ClassifiedGroup) -> dict[str, Any]:
    return {
        "ref": item.ref,
        "group_kind": item.group_kind,
        "group_key": item.group_key,
        "category": item.category,
        "local_group": item.local_group,
        "cloud_group": item.cloud_group,
        "baseline_view": item.baseline_view,
    }


def _load_classified(row: Mapping[str, Any]) -> ClassifiedGroup:
    return ClassifiedGroup(
        ref=row["ref"],
        group_kind=row["group_kind"],
        group_key=dict(row["group_key"]),
        category=row["category"],
        local_group=dict(row["local_group"]),
        cloud_group=dict(row["cloud_group"]),
        baseline_view=dict(row["baseline_view"]),
    )


def _same_record(item: ClassifiedGroup) -> dict[str, Any]:
    return {
        "ref": item.ref,
        "local_group": item.local_group,
        "cloud_group": item.cloud_group,
    }


def _classify(
    local_groups: Mapping[str, Any],
    cloud_groups: Mapping[str, Any],
    baselines: Mapping[str, Any],
    covered: bool,
    snapshot: Mapping[str, Any],
    authorizations: Mapping[str, Any],
) -> tuple[list[ClassifiedGroup], list[str]]:
    from .sync_groups import classify_ledger

    return classify_ledger(
        local_groups=local_groups,
        cloud_groups=cloud_groups,
        baselines=baselines,
        covered=covered,
        history=snapshot_history(snapshot),
        authorizations=authorizations,
    )


def _push_report(
    *,
    sqlite_path: Path,
    project_id: str,
    ledger_id: str,
    revision: int,
    classified: Sequence[ClassifiedGroup],
    auth_items: Sequence[ClassifiedGroup],
    committed: Sequence[str],
    failed: bool,
    unknown: bool,
    detail: str | None = None,
    resumed: bool = False,
) -> dict[str, Any]:
    authorized_refs = {item.ref for item in auth_items}
    download = []
    authorized = []
    conflicts = []
    local_deletes = []
    same = []
    for item in classified:
        if item.ref in authorized_refs or item.category == "authorized":
            authorized.append(
                {"group": item.ref, "reason": "已授权保留云端，待下载", "requires_choice": False}
            )
        elif item.category == "same":
            same.append(
                {
                    "group": item.ref,
                    "audit_time_matches": same_json_value(item.local_group, item.cloud_group),
                    "business_matches": same_business(item.local_group, item.cloud_group),
                }
            )
        elif item.category in {"cloud_ahead", "cloud_only"}:
            download.append(
                {"group": item.ref, "reason": "云端更新待下载", "requires_choice": False}
            )
        elif item.category == "conflict" and item.ref not in committed:
            conflicts.append(_conflict_entry(item))
        elif item.category == "local_delete" and item.ref not in committed:
            local_deletes.append(_local_delete_entry(item))
    status = _status(
        committed=list(committed),
        same=[item["group"] for item in same],
        download=download,
        authorized=authorized,
        conflicts=conflicts,
        local_deletes=local_deletes,
        failed=failed,
        unknown=unknown,
    )
    report = {
        "command": "push",
        "sqlite_path": str(sqlite_path),
        "project_id": project_id,
        "ledger_id": ledger_id,
        "status": status,
        "revision": revision,
        "committed": list(committed),
        "same": same,
        "download": download,
        "authorized_download": authorized,
        "conflicts": conflicts,
        "local_deletes": local_deletes,
        "resumed": resumed,
    }
    if detail:
        report["detail"] = detail
    return report


def _pull_blocked_report(
    *,
    sqlite_path: Path,
    project_id: str,
    ledger_id: str,
    revision: int,
    classified: Sequence[ClassifiedGroup],
) -> dict[str, Any]:
    base = _push_report(
        sqlite_path=sqlite_path,
        project_id=project_id,
        ledger_id=ledger_id,
        revision=revision,
        classified=classified,
        auth_items=[],
        committed=[],
        failed=False,
        unknown=False,
    )
    base["command"] = "pull"
    base["wrote"] = False
    base["local_changes"] = [
        {
            "group": item.ref,
            "reason": "只有本地改变",
            "options": [
                {"action": "keep_cloud", "label": "保留云端"},
                {"action": "upload", "label": "先上传"},
            ],
        }
        for item in classified
        if item.category == "local_change"
    ]
    base["local_only"] = [
        {
            "group": item.ref,
            "reason": "本地独有",
            "options": [
                {"action": "keep_cloud", "label": "保留云端"},
                {"action": "upload", "label": "先上传"},
            ],
        }
        for item in classified
        if item.category == "local_only"
    ]
    blockers = [item["group"] for item in base["conflicts"] + base["local_deletes"]]
    blockers.extend(item["group"] for item in base["local_changes"] + base["local_only"])
    base["blockers"] = blockers
    if blockers:
        base["status"] = "needs_resolution"
    return base


def _conflict_entry(item: ClassifiedGroup) -> dict[str, Any]:
    deletes = item.local_group.get("exists") is not True
    return {
        "group": item.ref,
        "reason": "冲突",
        "local": item.local_group,
        "cloud": item.cloud_group,
        "options": [
            {"action": "keep_cloud", "label": "保留云端"},
            {
                "action": "adopt_local",
                "label": "采用本地",
                "deletes_cloud_group": deletes,
            },
        ],
    }


def _local_delete_entry(item: ClassifiedGroup) -> dict[str, Any]:
    return {
        "group": item.ref,
        "reason": "本地删除待处理",
        "local": item.local_group,
        "cloud": item.cloud_group,
        "options": [
            {"action": "restore_cloud", "label": "把云端恢复到本地"},
            {"action": "delete_on_cloud", "label": "在云端删除"},
        ],
    }


def _status(
    *,
    committed: Sequence[str],
    same: Sequence[str],
    download: Sequence[Any],
    authorized: Sequence[Any],
    conflicts: Sequence[Any],
    local_deletes: Sequence[Any],
    failed: bool,
    unknown: bool,
) -> str:
    if unknown:
        return "unknown"
    if failed:
        return "failed"
    pending = bool(download or authorized or conflicts or local_deletes)
    done = bool(committed or same)
    if pending and done:
        return "partial"
    if pending:
        return "needs_resolution"
    return "completed"


def _status_report(**kwargs: Any) -> dict[str, Any]:
    detail = kwargs.pop("detail", None)
    report = dict(kwargs)
    if detail:
        report["detail"] = detail
    return report


def _unknown_report(
    *,
    command: str,
    sqlite_path: Path,
    project_id: str,
    ledger_id: str,
    detail: str,
) -> dict[str, Any]:
    return {
        "command": command,
        "sqlite_path": str(sqlite_path),
        "project_id": project_id,
        "ledger_id": ledger_id,
        "status": "unknown",
        "detail": detail,
    }


def _fetch_snapshot(transport: Any) -> dict[str, Any]:
    payload = transport.call(
        "marketreview_sync_snapshot",
        {"schema_version": SCHEMA_VERSION},
        write=False,
    )
    if not isinstance(payload, dict):
        raise RemoteStoreError("云端快照不完整。", code="INCOMPLETE_RESPONSE")
    return payload


def _call_commit(transport: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    payload = transport.call("marketreview_sync_commit", request, write=True)
    if not isinstance(payload, dict):
        raise RemoteStoreError("云端提交结果不完整。", code="REMOTE_RESULT_UNKNOWN")
    return payload


def _query_result(transport: Any, operation_id: str) -> dict[str, Any] | None:
    try:
        payload = transport.call(
            "marketreview_sync_result",
            {"schema_version": SCHEMA_VERSION, "operation_id": operation_id},
            write=False,
        )
    except RemoteStoreError as exc:
        if exc.code == "OPERATION_NOT_FOUND":
            return None
        raise RemoteStoreError(
            "还不能确认原提交是否结束。",
            code="REMOTE_RESULT_UNKNOWN",
        ) from exc
    if not isinstance(payload, dict):
        raise RemoteStoreError("云端提交结果不完整。", code="REMOTE_RESULT_UNKNOWN")
    return payload


def _reject_if_too_large(request: Mapping[str, Any]) -> None:
    body = json.dumps({"p_request": request}, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_BODY_BYTES:
        raise MarketReviewError(code="REQUEST_TOO_LARGE", message="提交超过 8MB，整次失败，没有拆批。")


def _read_state(
    conn: sqlite3.Connection, project_id: str, ledger_id: str
) -> tuple[dict[str, Any], dict[str, Any], bool, dict[str, Any]]:
    try:
        begin_immediate(conn)
    except sqlite3.OperationalError as exc:
        raise _busy(exc) from exc
    try:
        return (
            read_local_groups(conn),
            read_baselines(conn, project_id=project_id, ledger_id=ledger_id),
            has_coverage(conn, project_id=project_id, ledger_id=ledger_id),
            read_authorizations(conn, project_id=project_id, ledger_id=ledger_id),
        )
    finally:
        conn.rollback()


def _reread_authorizations(
    conn: sqlite3.Connection, project_id: str, ledger_id: str
) -> dict[str, Any]:
    _groups, _baselines, _covered, auths = _read_state(conn, project_id, ledger_id)
    return auths


def _open_db(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    conn = connect(path)
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    ensure_sync_schema(conn)
    from .sqlite_schema import init_db

    init_db(conn)
    return conn


def _open_pending(state_dir: Path) -> dict[str, Any] | None:
    pending = read_pending(state_dir)
    if pending is None or pending.get("status") != "open":
        return None
    return pending


def _protected_paths(state_dir: Path) -> list[str]:
    path = state_dir / "protected-snapshots.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MarketReviewError(
            code="TARGET_FORBIDDEN",
            message="已登记快照清单无法读取，未修改目标。",
        ) from exc
    paths = payload.get("paths") if isinstance(payload, dict) else None
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        raise MarketReviewError(
            code="TARGET_FORBIDDEN",
            message="已登记快照清单无法读取，未修改目标。",
        )
    return paths


def _inode(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def _require_token(value: str, field: str) -> None:
    import re

    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        raise MarketReviewError(code="INVALID_REQUEST", message=f"{field} 格式不合法")


def _busy(exc: sqlite3.OperationalError) -> MarketReviewError:
    text = str(exc).lower()
    if "locked" in text or "busy" in text:
        return MarketReviewError(code="TARGET_BUSY", message="目标数据库正被占用，未修改数据。")
    return MarketReviewError(code="DB_UNAVAILABLE", message=f"数据库不可用（状态未知）：{exc}")


def _sql_value(value: Any) -> Any:
    if type(value) is bool:
        raise MarketReviewError(code="INVALID_TYPE", message="复盘字段不能是布尔值。")
    return value


def _sql_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _sql_leader(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if value is True else 0

