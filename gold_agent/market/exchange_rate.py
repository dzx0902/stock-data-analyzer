from __future__ import annotations

import threading
import time
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from gold_agent.config import (
    ALLTICK_API_KEY,
    ALLTICK_HTTP_BASE,
    ALLTICK_USD_CNY_SYMBOL,
    USD_CNY_CACHE_SECONDS,
    USD_CNY_FALLBACK_RATE,
    USD_CNY_PROVIDER_URL,
)
from gold_agent.infra.decimal_utils import decimal_text, to_decimal
from gold_agent.infra.http import http_get


@dataclass(frozen=True)
class ExchangeRateQuote:
    pair: str
    rate: Decimal
    source: str
    timestamp: str
    is_realtime: bool
    is_fallback: bool = False
    raw_payload: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "rate": decimal_text(self.rate),
            "source": self.source,
            "timestamp": self.timestamp,
            "is_realtime": self.is_realtime,
            "is_fallback": self.is_fallback,
            "raw_payload": self.raw_payload,
        }


_cache_lock = threading.Lock()
_cached_quote: ExchangeRateQuote | None = None
_cached_at = 0.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _valid_rate(value: Any) -> Decimal | None:
    rate = to_decimal(value)
    if rate is None or rate < Decimal("4") or rate > Decimal("12"):
        return None
    return rate


def _configured_provider() -> ExchangeRateQuote | None:
    if not USD_CNY_PROVIDER_URL:
        return None
    response = http_get(USD_CNY_PROVIDER_URL, timeout=8)
    response.raise_for_status()
    payload = response.json()
    candidates = [
        payload.get("rate"),
        (payload.get("rates") or {}).get("CNY"),
        (payload.get("data") or {}).get("rate"),
        (payload.get("data") or {}).get("CNY"),
    ]
    rate = None
    for value in candidates:
        rate = _valid_rate(value)
        if rate is not None:
            break
    if rate is None:
        raise ValueError("configured USD/CNY provider returned no valid rate")
    return ExchangeRateQuote(
        pair="USD/CNY",
        rate=rate,
        source="configured_provider",
        timestamp=str(payload.get("timestamp") or payload.get("date") or _now_iso()),
        is_realtime=True,
        raw_payload=payload,
    )


def _find_rate_in_payload(payload: Any) -> tuple[Decimal | None, dict[str, Any] | None]:
    keys = ("last_price", "lastPrice", "last", "price", "close", "latest_price")
    if isinstance(payload, dict):
        for key in keys:
            rate = _valid_rate(payload.get(key))
            if rate is not None:
                return rate, payload
        for value in payload.values():
            rate, node = _find_rate_in_payload(value)
            if rate is not None:
                return rate, node
    elif isinstance(payload, list):
        for value in payload:
            rate, node = _find_rate_in_payload(value)
            if rate is not None:
                return rate, node
    return None, None


def _alltick_provider() -> ExchangeRateQuote | None:
    if not ALLTICK_API_KEY or not ALLTICK_USD_CNY_SYMBOL:
        return None
    bases = [ALLTICK_HTTP_BASE] if ALLTICK_HTTP_BASE else []
    bases.extend(["https://quote.alltick.co", "https://quote.alltick.io"])
    query = {
        "trace": f"usd_cny_{int(time.time() * 1000)}",
        "data": {"symbol_list": [{"code": ALLTICK_USD_CNY_SYMBOL}]},
    }
    errors = []
    for base in dict.fromkeys(item.rstrip("/") for item in bases if item):
        try:
            response = http_get(
                base + "/quote-b-api/trade-tick",
                params={
                    "token": ALLTICK_API_KEY,
                    "query": json.dumps(
                        query, ensure_ascii=False, separators=(",", ":")
                    ),
                },
                timeout=10,
                referer="https://alltick.co/",
            )
            response.raise_for_status()
            payload = response.json()
            rate, raw_node = _find_rate_in_payload(payload)
            if rate is None:
                raise ValueError("response contained no valid rate")
            return ExchangeRateQuote(
                pair="USD/CNY",
                rate=rate,
                source=f"AllTick {ALLTICK_USD_CNY_SYMBOL}",
                timestamp=_now_iso(),
                is_realtime=True,
                raw_payload=raw_node or payload,
            )
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("; ".join(errors))


