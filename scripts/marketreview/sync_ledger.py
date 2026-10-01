"""Local SQLite records for ledger identity, baselines, authorizations, and sync results."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Mapping

from .errors import MarketReviewError
from .sync_groups import (
    absent_group,
    canonical_json,
    digest_of,
    group_ref,
    present_view,
)

METADATA_SCHEMA_VERSION = 1

DDL = (
    """
    CREATE TABLE IF NOT EXISTS sync_ledger_singleton (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        ledger_id TEXT NOT NULL,
        metadata_schema_version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_baseline (
        project_id TEXT NOT NULL,
        ledger_id TEXT NOT NULL,
        group_kind TEXT NOT NULL,
        group_key TEXT NOT NULL,
        baseline_state TEXT NOT NULL,
        group_payload TEXT NOT NULL,
        cloud_revision INTEGER NOT NULL,
        confirmed_operation_id TEXT,
        PRIMARY KEY (project_id, ledger_id, group_kind, group_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_coverage (
        project_id TEXT NOT NULL,
        ledger_id TEXT NOT NULL,
        snapshot_revision INTEGER NOT NULL,
        pull_operation_id TEXT NOT NULL,
        PRIMARY KEY (project_id, ledger_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_authorization (
        project_id TEXT NOT NULL,
        ledger_id TEXT NOT NULL,
        group_kind TEXT NOT NULL,
        group_key TEXT NOT NULL,
        decision_id TEXT NOT NULL,
        decision_version INTEGER NOT NULL,
        action TEXT NOT NULL,
        local_payload TEXT NOT NULL,
        cloud_payload TEXT NOT NULL,
        baseline_payload TEXT NOT NULL,
        local_digest TEXT NOT NULL,
        cloud_digest TEXT NOT NULL,
        baseline_digest TEXT NOT NULL,
        observed_cloud_revision INTEGER NOT NULL,
        state TEXT NOT NULL,
        PRIMARY KEY (project_id, ledger_id, group_kind, group_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_operation (
        operation_id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL,
        ledger_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        state TEXT NOT NULL,
        result_payload TEXT NOT NULL,
        backup_path TEXT
    )
    """,
)


def begin_immediate(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            raise MarketReviewError(
                code="TARGET_BUSY",
                message="目标数据库正被占用，未修改数据。",
            ) from exc
        raise


def ensure_sync_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise MarketReviewError(code="DB_UNAVAILABLE", message="同步表初始化不能在已有事务中执行。")
    conn.execute("BEGIN")
    try:
        for statement in DDL:
            conn.execute(statement)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def ensure_ledger(conn: sqlite3.Connection) -> str:
    begin_immediate(conn)
    try:
        row = conn.execute(
            "SELECT ledger_id FROM sync_ledger_singleton WHERE id = 1"
        ).fetchone()
        if row is None:
            ledger_id = "ledger-" + uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO sync_ledger_singleton (id, ledger_id, metadata_schema_version)
                VALUES (1, ?, ?)
                """,
                (ledger_id, METADATA_SCHEMA_VERSION),
            )
        else:
            ledger_id = row["ledger_id"]
        _assert_identity(conn, ledger_id)
        conn.commit()
        return ledger_id
    except Exception:
        conn.rollback()
        raise


def _assert_identity(conn: sqlite3.Connection, ledger_id: str) -> None:
    for table in ("sync_baseline", "sync_coverage", "sync_authorization", "sync_operation"):
        found = conn.execute(
            f"SELECT 1 FROM {table} WHERE ledger_id <> ? LIMIT 1",
            (ledger_id,),
        ).fetchone()
        if found is not None:
            raise MarketReviewError(
                code="IDENTITY_MISMATCH",
                message="本地账本里存在另一套身份的同步记录，已停止，不会按首次接入重建。",
            )


def read_baselines(
    conn: sqlite3.Connection, *, project_id: str, ledger_id: str
) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT group_kind, group_key, baseline_state, group_payload
        FROM sync_baseline
        WHERE project_id = ? AND ledger_id = ?
        """,
        (project_id, ledger_id),
    ).fetchall()
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            key = json.loads(row["group_key"])
            group = json.loads(row["group_payload"])
        except json.JSONDecodeError as exc:
            raise MarketReviewError(code="BASELINE_CORRUPT", message="共同基线无法读取。") from exc
        if not isinstance(key, dict) or not isinstance(group, dict):
            raise MarketReviewError(code="BASELINE_CORRUPT", message="共同基线无法读取。")
        if row["baseline_state"] not in {"present", "confirmed_absent"}:
            raise MarketReviewError(code="BASELINE_CORRUPT", message="共同基线状态无法读取。")
        ref = group_ref(row["group_kind"], key)
        found[ref] = {"baseline_state": row["baseline_state"], "group": group}
    return found


def has_coverage(conn: sqlite3.Connection, *, project_id: str, ledger_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM sync_coverage WHERE project_id = ? AND ledger_id = ?
        """,
        (project_id, ledger_id),
    ).fetchone()
    return row is not None


def read_authorizations(
    conn: sqlite3.Connection, *, project_id: str, ledger_id: str
) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT group_kind, group_key, decision_id, decision_version, action,
               local_payload, cloud_payload, baseline_payload,
               local_digest, cloud_digest, baseline_digest,
               observed_cloud_revision, state
        FROM sync_authorization
        WHERE project_id = ? AND ledger_id = ?
        """,
        (project_id, ledger_id),
    ).fetchall()
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            key = json.loads(row["group_key"])
            local_payload = json.loads(row["local_payload"])
            cloud_payload = json.loads(row["cloud_payload"])
            baseline_payload = json.loads(row["baseline_payload"])
        except json.JSONDecodeError:
            ref = f"corrupt:{row['group_kind']}:{row['group_key']}"
            found[ref] = {
                "state": "active",
                "action": "keep_cloud",
                "local_digest": None,
                "group_kind": row["group_kind"],
                "stored_key": row["group_key"],
            }
            continue
        if not isinstance(key, dict):
            continue
        ref = group_ref(row["group_kind"], key)
        found[ref] = {
            "group_kind": row["group_kind"],
            "group_key": key,
            "decision_id": row["decision_id"],
            "decision_version": row["decision_version"],
            "action": row["action"],
            "local_payload": local_payload,
            "cloud_payload": cloud_payload,
            "baseline_payload": baseline_payload,
            "local_digest": row["local_digest"],
            "cloud_digest": row["cloud_digest"],
            "baseline_digest": row["baseline_digest"],
            "observed_cloud_revision": row["observed_cloud_revision"],
            "state": row["state"],
            "stored_key": row["group_key"],
        }
    return found


def mark_authorizations_invalid(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    ledger_id: str,
    authorizations: Mapping[str, Mapping[str, Any]],
    refs: list[str],
) -> None:
    if not refs:
        return
    begin_immediate(conn)
    try:
        for ref in refs:
            auth = authorizations[ref]
            conn.execute(
                """
                UPDATE sync_authorization
                SET state = 'invalid'
                WHERE project_id = ? AND ledger_id = ? AND group_kind = ? AND group_key = ?
                      AND state = 'active'
                """,
                (project_id, ledger_id, auth["group_kind"], auth["stored_key"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def save_keep_cloud_authorizations(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    ledger_id: str,
    observed_revision: int,
    items: list[Mapping[str, Any]],
) -> None:
    if not items:
        return
    begin_immediate(conn)
    try:
        for item in items:
            kind = item["group_kind"]
            key = item["group_key"]
            stored_key = canonical_json(key)
            previous = conn.execute(
                """
                SELECT decision_version FROM sync_authorization
                WHERE project_id = ? AND ledger_id = ? AND group_kind = ? AND group_key = ?
                """,
                (project_id, ledger_id, kind, stored_key),
            ).fetchone()
            version = 1 if previous is None else int(previous["decision_version"]) + 1
            local_payload = item["local_payload"]
            cloud_payload = item["cloud_payload"]
            baseline_payload = item["baseline_payload"]
            conn.execute(
                """
                INSERT INTO sync_authorization (
                    project_id, ledger_id, group_kind, group_key, decision_id,
                    decision_version, action, local_payload, cloud_payload, baseline_payload,
                    local_digest, cloud_digest, baseline_digest, observed_cloud_revision, state
                ) VALUES (?, ?, ?, ?, ?, ?, 'keep_cloud', ?, ?, ?, ?, ?, ?, ?, 'active')
                ON CONFLICT(project_id, ledger_id, group_kind, group_key) DO UPDATE SET
                    decision_id = excluded.decision_id,
                    decision_version = excluded.decision_version,
                    action = 'keep_cloud',
                    local_payload = excluded.local_payload,
                    cloud_payload = excluded.cloud_payload,
                    baseline_payload = excluded.baseline_payload,
                    local_digest = excluded.local_digest,
                    cloud_digest = excluded.cloud_digest,
                    baseline_digest = excluded.baseline_digest,
                    observed_cloud_revision = excluded.observed_cloud_revision,
                    state = 'active'
                """,
                (
                    project_id,
                    ledger_id,
                    kind,
                    stored_key,
                    "decision-" + uuid.uuid4().hex,
                    version,
                    canonical_json(local_payload),
                    canonical_json(cloud_payload),
                    canonical_json(baseline_payload),
                    digest_of(local_payload),
                    digest_of(cloud_payload),
                    digest_of(baseline_payload),
                    observed_revision,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def write_baseline(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    ledger_id: str,
    group: Mapping[str, Any],
    cloud_revision: int,
    operation_id: str | None,
    present: bool,
) -> None:
    kind = group["group_kind"]
    key = group["group_key"]
    state = "present" if present else "confirmed_absent"
    payload = group if present else absent_group(kind, key)
    conn.execute(
        """
        INSERT INTO sync_baseline (
            project_id, ledger_id, group_kind, group_key, baseline_state,
            group_payload, cloud_revision, confirmed_operation_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id, ledger_id, group_kind, group_key) DO UPDATE SET
            baseline_state = excluded.baseline_state,
            group_payload = excluded.group_payload,
            cloud_revision = excluded.cloud_revision,
            confirmed_operation_id = excluded.confirmed_operation_id
        """,
        (
            project_id,
            ledger_id,
            kind,
            canonical_json(key),
            state,
            canonical_json(payload),
            cloud_revision,
            operation_id,
        ),
    )


def replace_coverage(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    ledger_id: str,
    snapshot_revision: int,
    pull_operation_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO sync_coverage (project_id, ledger_id, snapshot_revision, pull_operation_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(project_id, ledger_id) DO UPDATE SET
            snapshot_revision = excluded.snapshot_revision,
            pull_operation_id = excluded.pull_operation_id
        """,
        (project_id, ledger_id, snapshot_revision, pull_operation_id),
    )
    conn.execute(
        """
        DELETE FROM sync_baseline
        WHERE project_id = ? AND ledger_id = ?
        """,
        (project_id, ledger_id),
    )


def consume_authorizations(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    ledger_id: str,
    refs: list[str],
) -> None:
    for ref in refs:
        kind, key = _stored_key(ref)
        conn.execute(
            """
            UPDATE sync_authorization
            SET state = 'consumed'
            WHERE project_id = ? AND ledger_id = ? AND group_kind = ? AND group_key = ?
                  AND state = 'active'
            """,
            (project_id, ledger_id, kind, canonical_json(key)),
        )


def get_operation(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT operation_id, project_id, ledger_id, kind, request_digest, state,
               result_payload, backup_path
        FROM sync_operation WHERE operation_id = ?
        """,
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "operation_id": row["operation_id"],
        "project_id": row["project_id"],
        "ledger_id": row["ledger_id"],
        "kind": row["kind"],
        "request_digest": row["request_digest"],
        "state": row["state"],
        "result_payload": json.loads(row["result_payload"]),
        "backup_path": row["backup_path"],
    }


def save_operation(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    project_id: str,
    ledger_id: str,
    kind: str,
    request_digest: str,
    state: str,
    result_payload: Mapping[str, Any],
    backup_path: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO sync_operation (
            operation_id, project_id, ledger_id, kind, request_digest, state,
            result_payload, backup_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(operation_id) DO UPDATE SET
            state = excluded.state,
            result_payload = excluded.result_payload,
            backup_path = excluded.backup_path
        """,
        (
            operation_id,
            project_id,
            ledger_id,
            kind,
            request_digest,
            state,
            canonical_json(result_payload),
            backup_path,
        ),
    )


def baseline_payload_for(group: Mapping[str, Any], *, present: bool) -> dict[str, Any]:
    if present:
        return present_view(group)
    return {
        "baseline_state": "confirmed_absent",
        "group": absent_group(group["group_kind"], group["group_key"]),
    }


def _stored_key(ref: str) -> tuple[str, dict[str, str]]:
    from .sync_groups import parse_group_ref

    return parse_group_ref(ref)
