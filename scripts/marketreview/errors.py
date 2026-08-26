"""Structured errors for market review persistence."""

from __future__ import annotations

from dataclasses import dataclass, field


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
