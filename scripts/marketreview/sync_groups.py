"""Sync group payloads, business equality, and L/C/B classification.

Business equality ignores created_at and updated_at. Authorization rechecks
compare those timestamps strictly and never substitute a fresh hash of the
current content for the saved snapshot.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .errors import MarketReviewError, RemoteStoreError
from .schema import ATOMIC_FIELD_NAMES
from .write_gate import same_json_value

SCHEMA_VERSION = 1
INT_REVIEW_FIELDS = frozenset({"advancing_count", "declining_count", "pullback_count"})
FLOAT_REVIEW_FIELDS = frozenset(ATOMIC_FIELD_NAMES - INT_REVIEW_FIELDS)
DETAIL_FLOAT_FIELDS = (
    "previous_turnover_amount",
    "auction_amount",
    "previous_close",
    "open_price",
    "turnover_amount",
    "turnover_rate",
)
AUDIT_FIELDS = frozenset({"created_at", "updated_at"})
RESURRECTION_KINDS = frozenset({"delete", "direction_replace"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_of(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def group_ref(kind: str, key: Mapping[str, Any]) -> str:
    if kind == "review":
        return f"review:{key['trade_date']}"
    return f"event:{key['trade_date']}:{key['market']}:{key['code']}"


def parse_group_ref(text: str) -> tuple[str, dict[str, str]]:
    parts = text.split(":")
    if len(parts) == 2 and parts[0] == "review" and parts[1]:
        return "review", {"trade_date": parts[1]}
    if len(parts) == 4 and parts[0] == "event" and all(parts[1:]):
        return "event", {"trade_date": parts[1], "market": parts[2], "code": parts[3]}
    raise MarketReviewError(code="INVALID_REQUEST", message=f"无法识别同步组：{text}")


def absent_group(kind: str, key: Mapping[str, Any]) -> dict[str, Any]:
    return {"group_kind": kind, "group_key": dict(key), "exists": False}


def no_baseline_view() -> dict[str, Any]:
    return {"baseline_state": "no_baseline", "group": None}


def confirmed_absent_view(kind: str, key: Mapping[str, Any]) -> dict[str, Any]:
    return {"baseline_state": "confirmed_absent", "group": absent_group(kind, key)}


def present_view(group: Mapping[str, Any]) -> dict[str, Any]:
    return {"baseline_state": "present", "group": json.loads(canonical_json(group))}


def strip_audit(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_audit(item)
            for key, item in value.items()
            if key not in AUDIT_FIELDS
        }
    if isinstance(value, list):
        return [strip_audit(item) for item in value]
    return value


def same_business(left: Any, right: Any) -> bool:
    return same_json_value(strip_audit(left), strip_audit(right))


def commit_body(group: Mapping[str, Any]) -> dict[str, Any]:
    if group.get("exists") is True:
        return json.loads(canonical_json(group))
    return {
        "group_kind": group["group_kind"],
        "group_key": dict(group["group_key"]),
        "exists": False,
    }


def same_evidence(submitted: Sequence[Mapping[str, Any]], returned: Sequence[Mapping[str, Any]]) -> bool:
    if len(submitted) != len(returned):
        return False
    # PG jsonb emits whole floats as JSON integers; restore Python types before compare.
    left = {_ref_of(item): commit_body(normalize_public_group(item)) for item in submitted}
    right = {_ref_of(item): commit_body(normalize_public_group(item)) for item in returned}
    if left.keys() != right.keys():
        return False
    return all(same_json_value(left[ref], right[ref]) for ref in left)


def normalize_public_group(group: Mapping[str, Any]) -> dict[str, Any]:
    """Restore Python float/int/bool after JSON transport.

    PostgreSQL ``jsonb_build_object`` turns whole ``double precision`` values into
    JSON integers (``11``). Local SQLite and Python keep them as floats (``11.0``).
    Snapshot reads already coerce via ``_review_from_mapping``; sync commit
    ``result_payload`` groups need the same restore before evidence compare.
    """
    if not isinstance(group, Mapping):
        raise RemoteStoreError("同步组不是对象。", code="INCOMPLETE_RESPONSE")
    kind = group.get("group_kind")
    key = group.get("group_key")
    exists = group.get("exists")
    if kind not in {"review", "event"} or not isinstance(key, dict) or type(exists) is not bool:
        raise RemoteStoreError("同步组结构不完整。", code="INCOMPLETE_RESPONSE")
    body: dict[str, Any] = {
        "group_kind": kind,
        "group_key": dict(key),
        "exists": exists,
    }
    if exists is not True:
        return body
    if kind == "review":
        review = group.get("review")
        if not isinstance(review, dict):
            raise RemoteStoreError("复盘组缺少 review。", code="INCOMPLETE_RESPONSE")
        body["review"] = _review_from_mapping(review)
        return body
    events = group.get("events")
    if not isinstance(events, list) or not events:
        raise RemoteStoreError("事件组缺少 events。", code="INCOMPLETE_RESPONSE")
    body["events"] = [_normalize_public_event(item) for item in events]
    return body


def normalize_public_groups(groups: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_public_group(item) for item in groups]


def _normalize_public_event(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise RemoteStoreError("同步事件不是对象。", code="INCOMPLETE_RESPONSE")
    detail = event.get("detail")
    detail_exists = event.get("detail_exists")
    if type(detail_exists) is not bool:
        raise RemoteStoreError("同步事件 detail_exists 不合法。", code="INCOMPLETE_RESPONSE")
    if detail_exists:
        if not isinstance(detail, dict):
            raise RemoteStoreError("同步事件明细不完整。", code="INCOMPLETE_RESPONSE")
        detail_payload = _detail_payload(detail)
    else:
        detail_payload = None
    sectors = event.get("sectors")
    reasons = event.get("limit_up_reasons")
    if not isinstance(sectors, list) or not isinstance(reasons, list):
        raise RemoteStoreError("同步事件列表字段不完整。", code="INCOMPLETE_RESPONSE")
    return {
        "direction": event["direction"],
        "name": event["name"],
        "closed_at_limit": bool(event["closed_at_limit"]),
        "limit_rate_bp": event["limit_rate_bp"],
        "streak_height": event["streak_height"],
        "created_at": event["created_at"],
        "updated_at": event["updated_at"],
        "detail_exists": detail_exists,
        "detail": detail_payload,
        "sectors": list(sectors),
        "limit_up_reasons": list(reasons),
    }


def _ref_of(group: Mapping[str, Any]) -> str:
    return group_ref(str(group["group_kind"]), group["group_key"])


@dataclass(frozen=True)
class ClassifiedGroup:
    ref: str
    group_kind: str
    group_key: dict[str, str]
    category: str
    local_group: dict[str, Any]
    cloud_group: dict[str, Any]
    baseline_view: dict[str, Any]


def classify_ledger(
    *,
    local_groups: Mapping[str, Mapping[str, Any]],
    cloud_groups: Mapping[str, Mapping[str, Any]],
    baselines: Mapping[str, Mapping[str, Any]],
    covered: bool,
    history: Sequence[Mapping[str, Any]],
    authorizations: Mapping[str, Mapping[str, Any]],
) -> tuple[list[ClassifiedGroup], list[str]]:
    usable_authorizations = {
        ref: auth
        for ref, auth in authorizations.items()
        if isinstance(auth.get("local_payload"), (dict, type(None)))
        and "local_payload" in auth
        and not str(ref).startswith("corrupt:")
    }
    invalid_authorizations = [ref for ref in authorizations if ref not in usable_authorizations]
    refs = set(local_groups) | set(cloud_groups) | set(baselines) | set(usable_authorizations)
    classified: list[ClassifiedGroup] = []
    for ref in sorted(refs, key=_ref_sort):
        kind, key = _split_ref(ref)
        local_group = dict(local_groups[ref]) if ref in local_groups else absent_group(kind, key)
        cloud_group = dict(cloud_groups[ref]) if ref in cloud_groups else absent_group(kind, key)
        if ref in baselines:
            baseline_view = json.loads(canonical_json(baselines[ref]))
        elif covered:
            baseline_view = confirmed_absent_view(kind, key)
        else:
            baseline_view = no_baseline_view()
        authorization = usable_authorizations.get(ref)
        category, auth_ok = _category(
            local_group,
            cloud_group,
            baseline_view,
            _history_blocks(history, kind, key),
            authorization,
        )
        if authorization is not None and authorization.get("state") == "active" and not auth_ok:
            invalid_authorizations.append(ref)
        classified.append(
            ClassifiedGroup(
                ref=ref,
                group_kind=kind,
                group_key=dict(key),
                category=category,
                local_group=local_group,
                cloud_group=cloud_group,
                baseline_view=baseline_view,
            )
        )
    return classified, invalid_authorizations


def _category(
    local_group: Mapping[str, Any],
    cloud_group: Mapping[str, Any],
    baseline_view: Mapping[str, Any],
    history_blocks: bool,
    authorization: Mapping[str, Any] | None,
) -> tuple[str, bool]:
    auth_ok = authorization_matches(authorization, local_group, cloud_group, baseline_view)
    if auth_ok:
        return "authorized", True
    state = baseline_view.get("baseline_state")
    if state == "no_baseline":
        return _first_join(local_group, cloud_group, history_blocks), False
    if state not in {"present", "confirmed_absent"} or not isinstance(baseline_view.get("group"), dict):
        raise MarketReviewError(code="BASELINE_CORRUPT", message="共同基线损坏，不能按首次接入重建。")
    return _three_way(local_group, cloud_group, baseline_view["group"]), False


def authorization_matches(
    authorization: Mapping[str, Any] | None,
    local_group: Mapping[str, Any],
    cloud_group: Mapping[str, Any],
    baseline_view: Mapping[str, Any],
) -> bool:
    if authorization is None or authorization.get("state") != "active":
        return False
    if authorization.get("action") != "keep_cloud":
        return False
    saved = {
        "local": authorization.get("local_payload"),
        "cloud": authorization.get("cloud_payload"),
        "baseline": authorization.get("baseline_payload"),
    }
    digests = {
        "local": authorization.get("local_digest"),
        "cloud": authorization.get("cloud_digest"),
        "baseline": authorization.get("baseline_digest"),
    }
    current = {"local": local_group, "cloud": cloud_group, "baseline": baseline_view}
    for name in ("local", "cloud", "baseline"):
        if not isinstance(digests[name], str) or digest_of(saved[name]) != digests[name]:
            return False
        if not same_json_value(saved[name], current[name]):
            return False
    return True


def _first_join(
    local_group: Mapping[str, Any],
    cloud_group: Mapping[str, Any],
    history_blocks: bool,
) -> str:
    if same_business(local_group, cloud_group):
        return "same"
    local_exists = local_group.get("exists") is True
    cloud_exists = cloud_group.get("exists") is True
    if local_exists and not cloud_exists:
        if history_blocks:
            return "conflict"
        return "local_only"
    if cloud_exists and not local_exists:
        return "cloud_only"
    return "conflict"


def _three_way(
    local_group: Mapping[str, Any],
    cloud_group: Mapping[str, Any],
    baseline_group: Mapping[str, Any],
) -> str:
    local_matches_cloud = same_business(local_group, cloud_group)
    local_matches_base = same_business(local_group, baseline_group)
    cloud_matches_base = same_business(cloud_group, baseline_group)
    if local_matches_cloud or (local_matches_base and cloud_matches_base):
        return "same"
    if local_matches_base and not cloud_matches_base:
        return "cloud_ahead"
    if cloud_matches_base and not local_matches_base:
        if local_group.get("exists") is True:
            return "local_change"
        return "local_delete"
    return "conflict"


def _history_blocks(history: Sequence[Mapping[str, Any]], kind: str, key: Mapping[str, Any]) -> bool:
    for row in history:
        if row.get("group_kind") != kind or not isinstance(row.get("group_key"), dict):
            continue
        if group_ref(kind, row["group_key"]) != group_ref(kind, key):
            continue
        if row.get("change_kind") in RESURRECTION_KINDS or row.get("after_exists") is False:
            return True
    return False


def _split_ref(ref: str) -> tuple[str, dict[str, str]]:
    kind, key = parse_group_ref(ref)
    return kind, key


def _ref_sort(ref: str) -> tuple[int, str]:
    return (0 if ref.startswith("review:") else 1, ref)


def read_local_groups(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    columns = ["trade_date", *sorted(ATOMIC_FIELD_NAMES), "created_at", "updated_at"]
    query = "SELECT " + ", ".join(columns) + " FROM daily_market_review ORDER BY trade_date"
    for row in conn.execute(query):
        review = _review_from_mapping(row)
        key = {"trade_date": review["trade_date"]}
        groups[group_ref("review", key)] = {
            "group_kind": "review",
            "group_key": key,
            "exists": True,
            "review": review,
        }
    events = [dict(row) for row in conn.execute(
        """
        SELECT trade_date, market, code, name, direction, closed_at_limit,
               limit_rate_bp, streak_height, created_at, updated_at
        FROM daily_price_limit_event
        ORDER BY trade_date, market, code, direction
        """
    )]
    details = {
        (row["trade_date"], row["market"], row["code"], row["direction"]): dict(row)
        for row in conn.execute(
            """
            SELECT trade_date, market, code, direction,
                   previous_turnover_amount, auction_amount, previous_close, open_price,
                   turnover_amount, turnover_rate, is_leader, note, created_at, updated_at
            FROM daily_price_limit_event_detail
            """
        )
    }
    lists = {
        "sectors": _string_lists(conn, "daily_price_limit_event_sector"),
        "limit_up_reasons": _string_lists(conn, "daily_price_limit_event_reason"),
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    keys: dict[str, dict[str, str]] = {}
    for row in events:
        key = {"trade_date": row["trade_date"], "market": row["market"], "code": row["code"]}
        ref = group_ref("event", key)
        identity = (row["trade_date"], row["market"], row["code"], row["direction"])
        detail_row = details.get(identity)
        keys[ref] = key
        grouped.setdefault(ref, []).append(
            {
                "direction": row["direction"],
                "name": row["name"],
                "closed_at_limit": bool(row["closed_at_limit"]),
                "limit_rate_bp": row["limit_rate_bp"],
                "streak_height": row["streak_height"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "detail_exists": detail_row is not None,
                "detail": None if detail_row is None else _detail_payload(detail_row),
                "sectors": lists["sectors"].get(identity, []),
                "limit_up_reasons": lists["limit_up_reasons"].get(identity, []),
            }
        )
    for ref, items in grouped.items():
        groups[ref] = {
            "group_kind": "event",
            "group_key": keys[ref],
            "exists": True,
            "events": sorted(items, key=lambda item: item["direction"]),
        }
    return groups


def groups_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    _require_snapshot(snapshot)
    groups: dict[str, dict[str, Any]] = {}
    for review in snapshot["reviews"]:
        if not isinstance(review, dict):
            raise RemoteStoreError("云端快照里的复盘行不完整。", code="INCOMPLETE_RESPONSE")
        normalized = _review_from_mapping(review)
        key = {"trade_date": normalized["trade_date"]}
        ref = group_ref("review", key)
        if ref in groups:
            raise RemoteStoreError("云端快照有重复复盘组。", code="INCOMPLETE_RESPONSE")
        groups[ref] = {
            "group_kind": "review",
            "group_key": key,
            "exists": True,
            "review": normalized,
        }
    details = {}
    for row in snapshot["details"]:
        if not isinstance(row, dict):
            raise RemoteStoreError("云端快照明细不完整。", code="INCOMPLETE_RESPONSE")
        identity = (row.get("trade_date"), row.get("market"), row.get("code"), row.get("direction"))
        if identity in details:
            raise RemoteStoreError("云端快照有重复明细。", code="INCOMPLETE_RESPONSE")
        details[identity] = row
    sectors = _snapshot_lists(snapshot["sectors"])
    reasons = _snapshot_lists(snapshot["reasons"])
    grouped: dict[str, dict[str, Any]] = {}
    for row in snapshot["events"]:
        if not isinstance(row, dict):
            raise RemoteStoreError("云端快照事件不完整。", code="INCOMPLETE_RESPONSE")
        key = {
            "trade_date": row["trade_date"],
            "market": row["market"],
            "code": row["code"],
        }
        ref = group_ref("event", key)
        identity = (row["trade_date"], row["market"], row["code"], row["direction"])
        bucket = grouped.setdefault(ref, {"key": key, "events": []})
        detail_row = details.pop(identity, None)
        bucket["events"].append(
            {
                "direction": row["direction"],
                "name": row["name"],
                "closed_at_limit": row["closed_at_limit"] is True,
                "limit_rate_bp": row["limit_rate_bp"],
                "streak_height": row["streak_height"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "detail_exists": detail_row is not None,
                "detail": None if detail_row is None else _detail_payload(detail_row),
                "sectors": sectors.pop(identity, []),
                "limit_up_reasons": reasons.pop(identity, []),
            }
        )
    if details or sectors or reasons:
        raise RemoteStoreError("云端快照有无法归属到事件的明细或列表。", code="INCOMPLETE_RESPONSE")
    for ref, bucket in grouped.items():
        groups[ref] = {
            "group_kind": "event",
            "group_key": bucket["key"],
            "exists": True,
            "events": sorted(bucket["events"], key=lambda item: item["direction"]),
        }
    return groups


def snapshot_history(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    _require_snapshot(snapshot)
    history = snapshot["history"]
    if not isinstance(history, list):
        raise RemoteStoreError("云端快照缺少组历史。", code="INCOMPLETE_RESPONSE")
    return [dict(row) for row in history]


def _require_snapshot(snapshot: Mapping[str, Any]) -> None:
    if snapshot.get("format_version") != 1 or snapshot.get("schema_version") != SCHEMA_VERSION:
        raise RemoteStoreError("云端快照版本与冻结合同不一致。", code="SCHEMA_VERSION_MISMATCH")
    if snapshot.get("complete") is not True:
        raise RemoteStoreError("云端快照不完整。", code="INCOMPLETE_RESPONSE")
    if type(snapshot.get("revision")) is not int or snapshot["revision"] < 0:
        raise RemoteStoreError("云端快照缺少账本版本。", code="INCOMPLETE_RESPONSE")
    counts = snapshot.get("counts")
    if not isinstance(counts, dict):
        raise RemoteStoreError("云端快照缺少计数。", code="INCOMPLETE_RESPONSE")
    for name in ("reviews", "events", "details", "sectors", "reasons", "history", "sync_results"):
        rows = snapshot.get(name)
        if not isinstance(rows, list) or counts.get(name) != len(rows):
            raise RemoteStoreError(f"云端快照的 {name} 计数不一致。", code="INCOMPLETE_RESPONSE")


def _review_from_mapping(row: Mapping[str, Any]) -> dict[str, Any]:
    review: dict[str, Any] = {"trade_date": row["trade_date"]}
    for name in sorted(ATOMIC_FIELD_NAMES):
        value = row[name]
        if name in INT_REVIEW_FIELDS:
            review[name] = None if value is None else value
        elif value is None:
            review[name] = None
        elif type(value) is bool or type(value) not in {int, float}:
            raise RemoteStoreError(f"复盘字段 {name} 不是数值。", code="INCOMPLETE_RESPONSE")
        else:
            review[name] = float(value)
    review["created_at"] = row["created_at"]
    review["updated_at"] = row["updated_at"]
    return review


def _detail_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name in DETAIL_FLOAT_FIELDS:
        value = row[name]
        if value is None:
            payload[name] = None
        elif type(value) is bool or type(value) not in {int, float}:
            raise RemoteStoreError(f"明细字段 {name} 不是数值。", code="INCOMPLETE_RESPONSE")
        else:
            payload[name] = float(value)
    leader = row["is_leader"]
    if leader is None:
        payload["is_leader"] = None
    elif type(leader) is bool:
        payload["is_leader"] = leader
    elif leader in {0, 1}:
        payload["is_leader"] = bool(leader)
    else:
        raise RemoteStoreError("明细 is_leader 不合法。", code="INCOMPLETE_RESPONSE")
    payload["note"] = row["note"]
    payload["created_at"] = row["created_at"]
    payload["updated_at"] = row["updated_at"]
    return payload


def _string_lists(conn: sqlite3.Connection, table: str) -> dict[tuple[str, str, str, str], list[str]]:
    grouped: dict[tuple[str, str, str, str], list[tuple[int, str]]] = {}
    for row in conn.execute(
        f"""
        SELECT trade_date, market, code, direction, position, value
        FROM {table}
        ORDER BY trade_date, market, code, direction, position
        """
    ):
        identity = (row["trade_date"], row["market"], row["code"], row["direction"])
        grouped.setdefault(identity, []).append((row["position"], row["value"]))
    return {identity: [value for _, value in rows] for identity, rows in grouped.items()}


def _snapshot_lists(rows: Sequence[Any]) -> dict[tuple[Any, Any, Any, Any], list[str]]:
    grouped: dict[tuple[Any, Any, Any, Any], list[tuple[int, str]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RemoteStoreError("云端快照列表行不完整。", code="INCOMPLETE_RESPONSE")
        identity = (row.get("trade_date"), row.get("market"), row.get("code"), row.get("direction"))
        grouped.setdefault(identity, []).append((row["position"], row["value"]))
    result = {}
    for identity, items in grouped.items():
        ordered = sorted(items, key=lambda item: item[0])
        result[identity] = [value for _, value in ordered]
    return result
