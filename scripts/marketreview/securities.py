"""Minimal securities lookup for market and code validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SECURITIES_PATH = SKILL_ROOT / "assets" / "securities_master.json"


@dataclass(frozen=True)
class SecurityRecord:
    code: str
    market: str
    name: str
    security_type: str


class SecuritiesLookup:
    def __init__(self, master_path: Path | str | None = None) -> None:
        path = Path(master_path) if master_path else DEFAULT_SECURITIES_PATH
        rows = json.loads(path.read_text(encoding="utf-8"))
        self._by_key: dict[tuple[str, str], SecurityRecord] = {}
        for row in rows:
            market = str(row["market"]).lower()
            code = str(row["code"])
            record = SecurityRecord(
                code=code,
                market=market,
                name=str(row.get("name", "")),
                security_type=str(row.get("type", "")),
            )
            self._by_key[(market, code)] = record

    def lookup(self, market: str, code: str) -> SecurityRecord | None:
        return self._by_key.get((market.lower(), code))

    def validate(self, market: str, code: str) -> SecurityRecord:
        record = self.lookup(market, code)
        if record is None:
            raise ValueError(f"unknown security: {market}.{code}")
        return record
