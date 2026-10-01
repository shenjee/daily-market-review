"""Structured errors for market review persistence."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, field


@dataclass(frozen=True)
class MarketReviewError(Exception):
    code: str
    message: str
    problem_codes: tuple[str, ...] = field(default_factory=tuple)

    def __str__(self) -> str:
        return self.message


class InvalidFieldValueError(MarketReviewError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            code="INVALID_FIELD_VALUE",
            message=f"字段值不合法，本次未写入任何数据：{detail}",
        )


class DatabaseUnavailableError(MarketReviewError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            code="DB_UNAVAILABLE",
            message=f"数据库不可用（状态未知）：{detail}",
        )


class BackendSelectionError(MarketReviewError):
    def __init__(self, detail: str, *, code: str = "INVALID_BACKEND") -> None:
        super().__init__(code=code, message=detail)


def _allow_exception_state(self: MarketReviewError, name: str, value: object) -> None:
    if name in {"__traceback__", "__context__", "__cause__", "__suppress_context__", "__notes__"}:
        object.__setattr__(self, name, value)
        return
    raise FrozenInstanceError(f"cannot assign to field {name!r}")


MarketReviewError.__setattr__ = _allow_exception_state  # type: ignore[method-assign]


class RemoteStoreError(MarketReviewError):
    def __init__(self, detail: str, *, code: str = "REMOTE_UNAVAILABLE") -> None:
        super().__init__(code=code, message=detail)
