"""Supabase Data API storage. Daily methods map onto frozen RPC names.

The transport is stdlib urllib. Tests inject a fake ``call`` so this module
never opens a socket or reads ``~/.marketreview``.
"""

from __future__ import annotations

import json
import socket
import uuid
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .backend import SupabaseSettings, redact_secret
from .errors import InvalidFieldValueError, RemoteStoreError
from .repository import (
    _detail_patch_as_mapping,
    _normalize_detail_patch,
    _normalize_event,
    _reject_duplicate_detail_identities,
    _reject_duplicate_event_identities,
    _require_str,
)
from .schema import (
    ATOMIC_FIELD_NAMES,
    PRICE_LIMIT_EVENT_DETAIL_SCALAR_FIELD_ORDER,
    DayRead,
    DailyMarketReviewAtoms,
    PriceLimitEventDetailLike,
    PriceLimitEventDetailRecord,
    PriceLimitEventLike,
    PriceLimitEventRecord,
)
from .sqlite_schema import utc_now_iso
from .validation import normalize_trade_date, validate_atomic_field
from .write_gate import (
    assert_no_open_pending,
    close_pending,
    direction_absent,
    direction_present,
    exclusive_write,
    read_pending,
    same_json_value,
    save_open_pending,
)

SCHEMA_VERSION = 1
FORMAT_VERSION = 1
MAX_BODY_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
# Data API + Secret 路径实测：authenticator 会话 statement_timeout = 8s（见实测记录）。
DATA_API_STATEMENT_TIMEOUT_SECONDS = 8.0
_INT_REVIEW_FIELDS = frozenset({"advancing_count", "declining_count", "pullback_count"})

_EVENT_ORDER = ("trade_date", "market", "code", "direction")
_LIST_ORDER = ("trade_date", "market", "code", "direction", "position")


class RpcTransport:
    def call(self, function: str, request: Mapping[str, Any], *, write: bool) -> dict[str, Any]:
        raise NotImplementedError


