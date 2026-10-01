"""Local write lock and the direction-replacement preimage record.

The pending file lives in the caller's state directory. It stores no credentials.
Tests pass a temporary directory and never touch ``~/.marketreview``.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import RemoteStoreError
from .schema import PRICE_LIMIT_EVENT_DETAIL_SCALAR_FIELD_ORDER

PENDING_FILENAME = "pending-write.json"
LOCK_FILENAME = "write.lock"
PENDING_FORMAT_VERSION = 1
PENDING_KINDS = frozenset({"replace-direction", "sync-push", "sync-pull"})
PENDING_RESULTS = frozenset({"confirmed", "rejected", "not_executed"})
FLOAT_REL_TOL = 1e-12
FLOAT_ABS_TOL = 1e-9


@contextmanager
def exclusive_write(state_dir: Path) -> Iterator[Path]:
    try:
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise RemoteStoreError(
            f"无法创建本机写入状态目录，未发送写请求：{exc.strerror or exc}",
            code="PENDING_UNREADABLE",
        ) from exc
    lock_path = state_dir / LOCK_FILENAME
    try:
        handle = open(lock_path, "a+", encoding="utf-8")
    except OSError as exc:
        raise RemoteStoreError(
            f"无法取得本机写入锁，未发送写请求：{exc.strerror or exc}",
            code="PENDING_UNREADABLE",
        ) from exc
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield state_dir
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def pending_path(state_dir: Path) -> Path:
    return state_dir / PENDING_FILENAME


def assert_no_open_pending(state_dir: Path) -> None:
    record = read_pending(state_dir)
    if record is not None and record.get("status") == "open":
        raise RemoteStoreError(
            "存在未关闭的待核验记录，未发送新的写请求。",
            code="PENDING_WRITE",
        )


def read_pending(state_dir: Path) -> dict[str, Any] | None:
    path = pending_path(state_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoteStoreError(
            "待核验记录无法读取或已损坏，未发送写请求。",
            code="PENDING_UNREADABLE",
        ) from exc
    return _require_pending_record(payload)


def save_open_pending(state_dir: Path, record: dict[str, Any]) -> None:
    body = dict(record)
    body["status"] = "open"
    if "verification_history" not in body:
        body["verification_history"] = []
    _require_pending_record(body)
    _replace_json(pending_path(state_dir), body)


def close_pending(state_dir: Path, *, result: str) -> None:
    current = read_pending(state_dir)
    if current is None or current.get("status") != "open":
        raise RemoteStoreError(
            "待核验记录丢失，未能关闭。",
            code="PENDING_UNREADABLE",
        )
    history = list(current.get("verification_history") or [])
    history.append({"result": result})
    current["verification_history"] = history
    current["status"] = "closed"
    current["result"] = result
    _require_pending_record(current)
    _replace_json(pending_path(state_dir), current)


def _require_pending_record(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        _corrupt()
    if payload.get("format_version") != PENDING_FORMAT_VERSION or type(payload.get("format_version")) is not int:
        _corrupt()
    if type(payload.get("operation_id")) is not str or not payload["operation_id"]:
        _corrupt()
    if payload.get("kind") not in PENDING_KINDS:
        _corrupt()
    if payload.get("status") not in {"open", "closed"}:
        _corrupt()
    history = payload.get("verification_history")
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        _corrupt()
    if payload["status"] == "closed":
        if payload.get("result") not in PENDING_RESULTS or not history:
            _corrupt()
        if history[-1].get("result") != payload["result"]:
            _corrupt()
    kind = payload["kind"]
    if kind == "replace-direction":
        _require_replace_pending(payload)
    else:
        _require_text(payload, "project_id")
        _require_text(payload, "ledger_id")
        _require_text(payload, "request_digest")
        if kind == "sync-push":
            if not isinstance(payload.get("request"), dict):
                _corrupt()
            # groups may be missing, null, or damaged. Cloud-only recovery uses result_payload
            # when local evidence is not a list; wrong shape must not make the whole record unreadable.
        elif not isinstance(payload.get("snapshot"), dict):
            _corrupt()
    return payload


def _require_replace_pending(payload: dict[str, Any]) -> None:
    _require_text(payload, "project_ref")
    _require_text(payload, "started_at")
    if type(payload.get("schema_version")) is not int:
        _corrupt()
    request = payload.get("payload")
    if not isinstance(request, dict) or not isinstance(request.get("event"), dict):
        _corrupt()
    for name in ("trade_date", "market", "code", "old_direction", "batch_time"):
        if type(request.get(name)) is not str or not request[name]:
            _corrupt()
    event = request["event"]
    if type(event.get("direction")) is not str or not event["direction"]:
        _corrupt()
    preimage = payload.get("preimage")
    # ``{}`` and other incomplete shapes must block the gate, including closed records.
    if not isinstance(preimage, dict) or not preimage:
        _corrupt()
    if type(preimage.get("revision")) is not int:
        _corrupt()
    for name in ("trade_date", "market", "code", "old_direction", "new_direction"):
        if type(preimage.get(name)) is not str or not preimage[name]:
            _corrupt()
    if (
        preimage["trade_date"] != request["trade_date"]
        or preimage["market"] != request["market"]
        or preimage["code"] != request["code"]
        or preimage["old_direction"] != request["old_direction"]
        or preimage["new_direction"] != event["direction"]
    ):
        _corrupt()
    # Nested old/new must be complete direction states, not bare ``{}``.
    if not direction_present(preimage.get("old")):
        _corrupt()
    if not direction_absent(preimage.get("new")):
        _corrupt()
    old_event = preimage["old"]["event"]
    if (
        old_event.get("trade_date") != preimage["trade_date"]
        or old_event.get("market") != preimage["market"]
        or old_event.get("code") != preimage["code"]
        or old_event.get("direction") != preimage["old_direction"]
    ):
        _corrupt()


def direction_present(state: Any) -> bool:
    """Return True when a preimage direction side is a complete present state."""
    if not isinstance(state, dict) or state.get("exists") is not True:
        return False
    event = state.get("event")
    if not isinstance(event, dict):
        return False
    for name in (
        "trade_date",
        "market",
        "code",
        "name",
        "direction",
        "created_at",
        "updated_at",
    ):
        if type(event.get(name)) is not str or not event[name]:
            return False
    if type(event.get("closed_at_limit")) is not bool:
        return False
    if type(event.get("limit_rate_bp")) is not int or type(event.get("streak_height")) is not int:
        return False
    if type(state.get("detail_exists")) is not bool:
        return False
    if not _string_list(state.get("sectors")) or not _string_list(state.get("reasons")):
        return False
    detail = state.get("detail")
    if state["detail_exists"]:
        return detail_complete(detail, event=event)
    return detail is None


def direction_absent(state: Any) -> bool:
    """Return True when a preimage direction side is a complete absent state."""
    if not isinstance(state, dict) or state.get("exists") is not False:
        return False
    return (
        state.get("event") is None
        and state.get("detail_exists") is False
        and state.get("detail") is None
        and state.get("sectors") == []
        and state.get("reasons") == []
    )


def detail_complete(detail: Any, *, event: Mapping[str, Any]) -> bool:
    """Return True when detail scalars exist and identity matches the parent event."""
    if not isinstance(detail, dict):
        return False
    for name in ("trade_date", "market", "code", "direction", "created_at", "updated_at"):
        if type(detail.get(name)) is not str or not detail[name]:
            return False
    for name in ("trade_date", "market", "code", "direction"):
        if detail.get(name) != event.get(name):
            return False
    for name in PRICE_LIMIT_EVENT_DETAIL_SCALAR_FIELD_ORDER:
        if name not in detail:
            return False
    return True


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(type(item) is str for item in value)


def _require_text(payload: dict[str, Any], name: str) -> None:
    if type(payload.get(name)) is not str or not payload[name]:
        _corrupt()


def _corrupt() -> None:
    raise RemoteStoreError(
        "待核验记录无法读取或已损坏，未发送写请求。",
        code="PENDING_UNREADABLE",
    )


def same_json_value(left: Any, right: Any) -> bool:
    """Compare stored JSON. Integers, booleans, nulls and text are exact; floats use the contract tolerance."""
    if type(left) is bool or type(right) is bool:
        return type(left) is bool and type(right) is bool and left is right
    if left is None or right is None:
        return left is None and right is None
    if type(left) is str or type(right) is str:
        return type(left) is str and left == right
    if type(left) is int and type(right) is int:
        return left == right
    if type(left) is float and type(right) is float:
        return math.isclose(left, right, rel_tol=FLOAT_REL_TOL, abs_tol=FLOAT_ABS_TOL)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            same_json_value(item, other) for item, other in zip(left, right)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            same_json_value(left[key], right[key]) for key in left
        )
    return False


def _replace_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RemoteStoreError(
            f"待核验记录未能保存，未发送写请求：{exc.strerror or exc}",
            code="PENDING_UNREADABLE",
        ) from exc
