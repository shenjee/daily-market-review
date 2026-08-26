"""Minimal Tencent Finance index daily kline access."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta

INDICES: dict[str, tuple[str, str]] = {
    "sh000001": ("000001", "sh"),
    "sz399001": ("399001", "sz"),
    "sz399006": ("399006", "sz"),
}

INDEX_FIELD_MAP: dict[str, tuple[str, str]] = {
    "sh000001": ("sh_index_close", "sh_index_prev_close"),
    "sz399001": ("sz_index_close", "sz_index_prev_close"),
    "sz399006": ("cy_index_close", "cy_index_prev_close"),
}


@dataclass(frozen=True)
class IndexDailyQuote:
    symbol: str
    trade_date: str
    close: float
    prev_close: float


class TencentIndexClient:
    TIMEOUT = 10
    MAX_RETRIES = 3

    @classmethod
    def fetch_index_daily(
        cls,
        symbol: str,
        trade_date: str | date,
    ) -> IndexDailyQuote | None:
        if symbol not in INDICES:
            raise ValueError(f"unsupported index symbol: {symbol}")
        code, market = INDICES[symbol]
        day = trade_date.isoformat() if isinstance(trade_date, date) else trade_date
        start = (date.fromisoformat(day) - timedelta(days=10)).isoformat()
        url = (
            "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
            f"?param={market}{code},day,{start},{day},20,"
        )
        payload = cls._fetch_json(url)
        if not isinstance(payload, dict) or payload.get("code") != 0:
            return None
        data = payload.get("data", {})
        if not isinstance(data, dict):
            return None
        series = data.get(f"{market}{code}", {})
        if not isinstance(series, dict):
            return None
        klines = series.get("day", [])
        if not isinstance(klines, list) or not klines:
            return None
        parsed = []
        for item in klines:
            try:
                parsed.append(
                    {
                        "date": item[0],
                        "close": round(float(item[2]), 2),
                    }
                )
            except (TypeError, ValueError, IndexError):
                continue
        if not parsed:
            return None
        by_date = {row["date"]: row["close"] for row in parsed}
        if day not in by_date:
            return None
        ordered_dates = [row["date"] for row in parsed]
        idx = ordered_dates.index(day)
        if idx == 0:
            return None
        return IndexDailyQuote(
            symbol=symbol,
            trade_date=day,
            close=by_date[day],
            prev_close=parsed[idx - 1]["close"],
        )

    @classmethod
    def fetch_index_atoms(cls, trade_date: str | date) -> dict[str, float]:
        atoms: dict[str, float] = {}
        for symbol, (close_field, prev_field) in INDEX_FIELD_MAP.items():
            quote = cls.fetch_index_daily(symbol, trade_date)
            if quote is None:
                continue
            atoms[close_field] = quote.close
            atoms[prev_field] = quote.prev_close
        return atoms

    @classmethod
    def _fetch_json(cls, url: str) -> object:
        last_error: Exception | None = None
        for _ in range(cls.MAX_RETRIES):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "daily-market-review/0.1"})
                with urllib.request.urlopen(request, timeout=cls.TIMEOUT) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("tencent request failed")