def _yahoo_provider() -> ExchangeRateQuote:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/CNY=X"
    response = http_get(
        url,
        params={"interval": "1m", "range": "1d"},
        timeout=8,
        referer="https://finance.yahoo.com/",
    )
    response.raise_for_status()
    payload = response.json()
    result = ((payload.get("chart") or {}).get("result") or [None])[0] or {}
    meta = result.get("meta") or {}
    rate = _valid_rate(meta.get("regularMarketPrice"))
    if rate is None:
        closes = (((result.get("indicators") or {}).get("quote") or [{}])[0]).get(
            "close"
        ) or []
        rate = next(
            (_valid_rate(value) for value in reversed(closes) if value is not None),
            None,
        )
    if rate is None:
        raise ValueError("Yahoo Finance returned no valid USD/CNY rate")
    market_time = meta.get("regularMarketTime")
    timestamp = (
        datetime.fromtimestamp(int(market_time), timezone.utc).isoformat(timespec="seconds")
        if market_time
        else _now_iso()
    )
    return ExchangeRateQuote(
        pair="USD/CNY",
        rate=rate,
        source="Yahoo Finance CNY=X",
        timestamp=timestamp,
        is_realtime=True,
        raw_payload={"meta": meta},
    )


def _eastmoney_provider() -> ExchangeRateQuote:
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    response = http_get(
        url,
        params={
            "secid": "133.USDCNY",
            "fields": "f43,f57,f58,f59,f86",
        },
        timeout=8,
        referer="https://quote.eastmoney.com/",
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or {}
    raw_rate = to_decimal(data.get("f43"))
    decimals = int(data.get("f59") or 0)
    if raw_rate is not None and raw_rate > Decimal("12") and decimals:
        raw_rate /= Decimal("10") ** decimals
    rate = _valid_rate(raw_rate)
    if rate is None:
        raise ValueError("Eastmoney returned no valid USD/CNY rate")
    market_time = data.get("f86")
    timestamp = (
        datetime.fromtimestamp(int(market_time), timezone.utc).isoformat(timespec="seconds")
        if market_time
        else _now_iso()
    )
    return ExchangeRateQuote(
        pair="USD/CNY",
        rate=rate,
        source="Eastmoney USDCNY",
        timestamp=timestamp,
        is_realtime=True,
        raw_payload=data,
    )


def _frankfurter_provider() -> ExchangeRateQuote:
    response = http_get(
        "https://api.frankfurter.app/latest",
        params={"from": "USD", "to": "CNY"},
        timeout=8,
    )
    response.raise_for_status()
    payload = response.json()
    rate = _valid_rate((payload.get("rates") or {}).get("CNY"))
    if rate is None:
        raise ValueError("Frankfurter returned no valid USD/CNY rate")
    return ExchangeRateQuote(
        pair="USD/CNY",
        rate=rate,
        source="Frankfurter / ECB reference",
        timestamp=str(payload.get("date") or _now_iso()),
        is_realtime=False,
        raw_payload=payload,
    )


def get_usd_cny_rate(
    force_refresh: bool = False,
    providers: list[Callable[[], ExchangeRateQuote | None]] | None = None,
) -> ExchangeRateQuote:
    global _cached_at, _cached_quote
    now = time.monotonic()
    with _cache_lock:
        if (
            not force_refresh
            and _cached_quote is not None
            and now - _cached_at < USD_CNY_CACHE_SECONDS
        ):
            return _cached_quote

    errors: list[str] = []
    for provider in providers or [
        _configured_provider,
        _alltick_provider,
        _eastmoney_provider,
        _yahoo_provider,
        _frankfurter_provider,
    ]:
        try:
            quote = provider()
            if quote is None:
                continue
            with _cache_lock:
                _cached_quote = quote
                _cached_at = now
            return quote
        except Exception as exc:
            errors.append(f"{provider.__name__}: {exc}")

    fallback = _valid_rate(USD_CNY_FALLBACK_RATE)
    if fallback is not None:
        return ExchangeRateQuote(
            pair="USD/CNY",
            rate=fallback,
            source="USD_CNY_FALLBACK_RATE",
            timestamp=_now_iso(),
            is_realtime=False,
            is_fallback=True,
            raw_payload={"provider_errors": errors},
        )
    raise RuntimeError("无法获取实时 USD/CNY 汇率，且未配置有效兜底值：" + "; ".join(errors))


def clear_exchange_rate_cache() -> None:
    global _cached_at, _cached_quote
    with _cache_lock:
        _cached_quote = None
        _cached_at = 0.0