class UrllibRpcTransport(RpcTransport):
    def __init__(
        self,
        settings: SupabaseSettings,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._settings = settings
        self._timeout = timeout
        parsed = urlparse(settings.url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise RemoteStoreError("Supabase URL 必须是不含凭证的 https 地址。", code="CONFIG_MISSING")
        self._host = parsed.netloc

    def call(self, function: str, request: Mapping[str, Any], *, write: bool) -> dict[str, Any]:
        url = f"{self._settings.url}/rest/v1/rpc/{function}"
        body = json.dumps({"p_request": request}, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_BODY_BYTES:
            raise RemoteStoreError("请求超过 8MB，已整次拒绝。", code="REQUEST_TOO_LARGE")
        http_request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "apikey": self._settings.secret_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        opener = urllib.request.build_opener(_RefuseRedirect(self._host, self._settings.secret_key))
        try:
            with opener.open(http_request, timeout=self._timeout) as response:
                payload = _read_limited(response, self._settings.secret_key, write=write)
                status = getattr(response, "status", 200)
        except RemoteStoreError:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise _transport_failure(exc, secret=self._settings.secret_key, write=write) from exc
        except urllib.error.HTTPError as exc:
            detail = _read_error_body(exc, self._settings.secret_key)
            raise _http_failure(exc.code, detail, write=write) from exc
        except urllib.error.URLError as exc:
            raise _transport_failure(exc, secret=self._settings.secret_key, write=write) from exc
        if status >= 400:
            raise _http_failure(status, payload.decode("utf-8", errors="replace"), write=write)
        return _decode_json_object(payload, secret=self._settings.secret_key, write=write)


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, host: str, secret: str) -> None:
        super().__init__()
        self._host = host
        self._secret = secret

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        target = urlparse(newurl)
        if target.netloc != self._host:
            raise RemoteStoreError(
                "云端响应要求把密钥转到其他主机，已拒绝。",
                code="REMOTE_REDIRECT",
            )
        raise RemoteStoreError(
            redact_secret(f"云端返回重定向 {code}，已拒绝继续发送密钥。", self._secret),
            code="REMOTE_REDIRECT",
        )


# 这些码表示服务端在提交前拒绝，或请求根本没有发出。
# 响应体校验失败和响应超限不算在内，它们不能证明数据库没有提交。
_DEFINITE_SERVER_REJECT = frozenset(
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
        "OPERATION_NOT_FOUND",
        "EMPTY_COMMIT",
        "LIST_NULL",
        "REMOTE_FORBIDDEN",
        "42501",
        "REQUEST_TOO_LARGE",
    }
)


class SupabaseRepository:
    def __init__(
        self,
        transport: RpcTransport,
        *,
        clock: Callable[[], str] = utc_now_iso,
        state_dir: Path | None = None,
        project_ref: str | None = None,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._state_dir = state_dir
        self._project_ref = project_ref

    def close(self) -> None:
        return None

    def __enter__(self) -> "SupabaseRepository":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def read_day(
        self,
        trade_date: str | date,
        previous_trade_date: str | date | None,
    ) -> DayRead:
        normalized_date = normalize_trade_date(trade_date)
        previous = (
            None if previous_trade_date is None else normalize_trade_date(previous_trade_date)
        )
        queried_previous = previous or normalized_date
        payload = self._read(
            "marketreview_get_day",
            {
                "schema_version": SCHEMA_VERSION,
                "trade_date": normalized_date,
                "previous_trade_date": queried_previous,
            },
        )
        _require_keys(
            payload,
            ("review", "events", "details", "sectors", "reasons", "previous_events"),
        )
        _require_counts(
            payload,
            {
                "reviews": 0 if payload.get("review") is None else 1,
                "events": len(payload["events"]),
                "details": len(payload["details"]),
                "sectors": len(payload["sectors"]),
                "reasons": len(payload["reasons"]),
                "previous_events": len(payload["previous_events"]),
            },
        )
        events = _events(payload["events"])
        previous_events = [] if previous is None else _events(payload["previous_events"])
        return DayRead(
            trade_date=normalized_date,
            review=_review(payload.get("review")),
            events=events,
            details=_details(
                payload["details"],
                payload["sectors"],
                payload["reasons"],
                {(item.trade_date, item.market, item.code, item.direction) for item in events},
            ),
            previous_events=previous_events,
        )

    def get_review(self, trade_date: str | date) -> DailyMarketReviewAtoms | None:
        reviews = self.list_reviews(trade_date, trade_date)
        if len(reviews) > 1:
            raise RemoteStoreError("同一交易日返回了多条复盘。", code="INCOMPLETE_RESPONSE")
        return reviews[0] if reviews else None

    def list_reviews(
        self,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
    ) -> list[DailyMarketReviewAtoms]:
        payload = self._read("marketreview_list_reviews", _range_request(start_date, end_date))
        _require_keys(payload, ("reviews",))
        reviews = payload["reviews"]
        _require_count(payload, "reviews", len(reviews))
        parsed = [_review(item) for item in reviews]
        if any(item is None for item in parsed):
            raise RemoteStoreError("复盘列表含空记录。", code="INCOMPLETE_RESPONSE")
        _assert_unique((item.trade_date,) for item in parsed if item is not None)
        return [item for item in parsed if item is not None]

    def list_trade_dates(
        self,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
    ) -> list[str]:
        payload = self._read("marketreview_list_trade_dates", _range_request(start_date, end_date))
        _require_keys(payload, ("trade_dates",))
        dates = payload["trade_dates"]
        if not isinstance(dates, list):
            raise RemoteStoreError("trade_dates 必须为数组。", code="INCOMPLETE_RESPONSE")
        _require_count(payload, "trade_dates", len(dates))
        normalized = [normalize_trade_date(item) for item in dates]
        _assert_sorted(normalized, lambda item: (item,))
        _assert_unique((item,) for item in normalized)
        return normalized

    def get_price_limit_events(self, trade_date: str | date) -> list[PriceLimitEventRecord]:
        return self.list_price_limit_events(trade_date, trade_date)

    def list_price_limit_events(
        self,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
    ) -> list[PriceLimitEventRecord]:
        payload = self._read("marketreview_list_events", _range_request(start_date, end_date))
        _require_keys(payload, ("events",))
        _require_count(payload, "events", len(payload["events"]))
        return _events(payload["events"])

    def get_price_limit_event_details(
        self,
        trade_date: str | date,
    ) -> list[PriceLimitEventDetailRecord]:
        return self.list_price_limit_event_details(trade_date, trade_date)

    def list_price_limit_event_details(
        self,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
    ) -> list[PriceLimitEventDetailRecord]:
        payload = self._read("marketreview_list_event_details", _range_request(start_date, end_date))
        _require_keys(payload, ("details", "sectors", "reasons", "events"))
        _require_counts(
            payload,
            {
                "details": len(payload["details"]),
                "sectors": len(payload["sectors"]),
                "reasons": len(payload["reasons"]),
                "events": len(payload["events"]),
            },
        )
        events = _events(payload["events"])
        return _details(
            payload["details"],
            payload["sectors"],
            payload["reasons"],
            {(item.trade_date, item.market, item.code, item.direction) for item in events},
        )

    def save_review(self, trade_date: str | date, fields: Mapping[str, Any] | None = None) -> None:
        normalized_date = normalize_trade_date(trade_date)
        payload = dict(fields or {})
        if not payload:
            return
        unknown = set(payload) - ATOMIC_FIELD_NAMES
        if unknown:
            raise InvalidFieldValueError(f"未知字段：{', '.join(sorted(unknown))}")
        normalized = {key: validate_atomic_field(key, value) for key, value in payload.items()}
        self._write(
            "marketreview_save_review",
            {
                "schema_version": SCHEMA_VERSION,
                "trade_date": normalized_date,
                "batch_time": self._clock(),
                "fields": normalized,
            },
        )

    def save_price_limit_events(
        self,
        trade_date: str | date,
        events: Sequence[PriceLimitEventLike],
    ) -> None:
        normalized_date = normalize_trade_date(trade_date)
        if not events:
            return
        records = [_normalize_event(normalized_date, event) for event in events]
        _reject_duplicate_event_identities(records)
        self._write(
            "marketreview_save_events",
            {
                "schema_version": SCHEMA_VERSION,
                "trade_date": normalized_date,
                "batch_time": self._clock(),
                "events": [_event_body(record) for record in records],
            },
        )

    def save_price_limit_event_details(
        self,
        trade_date: str | date,
        details: Sequence[PriceLimitEventDetailLike],
    ) -> None:
        normalized_date = normalize_trade_date(trade_date)
        if not details:
            return
        patches = [_normalize_detail_patch(detail) for detail in details]
        _reject_duplicate_detail_identities(patches)
        self._write(
            "marketreview_save_event_details",
            {
                "schema_version": SCHEMA_VERSION,
                "trade_date": normalized_date,
                "batch_time": self._clock(),
                "details": [_detail_patch_as_mapping(patch) for patch in patches],
            },
        )

    def delete_review(self, trade_date: str | date) -> None:
        self._write(
            "marketreview_delete_review",
            {
                "schema_version": SCHEMA_VERSION,
                "trade_date": normalize_trade_date(trade_date),
                "batch_time": self._clock(),
            },
        )

    def delete_price_limit_events(self, trade_date: str | date) -> None:
        self._write(
            "marketreview_delete_price_limit_events",
            {
                "schema_version": SCHEMA_VERSION,
                "trade_date": normalize_trade_date(trade_date),
                "batch_time": self._clock(),
            },
        )

    def delete_price_limit_event(
        self,
        trade_date: str | date,
        market: str,
        code: str,
        direction: str,
    ) -> None:
        self._write(
            "marketreview_delete_event",
            {
                "schema_version": SCHEMA_VERSION,
                "trade_date": normalize_trade_date(trade_date),
                "batch_time": self._clock(),
                "market": _require_str("market", market),
                "code": _require_str("code", code),
                "direction": _require_str("direction", direction),
            },
        )

    def replace_price_limit_event_direction(
        self,
        trade_date: str | date,
        market: str,
        code: str,
        old_direction: str,
        event: PriceLimitEventLike,
    ) -> None:
        normalized_date = normalize_trade_date(trade_date)
        market_value = _require_str("market", market)
        code_value = _require_str("code", code)
        old_direction_value = _require_str("old_direction", old_direction)
        record = _normalize_event(normalized_date, event)
        if record.market != market_value or record.code != code_value:
            raise InvalidFieldValueError(
                "替换事件的 market/code 必须与删除目标一致："
                f"{market_value}.{code_value}"
            )
        if record.direction == old_direction_value:
            raise InvalidFieldValueError(
                "方向未变化时请使用 save_price_limit_events，不要调用方向替换"
            )
        batch_time = self._clock()
        request = {
            "schema_version": SCHEMA_VERSION,
            "trade_date": normalized_date,
            "batch_time": batch_time,
            "market": market_value,
            "code": code_value,
            "old_direction": old_direction_value,
            "event": _event_body(record),
        }
        preimage_request = {
            "schema_version": SCHEMA_VERSION,
            "trade_date": normalized_date,
            "market": market_value,
            "code": code_value,
            "old_direction": old_direction_value,
            "new_direction": record.direction,
        }
        state_dir = self._require_state_dir()
        with exclusive_write(state_dir):
            assert_no_open_pending(state_dir)
            preimage = self._read("marketreview_replace_direction_preimage", preimage_request)
            _require_preimage_shape(
                preimage,
                trade_date=normalized_date,
                market=market_value,
                code=code_value,
                old_direction=old_direction_value,
                new_direction=record.direction,
            )
            save_open_pending(
                state_dir,
                {
                    "format_version": FORMAT_VERSION,
                    "operation_id": uuid.uuid4().hex,
                    "kind": "replace-direction",
                    "project_ref": self._project_ref,
                    "schema_version": SCHEMA_VERSION,
                    "started_at": self._clock(),
                    "payload": request,
                    "preimage": preimage,
                },
            )
            try:
                self._send_write("marketreview_replace_direction", request, probe=False)
            except RemoteStoreError as exc:
                if exc.code not in _DEFINITE_SERVER_REJECT:
                    raise
                try:
                    close_pending(state_dir, result="rejected")
                except RemoteStoreError as close_exc:
                    raise RemoteStoreError(
                        "云端写入已被拒绝，但本机待核验记录未能关闭，后续写入已停止。",
                        code="PENDING_UNREADABLE",
                    ) from close_exc
                raise
            try:
                close_pending(state_dir, result="confirmed")
            except RemoteStoreError as exc:
                raise RemoteStoreError(
                    "云端写入已返回成功，但本机待核验记录未能关闭，后续写入已停止。",
                    code="PENDING_UNREADABLE",
                ) from exc

    def verify_pending_replace(self) -> None:
        """Re-read an open direction replacement and recover under the write lock.

        Matching preimage after the Data API statement-timeout window means the
        original request did not commit; close, reopen with a fresh started_at,
        then resend. Within the window the gate stays open and nothing is sent.
        """
        state_dir = self._require_state_dir()
        with exclusive_write(state_dir):
            record = read_pending(state_dir)
            if record is None or record.get("status") != "open":
                return
            if record.get("kind") != "replace-direction":
                raise RemoteStoreError(
                    "待核验记录无法读取或已损坏，未发送写请求。",
                    code="PENDING_UNREADABLE",
                )
            # Shared state dir must not close another project's open replace record.
            if record.get("project_ref") != self._project_ref:
                raise RemoteStoreError(
                    "待核验记录属于其他项目，未关闭、未按当前项目核验。",
                    code="IDENTITY_MISMATCH",
                )
            payload = record.get("payload")
            preimage = record.get("preimage")
            event = payload.get("event") if isinstance(payload, dict) else None
            if (
                not isinstance(payload, dict)
                or not isinstance(event, dict)
                or "direction" not in event
            ):
                raise RemoteStoreError(
                    "待核验记录无法读取或已损坏，未发送写请求。",
                    code="PENDING_UNREADABLE",
                )
            if not isinstance(preimage, dict) or not preimage:
                raise RemoteStoreError(
                    "方向替换结果未知：缺少保存的前像。待核验记录仍打开。",
                    code="REMOTE_RESULT_UNKNOWN",
                )
            try:
                current = self._read(
                    "marketreview_replace_direction_preimage",
                    {
                        "schema_version": SCHEMA_VERSION,
                        "trade_date": payload["trade_date"],
                        "market": payload["market"],
                        "code": payload["code"],
                        "old_direction": payload["old_direction"],
                        "new_direction": event["direction"],
                    },
                )
            except RemoteStoreError as exc:
                raise RemoteStoreError(
                    "方向替换核验读取失败，不能判断原请求是否已结束。待核验记录仍打开。",
                    code="REMOTE_RESULT_UNKNOWN",
                ) from exc
            if not _preimage_states_complete(current):
                raise RemoteStoreError(
                    "方向替换结果未知：读取没有返回两边的完整现状。待核验记录仍打开。",
                    code="REMOTE_RESULT_UNKNOWN",
                )
            if same_json_value(preimage, current):
                remaining = _in_flight_remaining_seconds(
                    record.get("started_at"),
                    self._clock(),
                )
                if remaining is None:
                    raise RemoteStoreError(
                        "旧前像仍一致，但待核验记录的开始时间无法解析，"
                        "不能确认原请求已回滚，也不能重发。待核验记录仍打开。",
                        code="REMOTE_RESULT_UNKNOWN",
                    )
                if remaining > 0:
                    raise RemoteStoreError(
                        "旧前像仍一致，但距请求开始未满服务端语句超时上限"
                        f"（{DATA_API_STATEMENT_TIMEOUT_SECONDS:g} 秒），"
                        f"不能排除原请求仍在途（约剩余 {remaining:.1f} 秒），"
                        "不能重发。待核验记录仍打开。",
                        code="REMOTE_RESULT_UNKNOWN",
                    )
                self._resend_replace_after_not_executed(state_dir, payload)
                return
            if _replacement_succeeded(preimage, current, payload):
                try:
                    close_pending(state_dir, result="confirmed")
                except RemoteStoreError as exc:
                    raise RemoteStoreError(
                        "方向替换已在云端成功，但本机待核验记录未能关闭，后续写入已停止。",
                        code="PENDING_UNREADABLE",
                    ) from exc
                return
            raise RemoteStoreError(
                "方向替换结果未知：现状与保存的前像不一致。待核验记录仍打开。",
                code="REMOTE_RESULT_UNKNOWN",
            )

    def _resend_replace_after_not_executed(
        self,
        state_dir: Path,
        payload: Mapping[str, Any],
    ) -> None:
        """Close as not_executed, reopen with a new started_at, then resend.

        Caller must already hold write.lock. Failure of either durable step
        must not send the write RPC.
        """
        try:
            close_pending(state_dir, result="not_executed")
        except RemoteStoreError as exc:
            raise RemoteStoreError(
                "已确认方向替换未执行，但本机待核验记录未能关闭，未重发。",
                code="PENDING_UNREADABLE",
            ) from exc
        closed = read_pending(state_dir)
        if closed is None:
            raise RemoteStoreError(
                "已确认方向替换未执行，但关闭后的记录丢失，未重发。",
                code="PENDING_UNREADABLE",
            )
        reopen = {
            "format_version": closed.get("format_version", FORMAT_VERSION),
            "operation_id": closed["operation_id"],
            "kind": "replace-direction",
            "project_ref": closed["project_ref"],
            "schema_version": closed["schema_version"],
            "started_at": self._clock(),
            "payload": closed["payload"],
            "preimage": closed["preimage"],
            "verification_history": list(closed.get("verification_history") or []),
        }
        try:
            save_open_pending(state_dir, reopen)
        except RemoteStoreError as exc:
            raise RemoteStoreError(
                "已确认方向替换未执行并已关闭记录，但重新持久化未关闭状态失败，未重发。",
                code="PENDING_UNREADABLE",
            ) from exc
        try:
            self._send_write("marketreview_replace_direction", payload, probe=False)
        except RemoteStoreError as exc:
            if exc.code not in _DEFINITE_SERVER_REJECT:
                raise
            try:
                close_pending(state_dir, result="rejected")
            except RemoteStoreError as close_exc:
                raise RemoteStoreError(
                    "云端写入已被拒绝，但本机待核验记录未能关闭，后续写入已停止。",
                    code="PENDING_UNREADABLE",
                ) from close_exc
            raise
        try:
            close_pending(state_dir, result="confirmed")
        except RemoteStoreError as exc:
            raise RemoteStoreError(
                "云端写入已返回成功，但本机待核验记录未能关闭，后续写入已停止。",
                code="PENDING_UNREADABLE",
            ) from exc

    def _require_state_dir(self) -> Path:
        if self._state_dir is None or not self._project_ref:
            raise RemoteStoreError(
                "没有本机写入状态目录，未发送写请求。",
                code="PENDING_UNREADABLE",
            )
        return self._state_dir

    def _read(self, function: str, request: Mapping[str, Any]) -> dict[str, Any]:
        payload = self._transport.call(function, request, write=False)
        _require_envelope(payload)
        return payload

    def _write(
        self,
        function: str,
        request: Mapping[str, Any],
        *,
        probe: bool = True,
    ) -> None:
        state_dir = self._require_state_dir()
        with exclusive_write(state_dir):
            assert_no_open_pending(state_dir)
            self._send_write(function, request, probe=probe)

    def _send_write(
        self,
        function: str,
        request: Mapping[str, Any],
        *,
        probe: bool,
    ) -> None:
        if probe:
            self._read("marketreview_probe", {"schema_version": SCHEMA_VERSION})
        payload = self._transport.call(function, request, write=True)
        try:
            _require_envelope(payload)
            if not isinstance(payload.get("noop"), bool) or type(payload.get("revision")) is not int:
                raise RemoteStoreError("写入响应缺少 noop 或 revision。", code="INCOMPLETE_RESPONSE")
        except RemoteStoreError as exc:
            raise RemoteStoreError(
                f"云端写入结果未知，不能视为已回滚或已成功：{exc}",
                code="REMOTE_RESULT_UNKNOWN",
            ) from exc


def _parse_iso_utc(value: Any) -> datetime | None:
    if type(value) is not str or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _in_flight_remaining_seconds(started_at: Any, now: Any) -> float | None:
    """Seconds left in the Data API statement-timeout window; None if unusable."""
    start = _parse_iso_utc(started_at)
    current = _parse_iso_utc(now)
    if start is None or current is None:
        return None
    deadline = start + timedelta(seconds=DATA_API_STATEMENT_TIMEOUT_SECONDS)
    return (deadline - current).total_seconds()


def _require_preimage_shape(
    preimage: Mapping[str, Any],
    *,
    trade_date: str,
    market: str,
    code: str,
    old_direction: str,
    new_direction: str,
) -> None:
    if type(preimage.get("revision")) is not int:
        raise RemoteStoreError("方向替换前像缺少版本。", code="INCOMPLETE_RESPONSE")
    for name, expected in (
        ("trade_date", trade_date),
        ("market", market),
        ("code", code),
        ("old_direction", old_direction),
        ("new_direction", new_direction),
    ):
        if preimage.get(name) != expected:
            raise RemoteStoreError(
                f"方向替换前像的 {name} 与写入目标不一致。",
                code="INCOMPLETE_RESPONSE",
            )
    old_state = preimage.get("old")
    new_state = preimage.get("new")
    if not direction_present(old_state):
        raise RemoteStoreError("方向替换前像缺少旧事件、明细、时间或列表。", code="INCOMPLETE_RESPONSE")
    if not direction_absent(new_state):
        raise RemoteStoreError("方向替换前像未确认目标方向不存在。", code="INCOMPLETE_RESPONSE")
    old_event = old_state["event"]
    if (
        old_event.get("trade_date") != trade_date
        or old_event.get("market") != market
        or old_event.get("code") != code
        or old_event.get("direction") != old_direction
    ):
        raise RemoteStoreError("方向替换前像的旧事件身份与写入目标不一致。", code="INCOMPLETE_RESPONSE")
    if preimage.get("new_direction") == preimage.get("old_direction"):
        raise RemoteStoreError("方向替换前像的新旧方向相同。", code="INCOMPLETE_RESPONSE")


def _preimage_states_complete(preimage: Mapping[str, Any]) -> bool:
    old_state = preimage.get("old")
    new_state = preimage.get("new")
    old_ok = direction_present(old_state) or direction_absent(old_state)
    new_ok = direction_present(new_state) or direction_absent(new_state)
    return old_ok and new_ok and type(preimage.get("revision")) is int


def _replacement_succeeded(
    saved: Mapping[str, Any],
    current: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> bool:
    old_now = current.get("old")
    new_now = current.get("new")
    saved_old = saved.get("old")
    event = payload.get("event")
    batch_time = payload.get("batch_time")
    if not direction_absent(old_now) or not direction_present(new_now):
        return False
    if not isinstance(saved_old, dict) or not isinstance(event, dict) or type(batch_time) is not str:
        return False
    new_event = new_now["event"]
    for name in ("market", "code", "name", "direction", "closed_at_limit", "limit_rate_bp", "streak_height"):
        if not same_json_value(new_event.get(name), event.get(name)):
            return False
    if new_event.get("trade_date") != payload.get("trade_date"):
        return False
    if new_event.get("created_at") != batch_time or new_event.get("updated_at") != batch_time:
        return False
    if new_now.get("detail_exists") is not saved_old.get("detail_exists"):
        return False
    if saved_old.get("detail_exists") is True:
        detail = new_now.get("detail")
        saved_detail = saved_old.get("detail")
        if not isinstance(detail, dict) or not isinstance(saved_detail, dict):
            return False
        for name in PRICE_LIMIT_EVENT_DETAIL_SCALAR_FIELD_ORDER:
            if not same_json_value(detail.get(name), saved_detail.get(name)):
                return False
        if detail.get("created_at") != batch_time or detail.get("updated_at") != batch_time:
            return False
        if detail.get("direction") != event.get("direction"):
            return False
    elif new_now.get("detail") is not None:
        return False
    if not same_json_value(new_now.get("sectors"), saved_old.get("sectors")):
        return False
    if event.get("direction") == "up":
        return same_json_value(new_now.get("reasons"), saved_old.get("reasons"))
    return new_now.get("reasons") == []


def _event_body(record: PriceLimitEventRecord) -> dict[str, Any]:
    return {
        "market": record.market,
        "code": record.code,
        "name": record.name,
        "direction": record.direction,
        "closed_at_limit": record.closed_at_limit,
        "limit_rate_bp": record.limit_rate_bp,
        "streak_height": record.streak_height,
    }


def _range_request(
    start_date: str | date | None,
    end_date: str | date | None,
) -> dict[str, Any]:
    request: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    if start_date is not None:
        request["start_date"] = normalize_trade_date(start_date)
    if end_date is not None:
        request["end_date"] = normalize_trade_date(end_date)
    return request


def _require_keys(payload: Mapping[str, Any], names: Sequence[str]) -> None:
    missing = [name for name in names if name not in payload]
    if missing:
        raise RemoteStoreError(
            f"响应缺少 {', '.join(missing)}。",
            code="INCOMPLETE_RESPONSE",
        )


def _require_envelope(payload: Mapping[str, Any]) -> None:
    if payload.get("format_version") != FORMAT_VERSION or payload.get("schema_version") != SCHEMA_VERSION:
        raise RemoteStoreError("响应版本与冻结合同不一致。", code="SCHEMA_VERSION_MISMATCH")
    if payload.get("complete") is not True:
        raise RemoteStoreError("响应未声明完整结果。", code="INCOMPLETE_RESPONSE")


def _require_count(payload: Mapping[str, Any], name: str, actual: int) -> None:
    counts = payload.get("counts")
    if not isinstance(counts, dict) or counts.get(name) != actual:
        raise RemoteStoreError(f"响应计数与 {name} 不符。", code="INCOMPLETE_RESPONSE")


def _require_counts(payload: Mapping[str, Any], expected: Mapping[str, int]) -> None:
    for name, actual in expected.items():
        _require_count(payload, name, actual)


def _review(value: Any) -> DailyMarketReviewAtoms | None:
    if value is None:
        return None
    if not isinstance(value, dict) or "trade_date" not in value:
        raise RemoteStoreError("复盘记录不完整。", code="INCOMPLETE_RESPONSE")
    atoms = {"trade_date": normalize_trade_date(value["trade_date"])}
    for name in ATOMIC_FIELD_NAMES:
        if name not in value:
            raise RemoteStoreError(f"复盘记录缺少 {name}。", code="INCOMPLETE_RESPONSE")
        if name in _INT_REVIEW_FIELDS:
            atoms[name] = _optional_int(name, value[name])
        else:
            atoms[name] = _optional_number(name, value[name])
    return DailyMarketReviewAtoms(**atoms)


def _events(values: Any) -> list[PriceLimitEventRecord]:
    if not isinstance(values, list):
        raise RemoteStoreError("events 必须为数组。", code="INCOMPLETE_RESPONSE")
    records = [_event(item) for item in values]
    _assert_sorted(records, lambda item: (item.trade_date, item.market, item.code, item.direction))
    _assert_unique((item.trade_date, item.market, item.code, item.direction) for item in records)
    return records


def _event(value: Any) -> PriceLimitEventRecord:
    if not isinstance(value, dict):
        raise RemoteStoreError("事件记录不完整。", code="INCOMPLETE_RESPONSE")
    for name in ("trade_date", "market", "code", "name", "direction", "closed_at_limit", "limit_rate_bp", "streak_height"):
        if name not in value:
            raise RemoteStoreError(f"事件记录缺少 {name}。", code="INCOMPLETE_RESPONSE")
    if type(value["closed_at_limit"]) is not bool:
        raise RemoteStoreError("closed_at_limit 必须为布尔值。", code="INCOMPLETE_RESPONSE")
    if type(value["limit_rate_bp"]) is not int or type(value["streak_height"]) is not int:
        raise RemoteStoreError("事件计数字段必须为整数。", code="INCOMPLETE_RESPONSE")
    return PriceLimitEventRecord(
        trade_date=normalize_trade_date(value["trade_date"]),
        market=value["market"],
        code=value["code"],
        name=value["name"],
        direction=value["direction"],
        closed_at_limit=value["closed_at_limit"],
        limit_rate_bp=value["limit_rate_bp"],
        streak_height=value["streak_height"],
    )


def _details(
    details: Any,
    sectors: Any,
    reasons: Any,
    parent_events: set[tuple[str, str, str, str]],
) -> list[PriceLimitEventDetailRecord]:
    if not isinstance(details, list) or not isinstance(sectors, list) or not isinstance(reasons, list):
        raise RemoteStoreError("明细响应必须包含三个数组。", code="INCOMPLETE_RESPONSE")
    _assert_sorted(details, lambda item: _identity(item, _EVENT_ORDER))
    _assert_sorted(sectors, lambda item: _identity(item, _LIST_ORDER))
    _assert_sorted(reasons, lambda item: _identity(item, _LIST_ORDER))
    records: dict[tuple[str, str, str, str], PriceLimitEventDetailRecord] = {}
    for item in details:
        key = _identity(item, _EVENT_ORDER)
        if key not in parent_events:
            raise RemoteStoreError("明细没有父事件。", code="INCOMPLETE_RESPONSE")
        if key in records:
            raise RemoteStoreError("明细身份重复。", code="INCOMPLETE_RESPONSE")
        records[key] = _detail(item)
    _consume_ordered_list(sectors, "sectors", records, parent_events)
    _consume_ordered_list(reasons, "limit_up_reasons", records, parent_events)
    return [records[key] for key in sorted(records)]


def _consume_ordered_list(
    rows: Sequence[Any],
    field_name: str,
    records: dict[tuple[str, str, str, str], PriceLimitEventDetailRecord],
    parent_events: set[tuple[str, str, str, str]],
) -> None:
    positions: dict[tuple[str, str, str, str], list[int]] = {}
    seen_values: set[tuple[Any, ...]] = set()
    for item in rows:
        key = _identity(item, _EVENT_ORDER)
        if key not in parent_events:
            raise RemoteStoreError("列表记录没有父事件。", code="INCOMPLETE_RESPONSE")
        position = item.get("position")
        value = item.get("value")
        if type(position) is not int or position < 0:
            raise RemoteStoreError("列表位置必须从 0 连续排列。", code="INCOMPLETE_RESPONSE")
        if type(value) is not str:
            raise RemoteStoreError("列表值必须为字符串。", code="INCOMPLETE_RESPONSE")
        business_key = key + (value,)
        if business_key in seen_values:
            raise RemoteStoreError("列表业务键重复。", code="INCOMPLETE_RESPONSE")
        seen_values.add(business_key)
        positions.setdefault(key, []).append(position)
        record = records.get(key) or _empty_detail(*key)
        records[key] = _append_list(record, field_name, value)
    for key, found in positions.items():
        if found != list(range(len(found))):
            raise RemoteStoreError("列表位置必须从 0 连续排列。", code="INCOMPLETE_RESPONSE")


def _detail(value: Mapping[str, Any]) -> PriceLimitEventDetailRecord:
    payload: dict[str, Any] = {
        "trade_date": normalize_trade_date(value["trade_date"]),
        "market": value["market"],
        "code": value["code"],
        "direction": value["direction"],
        "sectors": [],
        "limit_up_reasons": [],
    }
    for name in PRICE_LIMIT_EVENT_DETAIL_SCALAR_FIELD_ORDER:
        if name not in value:
            raise RemoteStoreError(f"明细记录缺少 {name}。", code="INCOMPLETE_RESPONSE")
        if name == "is_leader":
            leader = value[name]
            if leader is not None and type(leader) is not bool:
                raise RemoteStoreError("is_leader 必须为布尔值或 null。", code="INCOMPLETE_RESPONSE")
            payload[name] = leader
        elif name == "note":
            note = value[name]
            if note is not None and type(note) is not str:
                raise RemoteStoreError("note 必须为字符串或 null。", code="INCOMPLETE_RESPONSE")
            payload[name] = note
        else:
            payload[name] = _optional_number(name, value[name])
    return PriceLimitEventDetailRecord(**payload)


def _empty_detail(
    trade_date: str,
    market: str,
    code: str,
    direction: str,
) -> PriceLimitEventDetailRecord:
    return PriceLimitEventDetailRecord(
        trade_date=trade_date,
        market=market,
        code=code,
        direction=direction,
        previous_turnover_amount=None,
        auction_amount=None,
        previous_close=None,
        open_price=None,
        turnover_amount=None,
        turnover_rate=None,
        is_leader=None,
        note=None,
        sectors=[],
        limit_up_reasons=[],
    )


def _append_list(
    record: PriceLimitEventDetailRecord,
    field_name: str,
    value: Any,
) -> PriceLimitEventDetailRecord:
    if type(value) is not str:
        raise RemoteStoreError("列表值必须为字符串。", code="INCOMPLETE_RESPONSE")
    from dataclasses import replace

    current = list(getattr(record, field_name))
    current.append(value)
    return replace(record, **{field_name: current})


def _identity(value: Any, fields: Sequence[str]) -> tuple[Any, ...]:
    if not isinstance(value, dict):
        raise RemoteStoreError("记录必须为对象。", code="INCOMPLETE_RESPONSE")
    try:
        return tuple(value[field] for field in fields)
    except KeyError as exc:
        raise RemoteStoreError(f"记录缺少 {exc.args[0]}。", code="INCOMPLETE_RESPONSE") from exc


def _optional_int(name: str, value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise RemoteStoreError(f"{name} 必须为整数或 null。", code="INCOMPLETE_RESPONSE")
    return value


def _optional_number(name: str, value: Any) -> float | None:
    if value is None:
        return None
    # PG jsonb encodes whole double precision as JSON integers; SQLite keeps float.
    if type(value) not in {int, float}:
        raise RemoteStoreError(f"{name} 必须为数字或 null。", code="INCOMPLETE_RESPONSE")
    return float(value)


def _assert_sorted(items: Sequence[Any], key) -> None:  # noqa: ANN001
    keys = [key(item) for item in items]
    if keys != sorted(keys):
        raise RemoteStoreError("响应顺序与合同不一致。", code="INCOMPLETE_RESPONSE")


def _assert_unique(keys: Any) -> None:
    seen: set[tuple[Any, ...]] = set()
    for key in keys:
        if key in seen:
            raise RemoteStoreError("响应含重复业务键。", code="INCOMPLETE_RESPONSE")
        seen.add(key)


def _decode_json_object(payload: bytes, *, secret: str, write: bool) -> dict[str, Any]:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _incomplete(write, "响应不是完整 JSON。") from exc
    if not isinstance(decoded, dict):
        raise _incomplete(write, "响应必须是 JSON 对象。")
    return decoded


def _read_limited(response: Any, secret: str, *, write: bool = False) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        block = response.read(65536)
        if not block:
            break
        total += len(block)
        if total > MAX_BODY_BYTES:
            if write:
                raise RemoteStoreError(
                    "云端写入结果未知，不能视为已回滚或已成功：响应超过 8MB。",
                    code="REMOTE_RESULT_UNKNOWN",
                )
            raise RemoteStoreError("响应超过 8MB，已整次拒绝。", code="REQUEST_TOO_LARGE")
        chunks.append(block)
    return b"".join(chunks)


def _read_error_body(exc: urllib.error.HTTPError, secret: str) -> str:
    try:
        raw = exc.read(MAX_BODY_BYTES + 1)
    except Exception:
        return ""
    if len(raw) > MAX_BODY_BYTES:
        return "响应超过 8MB"
    return redact_secret(raw.decode("utf-8", errors="replace"), secret)


def _http_failure(status: int, detail: str, *, write: bool) -> RemoteStoreError:
    code, message = _application_error(detail)
    if status in {401, 403} or code == "42501":
        return RemoteStoreError(
            "云端拒绝了当前密钥，没有读写业务数据。",
            code="REMOTE_FORBIDDEN",
        )
    if code:
        return RemoteStoreError(message or "云端拒绝了这次请求。", code=code)
    if write and status >= 500:
        return RemoteStoreError(
            f"云端写入结果未知（HTTP {status}），不能视为已回滚或已成功。",
            code="REMOTE_RESULT_UNKNOWN",
        )
    return RemoteStoreError(f"云端请求失败（HTTP {status}）。", code="REMOTE_UNAVAILABLE")


def _application_error(detail: str) -> tuple[str | None, str]:
    try:
        payload = json.loads(detail) if detail else None
    except json.JSONDecodeError:
        return None, ""
    if not isinstance(payload, dict):
        return None, ""
    hint = payload.get("hint")
    message = payload.get("message")
    sqlstate = payload.get("code")
    text = message if isinstance(message, str) else ""
    if sqlstate == "42501":
        return "42501", text
    if isinstance(hint, str) and hint:
        return hint, text or hint
    if text.startswith("[") and "]" in text:
        return text[1 : text.index("]")], text
    return None, text


def _transport_failure(exc: Exception, *, secret: str, write: bool) -> RemoteStoreError:
    detail = redact_secret(str(exc.reason if isinstance(exc, urllib.error.URLError) else exc), secret)
    if write:
        return RemoteStoreError(
            f"云端写入结果未知，不能视为已回滚或已成功：{detail}",
            code="REMOTE_RESULT_UNKNOWN",
        )
    return RemoteStoreError(f"云端暂时不可达：{detail}", code="REMOTE_UNAVAILABLE")


def _incomplete(write: bool, detail: str) -> RemoteStoreError:
    if write:
        return RemoteStoreError(
            f"云端写入结果未知，不能视为已回滚或已成功：{detail}",
            code="REMOTE_RESULT_UNKNOWN",
        )
    return RemoteStoreError(detail, code="INCOMPLETE_RESPONSE")
