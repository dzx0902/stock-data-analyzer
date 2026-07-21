# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import csv
import io
import json
import math
import os
import re
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, quote_plus, urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from openai import OpenAI

from gold_agent.config import (
    ALLTICK_API_KEY,
    ALLTICK_GOLD_SYMBOL,
    ALLTICK_HTTP_BASE,
    ALLTICK_SILVER_SYMBOL,
    APP_TIMEZONE,
)
from gold_agent.infra.http import (
    common_headers,
    compact_text,
    http_get,
    http_post,
    now_ts,
    safe_float,
)
from gold_agent.infra.decimal_utils import decimal_text, to_decimal
from gold_agent.market.model import PriceQuote
from gold_agent.market.compare import compare_international_to_sge, compare_to_reference
from gold_agent.market.exchange_rate import ExchangeRateQuote, get_usd_cny_rate

def valid_price(v: Optional[float], unit: str) -> bool:
    if v is None:
        return False
    if unit == "CNY/g":
        return 200 <= v <= 2000
    if unit == "USD/oz":
        return 1000 <= v <= 10000
    return True


def normalize_quote(
    source: str,
    symbol: str,
    price: float,
    unit: str,
    timestamp: Optional[str] = None,
    confidence: float = 0.8,
    extra: Optional[dict] = None,
    raw_payload: Any = None,
    quote_type: str = "",
    is_realtime: Optional[bool] = None,
) -> Dict[str, Any]:
    metadata = dict(extra or {})
    currency, unit_name = ("CNY", "g") if unit == "CNY/g" else ("USD", "oz")
    category = str(metadata.get("category", "") or "")
    resolved_type = quote_type or {
        "sge_spot": "spot",
        "international_gold": "spot",
        "brand_gold": "brand_retail",
        "bank_gold_bar": "bank_sell",
        "gold_recycle": "recycle",
    }.get(category, "spot")
    realtime = bool(metadata.get("is_realtime", False)) if is_realtime is None else is_realtime
    asset = "silver" if "SILVER" in symbol.upper() or "XAG" in symbol.upper() else "gold"
    quote = PriceQuote(
        asset=asset,
        symbol=symbol,
        price=to_decimal(price) or Decimal("0"),
        currency=currency,
        unit=unit_name,
        source=source,
        timestamp=timestamp or time.strftime("%Y-%m-%d %H:%M:%S"),
        quote_type=resolved_type,
        is_realtime=realtime,
        raw_payload=raw_payload,
        confidence_score=to_decimal(confidence) or Decimal("0.8"),
        metadata=metadata,
    )
    return quote.to_legacy_dict()


# ==============================================================================
# 2. 黄金价格：精确口径查询
# ==============================================================================

# 轻量缓存，避免同一轮反复抓网页
_quote_cache: Dict[str, Tuple[int, Any]] = {}
_quote_cache_lock = threading.Lock()


def cache_get(key: str, ttl: int = 20) -> Optional[Any]:
    with _quote_cache_lock:
        item = _quote_cache.get(key)
        if not item:
            return None
        ts, data = item
        if now_ts() - ts <= ttl:
            return data
        return None


def cache_set(key: str, value: Any) -> None:
    with _quote_cache_lock:
        _quote_cache[key] = (now_ts(), value)


def fetch_sge_au9999_price() -> Dict[str, Any]:
    """
    上海黄金交易所官方公开接口：Au99.99 日行情。
    注意：这个接口通常是日线/最新交易日数据，不一定是 10 秒级实时。
    但它比网页二次站点更适合作为“上海金 / 国内金价”的默认权威口径。
    """
    cache_key = "sge:Au99.99"
    cached = cache_get(cache_key, ttl=60)
    if cached:
        return cached

    url = "https://www.sge.com.cn/graph/Dailyhq"
    headers_referer = "https://www.sge.com.cn/"
    payload = {"instid": "Au99.99"}

    try:
        resp = http_post(url, data=payload, timeout=12, referer=headers_referer)
        resp.raise_for_status()
        data = resp.json()

        rows = None
        for key in ["time", "data", "rows", "list"]:
            if isinstance(data, dict) and isinstance(data.get(key), list):
                rows = data.get(key)
                break
        if not rows:
            return {"status": "error", "message": "上海黄金交易所接口返回为空", "raw": data}

        # 找最后一条有效行。常见格式：[日期, 开盘价, 最高价, 最低价, 收盘价]
        latest = None
        for row in reversed(rows):
            if isinstance(row, (list, tuple)) and len(row) >= 5:
                close_price = safe_float(row[4])
                if valid_price(close_price, "CNY/g"):
                    latest = row
                    break
            elif isinstance(row, dict):
                price = safe_float(
                    row.get("close")
                    or row.get("closePrice")
                    or row.get("price")
                    or row.get("last")
                    or row.get("lastPrice")
                )
                if valid_price(price, "CNY/g"):
                    latest = row
                    break

        if latest is None:
            return {"status": "error", "message": "上海黄金交易所接口未解析到 Au99.99 有效价格", "raw": data}

        if isinstance(latest, (list, tuple)):
            trade_date = str(latest[0])
            open_price = safe_float(latest[1]) if len(latest) > 1 else None
            high = safe_float(latest[2]) if len(latest) > 2 else None
            low = safe_float(latest[3]) if len(latest) > 3 else None
            close_price = safe_float(latest[4])
            volume = latest[5] if len(latest) > 5 else None
        else:
            trade_date = str(latest.get("date") or latest.get("tradeDate") or latest.get("time") or "")
            open_price = safe_float(latest.get("open") or latest.get("openPrice"))
            high = safe_float(latest.get("high") or latest.get("highPrice"))
            low = safe_float(latest.get("low") or latest.get("lowPrice"))
            close_price = safe_float(
                latest.get("close")
                or latest.get("closePrice")
                or latest.get("price")
                or latest.get("last")
                or latest.get("lastPrice")
            )
            volume = latest.get("volume")

        if not valid_price(close_price, "CNY/g"):
            return {"status": "error", "message": "上海黄金交易所 Au99.99 价格超出合理范围", "raw": latest}

        result = normalize_quote(
            source="上海黄金交易所",
            symbol="Au99.99",
            price=close_price,
            unit="CNY/g",
            timestamp=trade_date or None,
            confidence=0.96,
            raw_payload=latest,
            extra={
                "category": "sge_spot",
                "display_name": "上海黄金交易所 Au99.99",
                "open": open_price,
                "high": high,
                "low": low,
                "close": close_price,
                "volume": volume,
                "note": "SGE Dailyhq 通常为最新交易日行情，不保证为秒级实时。",
            },
        )
        cache_set(cache_key, result)
        return result

    except Exception as e:
        return {"status": "error", "message": f"上海黄金交易所 Au99.99 获取失败：{e}"}


def fetch_sina_sge_realtime_price() -> Dict[str, Any]:
    """获取新浪公开行情转发的上金所 Au99.99 最新成交。

    这是第三方实时行情入口，不冒充上金所官方接口。交易时段内通常为秒级更新，
    休市后返回当日最后一笔成交。
    """
    cache_key = "sina:gds_AU9999"
    cached = cache_get(cache_key, ttl=10)
    if cached:
        return cached

    url = "https://hq.sinajs.cn/list=gds_AU9999"
    try:
        headers = common_headers(referer="https://finance.sina.com.cn/")
        resp = requests.get(url, headers=headers, timeout=8)
        resp.encoding = "gbk"
        text = resp.text or ""
        if '="' not in text:
            return {"status": "error", "message": "新浪上金所实时行情返回格式异常"}
        body = text.split('="', 1)[1].rsplit('"', 1)[0]
        fields = body.split(",")
        if len(fields) < 14:
            return {"status": "error", "message": "新浪上金所 Au99.99 实时行情为空"}

        price = safe_float(fields[0])
        high = safe_float(fields[4])
        low = safe_float(fields[5])
        time_text = fields[6].strip()
        date_text = fields[12].strip()
        display_name = fields[13].strip() or "Au99.99"
        if not valid_price(price, "CNY/g"):
            return {"status": "error", "message": "新浪上金所 Au99.99 最新价超出合理范围"}

        quote_dt = None
        try:
            quote_dt = datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=ZoneInfo(APP_TIMEZONE)
            )
        except Exception:
            pass
        now_local = datetime.now(ZoneInfo(APP_TIMEZONE))
        age_seconds = max(0, int((now_local - quote_dt).total_seconds())) if quote_dt else None
        market_live = age_seconds is not None and age_seconds <= 300

        result = normalize_quote(
            source="新浪财经公开行情（上金所行情转发）",
            symbol="Au99.99",
            price=price,
            unit="CNY/g",
            timestamp=f"{date_text} {time_text}".strip(),
            confidence=0.90,
            raw_payload={"fields": fields},
            extra={
                "category": "sge_spot",
                "display_name": f"上海黄金交易所 {display_name}",
                "high": high,
                "low": low,
                "is_realtime": market_live,
                "age_seconds": age_seconds,
                "market_status": "交易时段实时行情" if market_live else "最近一笔成交（可能已休市）",
                "note": "第三方公开行情转发；交易时段通常秒级更新，休市后为最后成交。",
            },
        )
        cache_set(cache_key, result)
        return result
    except Exception as e:
        return {"status": "error", "message": f"新浪上金所 Au99.99 实时行情获取失败：{e}"}


def fetch_huilvbiao_static_text() -> Tuple[str, List[str]]:
    cache_key = "huilvbiao:static:text"
    cached = cache_get(cache_key, ttl=60)
    if cached:
        return cached

    url = "https://www.huilvbiao.com/gold"
    resp = http_get(url, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text("\n")
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    joined = "\n".join(lines)
    result = (joined, lines)
    cache_set(cache_key, result)
    return result


def fetch_brand_gold_price(brand: str) -> Dict[str, Any]:
    brand = (brand or "").strip()
    if not brand:
        return {"status": "error", "message": "请指定品牌，例如 周大福、老凤祥、中国黄金"}

    aliases = {
        "周大福": ["周大福"],
        "周生生": ["周生生"],
        "周六福": ["周六福"],
        "六福": ["六福珠宝", "六福"],
        "六福珠宝": ["六福珠宝", "六福"],
        "老凤祥": ["老凤祥"],
        "老庙": ["老庙黄金", "老庙"],
        "老庙黄金": ["老庙黄金", "老庙"],
        "中国黄金": ["中国黄金"],
        "菜百": ["菜百首饰", "菜百"],
        "菜百首饰": ["菜百首饰", "菜百"],
        "潮宏基": ["潮宏基"],
        "金至尊": ["金至尊"],
        "谢瑞麟": ["谢瑞麟"],
    }
    candidates = aliases.get(brand, [brand])

    try:
        joined, _ = fetch_huilvbiao_static_text()
        for name in candidates:
            # 示例：周大福 1238 - 1086 元/克 2026-06-12
            pattern = (
                rf"{re.escape(name)}\s+"
                rf"(?P<gold>\d{{3,5}}(?:\.\d+)?)\s+"
                rf"(?P<platinum>-|\d{{3,5}}(?:\.\d+)?)\s+"
                rf"(?P<bar>-|\d{{3,5}}(?:\.\d+)?)\s+"
                rf"元/克\s+"
                rf"(?P<date>\d{{4}}-\d{{2}}-\d{{2}})"
            )
            m = re.search(pattern, joined)
            if not m:
                continue

            gold_price = safe_float(m.group("gold"))
            bar_price = safe_float(m.group("bar")) if m.group("bar") != "-" else None
            platinum_price = safe_float(m.group("platinum")) if m.group("platinum") != "-" else None
            date_text = m.group("date")

            if not valid_price(gold_price, "CNY/g"):
                continue

            return normalize_quote(
                source="汇率表",
                symbol=f"BRAND_GOLD_{name}",
                price=gold_price,
                unit="CNY/g",
                timestamp=date_text,
                confidence=0.84,
                raw_payload={"matched_text": m.group(0)},
                extra={
                    "category": "brand_gold",
                    "display_name": f"{name} 黄金首饰",
                    "brand": name,
                    "gold_price": gold_price,
                    "gold_bar_price": bar_price,
                    "platinum_price": platinum_price,
                    "date": date_text,
                },
            )

        return {"status": "error", "message": f"暂时没查到 {brand} 的品牌金价"}
    except Exception as e:
        return {"status": "error", "message": f"品牌金价获取失败：{e}"}


def fetch_bank_gold_bar_price(bank: str = "") -> Dict[str, Any]:
    bank = (bank or "").strip()
    bank_names = [
        "浦发银行投资金条",
        "和谐平安金条",
        "农行传世之宝金条",
        "工商银行如意金条",
        "中国银行金条",
        "建设银行龙鼎金条",
    ]

    if bank:
        bank_alias = {
            "浦发": "浦发银行投资金条",
            "浦发银行": "浦发银行投资金条",
            "农行": "农行传世之宝金条",
            "农业银行": "农行传世之宝金条",
            "工行": "工商银行如意金条",
            "工商银行": "工商银行如意金条",
            "中行": "中国银行金条",
            "中国银行": "中国银行金条",
            "建行": "建设银行龙鼎金条",
            "建设银行": "建设银行龙鼎金条",
        }
        target_names = [bank_alias.get(bank, bank)]
    else:
        target_names = bank_names

    try:
        joined, _ = fetch_huilvbiao_static_text()
        found: List[Dict[str, Any]] = []
        for name in target_names:
            pattern = rf"{re.escape(name)}\s+(?P<price>\d{{3,5}}(?:\.\d+)?)"
            m = re.search(pattern, joined)
            if not m:
                continue
            price = safe_float(m.group("price"))
            if not valid_price(price, "CNY/g"):
                continue
            found.append(
                normalize_quote(
                    source="汇率表",
                    symbol=f"BANK_BAR_{name}",
                    price=price,
                    unit="CNY/g",
                    confidence=0.86,
                    raw_payload={"matched_text": m.group(0)},
                    extra={
                        "category": "bank_gold_bar",
                        "display_name": name,
                        "bank": name,
                    },
                )
            )

        if not found:
            label = bank or "银行金条"
            return {"status": "error", "message": f"暂时没查到 {label} 价格"}

        # 未指定银行时，只返回一个主结果，其他放 alternatives，避免刷屏。
        best = found[0]
        best["alternatives"] = found[1:]
        return best
    except Exception as e:
        return {"status": "error", "message": f"银行金条价格获取失败：{e}"}


def fetch_gold_recycle_price() -> Dict[str, Any]:
    try:
        joined, _ = fetch_huilvbiao_static_text()
        patterns = [
            r"24K金回收\s+(?P<price>\d{3,5}(?:\.\d+)?)\s+元/克\s+(?P<date>\d{4}-\d{2}-\d{2})",
            r"黄金回收\s+(?P<price>\d{3,5}(?:\.\d+)?)\s+元/克\s+(?P<date>\d{4}-\d{2}-\d{2})",
        ]
        for pattern in patterns:
            m = re.search(pattern, joined)
            if not m:
                continue
            price = safe_float(m.group("price"))
            if not valid_price(price, "CNY/g"):
                continue
            return normalize_quote(
                source="汇率表",
                symbol="GOLD_RECYCLE_24K",
                price=price,
                unit="CNY/g",
                timestamp=m.group("date"),
                confidence=0.84,
                raw_payload={"matched_text": m.group(0)},
                extra={
                    "category": "gold_recycle",
                    "display_name": "24K金回收",
                    "date": m.group("date"),
                },
            )
        return {"status": "error", "message": "暂时没查到黄金回收价"}
    except Exception as e:
        return {"status": "error", "message": f"黄金回收价获取失败：{e}"}


def fetch_sina_international_gold_price() -> Dict[str, Any]:
    url = "https://hq.sinajs.cn/list=hf_GC"
    try:
        headers = common_headers(referer="https://finance.sina.com.cn/")
        headers["Referer"] = "https://finance.sina.com.cn/"
        resp = requests.get(url, headers=headers, timeout=8)
        resp.encoding = "gbk"
        text = resp.text or ""
        if '="' not in text:
            return {"status": "error", "message": "新浪财经返回格式异常", "raw": text[:300]}
        body = text.split('="', 1)[1].rsplit('"', 1)[0]
        fields = body.split(",")
        price = safe_float(fields[0] if fields else None)
        if not valid_price(price, "USD/oz"):
            return {"status": "error", "message": "新浪财经国际金价超出合理范围", "raw": text[:300]}
        high = safe_float(fields[4]) if len(fields) > 4 else None
        low = safe_float(fields[5]) if len(fields) > 5 else None
        timestamp = None
        # 新浪 hf 字段不同品种会有差异，能解析则解析，不能就用当前时间。
        for f in reversed(fields):
            if re.match(r"\d{4}-\d{2}-\d{2}", f):
                timestamp = f
                break
        return normalize_quote(
            source="新浪财经",
            symbol="COMEX_GC",
            price=price,
            unit="USD/oz",
            timestamp=timestamp,
            confidence=0.78,
            raw_payload={"fields": fields, "text": text[:500]},
            extra={
                "category": "international_gold",
                "display_name": "COMEX 黄金",
                "high": high,
                "low": low,
                "raw_text": text[:500],
            },
        )
    except Exception as e:
        return {"status": "error", "message": f"新浪财经国际金价获取失败：{e}"}


def _json_loads_loose(text: str) -> Optional[Dict[str, Any]]:
    """AllTick/网关异常时可能返回 HTML、空串或前后有噪声；这里做安全 JSON 解析。"""
    if not text:
        return None
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {"_root": obj}
    except Exception:
        pass
    # 尝试从响应中截取第一个 JSON 对象
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else {"_root": obj}
        except Exception:
            return None
    return None


def _flatten_json(obj: Any) -> List[Any]:
    out: List[Any] = []
    def walk(x: Any):
        out.append(x)
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(obj)
    return out


def _extract_price_from_alltick_payload(data: Dict[str, Any]) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
    """兼容 AllTick 不同版本字段：last_price/price/close/bid/ask/new_price 等。"""
    preferred_keys = [
        "last_price", "lastPrice", "last", "price", "close", "c", "new_price", "newPrice",
        "latest_price", "latestPrice", "bid", "ask", "bp", "ap"
    ]
    for node in _flatten_json(data):
        if not isinstance(node, dict):
            continue
        # 优先找带 XAU/GOLD/FOREX 语义的节点；没有也继续尝试。
        text_blob = json.dumps(node, ensure_ascii=False)[:500].lower()
        looks_related = any(x in text_blob for x in ["xau", "gold", "黄金", "forex", "gc"])
        for k in preferred_keys:
            if k in node:
                price = safe_float(node.get(k))
                if valid_price(price, "USD/oz"):
                    if looks_related or price >= 1000:
                        return price, node
    return None, None


def _alltick_base_candidates() -> List[str]:
    """AllTick HTTP 网关候选。

    官方文档给出的 HTTP 基础域名是 https://quote.alltick.co；
    这里把它放在第一位。quote.alltick.io 作为备用官网/网关兜底。
    不再默认打 api.alltick.co/v1/market/realtime，那不是当前文档里的标准接口。
    """
    bases: List[str] = []
    if ALLTICK_HTTP_BASE:
        bases.append(ALLTICK_HTTP_BASE)
    bases.extend([
        "https://quote.alltick.co",
        "https://quote.alltick.io",
    ])
    out: List[str] = []
    for b in bases:
        b = b.rstrip("/")
        if b and b not in out:
            out.append(b)
    return out


def _alltick_trace(prefix: str = "gold_agent") -> str:
    return f"{prefix}_{int(time.time() * 1000)}"


def _alltick_query(data: Dict[str, Any], prefix: str = "gold_agent") -> str:
    # requests 会负责 URL encode；这里返回紧凑 JSON 字符串即可。
    return json.dumps(
        {"trace": _alltick_trace(prefix), "data": data},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _alltick_request_candidates(symbol: str = "GOLD") -> List[Tuple[str, Dict[str, Any]]]:
    """按 AllTick 官方文档生成最新成交价请求。

    最新成交价接口：GET /quote-b-api/trade-tick
    query 结构：{"trace":"...","data":{"symbol_list":[{"code":"GOLD"}]}}
    注意：贵金属/商品走 quote-b-api，不走 quote-stock-b-api。
    """
    token = ALLTICK_API_KEY
    symbol = (symbol or ALLTICK_GOLD_SYMBOL or "GOLD").strip()
    reqs: List[Tuple[str, Dict[str, Any]]] = []
    for base in _alltick_base_candidates():
        reqs.append((
            base + "/quote-b-api/trade-tick",
            {"token": token, "query": _alltick_query({"symbol_list": [{"code": symbol}]}, "gold_tick")},
        ))
    return reqs


def fetch_alltick_gold_price() -> Dict[str, Any]:
    """获取 AllTick 黄金最新成交价。

    当前按官方文档实现：
    - URL: https://quote.alltick.co/quote-b-api/trade-tick
    - 参数: token + query(JSON string)
    - 黄金 code 默认 GOLD，不再默认 XAUUSD。
    """
    if not ALLTICK_API_KEY:
        return {"status": "error", "message": "未配置 ALLTICK_API_KEY"}

    attempts: List[Dict[str, Any]] = []
    symbol = (ALLTICK_GOLD_SYMBOL or "GOLD").strip()
    for url, params in _alltick_request_candidates(symbol):
        try:
            resp = requests.get(url, params=params, headers=common_headers(referer="https://alltick.co/"), timeout=12)
            head = (resp.text or "")[:800]
            attempt = {
                "url": url,
                "symbol": symbol,
                "status_code": resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "head": compact_text(head, 400),
            }
            data = _json_loads_loose(resp.text or "")
            if data is not None:
                attempt["ret"] = data.get("ret")
                attempt["msg"] = data.get("msg")
                attempt["json_keys"] = list(data.keys())[:12]
                price, raw_node = _extract_price_from_alltick_payload(data)
                if price is not None:
                    return normalize_quote(
                        source="AllTick",
                        symbol=symbol,
                        price=price,
                        unit="USD/oz",
                        confidence=0.95,
                        raw_payload=raw_node or data,
                        extra={
                            "category": "international_gold",
                            "display_name": f"AllTick {symbol}",
                            "endpoint": url,
                            "raw": raw_node or data,
                        },
                    )
                if data.get("ret") not in (None, 200, "200"):
                    attempt["api_response"] = data
            attempts.append(attempt)
        except Exception as e:
            attempts.append({"url": url, "symbol": symbol, "error": str(e)})

    return {
        "status": "error",
        "message": f"AllTick 未获取到有效黄金价格。当前 symbol={symbol}。请确认产品 code 列表是否包含 GOLD，以及 token 是否有商品/贵金属权限。",
        "attempts": attempts[-6:],
    }


def _extract_price_from_alltick_payload_range(data: Dict[str, Any], min_price: float, max_price: float) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
    """按价格范围解析 AllTick 最新价，给白银等非黄金品种使用。"""
    preferred_keys = [
        "last_price", "lastPrice", "last", "price", "close", "c", "new_price", "newPrice",
        "latest_price", "latestPrice", "bid", "ask", "bp", "ap"
    ]
    for node in _flatten_json(data):
        if not isinstance(node, dict):
            continue
        for k in preferred_keys:
            if k in node:
                price = safe_float(node.get(k))
                if price is not None and min_price <= price <= max_price:
                    return price, node
    return None, None


def fetch_alltick_symbol_price(
    symbol: str,
    display_name: str,
    category: str,
    unit: str = "USD/oz",
    min_price: float = 1.0,
    max_price: float = 10000.0,
) -> Dict[str, Any]:
    """按 AllTick 官方 trade-tick 接口获取任意贵金属 code 最新价。"""
    if not ALLTICK_API_KEY:
        return {"status": "error", "message": "未配置 ALLTICK_API_KEY"}
    symbol = (symbol or "").strip()
    if not symbol:
        return {"status": "error", "message": "未指定 AllTick symbol"}

    attempts: List[Dict[str, Any]] = []
    for base in _alltick_base_candidates():
        url = base + "/quote-b-api/trade-tick"
        params = {"token": ALLTICK_API_KEY, "query": _alltick_query({"symbol_list": [{"code": symbol}]}, "metal_tick")}
        try:
            resp = requests.get(url, params=params, headers=common_headers(referer="https://alltick.co/"), timeout=12)
            data = _json_loads_loose(resp.text or "")
            attempt = {
                "url": url,
                "symbol": symbol,
                "status_code": resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "head": compact_text((resp.text or "")[:800], 400),
            }
            if data is not None:
                attempt["ret"] = data.get("ret")
                attempt["msg"] = data.get("msg")
                price, raw_node = _extract_price_from_alltick_payload_range(data, min_price, max_price)
                if price is not None:
                    return normalize_quote(
                        source="AllTick",
                        symbol=symbol,
                        price=price,
                        unit=unit,
                        confidence=0.94,
                        raw_payload=raw_node or data,
                        extra={
                            "category": category,
                            "display_name": display_name,
                            "endpoint": url,
                            "raw": raw_node or data,
                            "quote_note": "AllTick quote-b-api trade-tick，商品/贵金属 code；具体交易场所按 AllTick 产品列表为准。",
                        },
                    )
            attempts.append(attempt)
        except Exception as e:
            attempts.append({"url": url, "symbol": symbol, "error": str(e)})
    return {"status": "error", "message": f"AllTick 未获取到 {display_name} 有效价格，symbol={symbol}", "attempts": attempts[-6:]}


def fetch_alltick_silver_price() -> Dict[str, Any]:
    """白银实时价。默认 AllTick code=SILVER，必要时用 ALLTICK_SILVER_SYMBOL 覆盖。"""
    # 白银美元/盎司常见范围 5-200，避免把黄金价格误解析成白银。
    return fetch_alltick_symbol_price(
        symbol=ALLTICK_SILVER_SYMBOL or "SILVER",
        display_name=f"AllTick {ALLTICK_SILVER_SYMBOL or 'SILVER'} 白银",
        category="international_silver",
        unit="USD/oz",
        min_price=5,
        max_price=200,
    )


def _alltick_kline_request_candidates(symbol: str, days: int) -> List[Tuple[str, Dict[str, Any]]]:
    """按 AllTick 官方文档生成历史 K 线请求。

    K线接口：GET /quote-b-api/kline
    日K kline_type=8；query_kline_num 最大 500。
    """
    token = ALLTICK_API_KEY
    symbol = (symbol or ALLTICK_GOLD_SYMBOL or "GOLD").strip()
    query_num = max(30, min(int(days or 180) + 20, 500))
    reqs: List[Tuple[str, Dict[str, Any]]] = []
    data = {
        "code": symbol,
        "kline_type": 8,
        "kline_timestamp_end": 0,
        "query_kline_num": query_num,
        "adjust_type": 0,
    }
    for base in _alltick_base_candidates():
        reqs.append((
            base + "/quote-b-api/kline",
            {"token": token, "query": _alltick_query(data, "gold_kline_d")},
        ))
    return reqs


def _extract_kline_rows_from_alltick(data: Dict[str, Any], limit: int, min_price: float = 1000.0, max_price: float = 10000.0) -> List[Dict[str, Any]]:
    """解析 AllTick 官方 kline_list。

    官方字段是 open_price / close_price / high_price / low_price / timestamp。
    旧版代码只识别 open/close/high/low，导致 close 为空，所以历史行情一直失败。
    """
    rows: List[Dict[str, Any]] = []

    # 优先走官方路径 data.kline_list
    root_data = data.get("data") if isinstance(data, dict) else None
    if isinstance(root_data, dict) and isinstance(root_data.get("kline_list"), list):
        candidate = root_data.get("kline_list") or []
    else:
        # 兜底：在 JSON 里找像 kline 的列表
        lists = [x for x in _flatten_json(data) if isinstance(x, list) and len(x) >= 2]
        candidate = []
        for arr in lists:
            if arr and isinstance(arr[0], dict) and any(k in arr[0] for k in ["close_price", "open_price", "timestamp"]):
                candidate = arr
                break

    for item in candidate:
        if not isinstance(item, dict):
            continue
        close = safe_float(item.get("close_price") or item.get("close") or item.get("c") or item.get("price"))
        if close is None or not (min_price <= close <= max_price):
            continue
        ts = item.get("timestamp") or item.get("time") or item.get("t") or item.get("kline_timestamp") or item.get("date")
        if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit()):
            ts_i = int(float(ts))
            if ts_i > 10_000_000_000:
                ts_i = ts_i // 1000
            date = datetime.fromtimestamp(ts_i).strftime("%Y-%m-%d")
        else:
            date = str(ts or "")[:10] or datetime.now().strftime("%Y-%m-%d")
        rows.append({
            "date": date,
            "open": safe_float(item.get("open_price") or item.get("open") or item.get("o")),
            "high": safe_float(item.get("high_price") or item.get("high") or item.get("h")),
            "low": safe_float(item.get("low_price") or item.get("low") or item.get("l")),
            "close": close,
            "volume": safe_float(item.get("volume")),
        })

    dedup: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if r.get("date"):
            dedup[str(r["date"])] = r
    out = [dedup[k] for k in sorted(dedup.keys())]
    return out[-limit:]


def fetch_alltick_daily_history(symbol: str = "GOLD", range_days: int = 180) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """用 AllTick 官方 /quote-b-api/kline 取日K历史数据。"""
    if not ALLTICK_API_KEY:
        return [], [{"error": "未配置 ALLTICK_API_KEY"}]
    attempts: List[Dict[str, Any]] = []
    symbol = (symbol or ALLTICK_GOLD_SYMBOL or "GOLD").strip()
    for url, params in _alltick_kline_request_candidates(symbol, range_days):
        try:
            resp = requests.get(url, params=params, headers=common_headers(referer="https://alltick.co/"), timeout=15)
            head = (resp.text or "")[:800]
            attempt = {
                "url": url,
                "symbol": symbol,
                "status_code": resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "head": compact_text(head, 400),
            }
            data = _json_loads_loose(resp.text or "")
            if data is not None:
                attempt["ret"] = data.get("ret")
                attempt["msg"] = data.get("msg")
                if isinstance(data.get("data"), dict):
                    attempt["data_keys"] = list(data["data"].keys())[:12]
                    kl = data["data"].get("kline_list")
                    if isinstance(kl, list):
                        attempt["kline_count"] = len(kl)
                # 黄金通常 >1000 美元/盎司；白银通常 5-200 美元/盎司。
                if symbol.upper() in ["SILVER", "XAGUSD", "XAGUSD=X"] or "SILVER" in symbol.upper() or "XAG" in symbol.upper():
                    rows = _extract_kline_rows_from_alltick(data, range_days, 5, 200)
                else:
                    rows = _extract_kline_rows_from_alltick(data, range_days, 1000, 10000)
                if len(rows) >= 20:
                    return rows, attempts + [attempt]
                if data.get("ret") not in (None, 200, "200"):
                    attempt["api_response"] = data
            attempts.append(attempt)
        except Exception as e:
            attempts.append({"url": url, "symbol": symbol, "error": str(e)})
    return [], attempts[-6:]


def get_gold_quote(quote_type: str, brand: str = "", bank: str = "") -> Dict[str, Any]:
    """
    给 LLM 调用的唯一金价工具：只查一个口径。
    quote_type:
      - sge_spot: 上海黄金交易所 Au99.99
      - brand: 品牌金店
      - bank_bar: 银行金条/积存金参考
      - recycle: 回收价
      - international: 国际金价
    """
    quote_type = (quote_type or "").strip().lower()
    if quote_type == "sge_spot":
        realtime = fetch_sina_sge_realtime_price()
        if realtime.get("status") == "success":
            return realtime
        official = fetch_sge_au9999_price()
        if official.get("status") == "success":
            official.setdefault("extra", {})["realtime_fallback_error"] = realtime.get("message")
            return official
        return {
            "status": "error",
            "message": f"国内金价获取失败。实时行情：{realtime.get('message')}；上金所日行情：{official.get('message')}",
        }
    if quote_type == "brand":
        return fetch_brand_gold_price(brand)
    if quote_type == "bank_bar":
        return fetch_bank_gold_bar_price(bank)
    if quote_type == "recycle":
        return fetch_gold_recycle_price()
    if quote_type == "international":
        # 优先 AllTick，失败再新浪；避免全量抓取。
        alltick = fetch_alltick_gold_price()
        if alltick.get("status") == "success":
            return alltick
        sina = fetch_sina_international_gold_price()
        if sina.get("status") == "success":
            return sina
        return {"status": "error", "message": f"国际金价获取失败。AllTick：{alltick.get('message')}；新浪：{sina.get('message')}"}
    return {"status": "error", "message": f"未知金价口径：{quote_type}"}


def get_all_gold_prices() -> Dict[str, Any]:
    """用户明确要“所有/整理”的时候才调用：汇总国际金、上海金、银行金条、品牌金店、回收价。"""
    brand_names = ["周六福", "中国黄金", "老凤祥", "老庙黄金", "菜百首饰", "潮宏基", "金至尊", "谢瑞麟", "六福珠宝", "周生生", "周大福"]
    results: Dict[str, Any] = {
        "status": "success",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "international": get_gold_quote("international"),
        "sge_spot": get_gold_quote("sge_spot"),
        "bank_bar": get_gold_quote("bank_bar"),
        "recycle": get_gold_quote("recycle"),
        "brands": [],
    }
    for b in brand_names:
        q = fetch_brand_gold_price(b)
        if q.get("status") == "success":
            results["brands"].append(q)
    return results


def _fmt_price_line(q: Dict[str, Any], fallback: str = "") -> str:
    if not q or q.get("status") != "success":
        return f"{fallback or '价格'}：未获取到"
    extra = q.get("extra") or {}
    name = extra.get("display_name") or q.get("symbol") or fallback or "价格"
    unit = "元/克" if q.get("unit") == "CNY/g" else "美元/盎司"
    ts = q.get("timestamp") or ""
    return f"{name}：{q.get('price')} {unit}" + (f"（{ts}）" if ts else "")


def format_all_gold_prices_reply(data: Dict[str, Any]) -> str:
    if data.get("status") != "success":
        return f"整理失败：{data.get('message', '未知错误')}"
    lines: List[str] = []
    lines.append(f"金价汇总（{data.get('timestamp')}）")
    lines.append("")
    lines.append("【国际金价】")
    intl = data.get("international") or {}
    lines.append(_fmt_price_line(intl, "国际黄金"))
    if intl.get("status") == "success":
        lines.append("口径：AllTick GOLD 为 AllTick 商品/贵金属国际黄金 code 的美元/盎司报价；不是上海金，也不等同于品牌金店价。")
    lines.append("")
    lines.append("【上海金】")
    lines.append(_fmt_price_line(data.get("sge_spot") or {}, "上海黄金交易所 Au99.99"))
    lines.append("")
    lines.append("【银行金条/积存金参考】")
    bank = data.get("bank_bar") or {}
    if bank.get("status") == "success":
        lines.append(_fmt_price_line(bank, "银行金条"))
        for alt in bank.get("alternatives") or []:
            lines.append(_fmt_price_line(alt, "银行金条"))
    else:
        lines.append("银行金条：未获取到")
    lines.append("")
    lines.append("【品牌金店】")
    brands = data.get("brands") or []
    if brands:
        for q in brands:
            extra = q.get("extra") or {}
            line = _fmt_price_line(q, extra.get("display_name", "品牌金价"))
            bar = extra.get("gold_bar_price")
            if bar is not None:
                line += f"；金条 {bar} 元/克"
            lines.append(line)
    else:
        lines.append("品牌金店：未获取到")
    lines.append("")
    lines.append("【回收价】")
    lines.append(_fmt_price_line(data.get("recycle") or {}, "24K金回收"))
    lines.append("")
    lines.append("说明：以上口径不同，不能互相替代；品牌首饰价含品牌/工费/渠道溢价，回收价通常低于投资金条/上海金。")
    return "\n".join(lines)


def format_silver_quote_reply(result: Dict[str, Any]) -> str:
    if result.get("status") != "success":
        return f"没查到白银实时价：{result.get('message', '未知错误')}\n可检查 ALLTICK_SILVER_SYMBOL 是否应为 SILVER、XAGUSD 或后台产品列表中的白银 code。"
    extra = result.get("extra") or {}
    name = extra.get("display_name") or result.get("symbol") or "白银"
    return "\n".join([
        f"{name}：{result.get('price')} 美元/盎司",
        f"来源：{result.get('source')}",
        f"时间：{result.get('timestamp')}",
        "口径：AllTick 商品/贵金属白银 code 的国际白银报价；不是旧新闻摘录。",
    ])


def format_gold_quote_reply(result: Dict[str, Any], requested_type: str = "") -> str:
    if result.get("status") != "success":
        return f"没查到这个口径的价格：{result.get('message', '未知错误')}"

    extra = result.get("extra") or {}
    name = extra.get("display_name") or result.get("symbol") or "黄金价格"
    price = result.get("price")
    unit = "元/克" if result.get("unit") == "CNY/g" else "美元/盎司"
    ts = result.get("timestamp") or "未知"
    source = result.get("source") or "未知来源"

    lines = [f"{name}：{price} {unit}", f"来源：{source}", f"时间：{ts}"]

    if extra.get("category") == "sge_spot":
        if extra.get("high") is not None and extra.get("low") is not None:
            lines.append(f"日内/当日区间：{extra.get('low')} - {extra.get('high')} 元/克")
        lines.append(f"行情状态：{extra.get('market_status', '上金所最新交易日行情')}")
        lines.append("口径：上海黄金交易所 Au99.99；实时数据来自第三方公开行情转发，失败时回退官方日行情。")
    elif extra.get("category") == "brand_gold":
        if extra.get("gold_bar_price") is not None:
            lines.append(f"金条价：{extra.get('gold_bar_price')} 元/克")
        if extra.get("platinum_price") is not None:
            lines.append(f"铂金价：{extra.get('platinum_price')} 元/克")
    elif extra.get("category") == "bank_gold_bar":
        # 不默认展示 alternatives，保持短回复。
        lines.append("口径：银行投资金条参考价。")
    elif extra.get("category") == "gold_recycle":
        lines.append("口径：24K 黄金回收参考价。")
    elif extra.get("category") == "international_gold":
        lines.append("口径：AllTick GOLD 商品/贵金属国际黄金美元/盎司报价；不是上海金，也不等同于品牌金店价。")

    return "\n".join(lines)


def analyze_price_relationships(
    brand: str = "",
    bank: str = "",
    usd_cny_rate: Any = None,
) -> Dict[str, Any]:
    sge_result = get_gold_quote("sge_spot")
    if sge_result.get("status") != "success":
        return {"status": "error", "message": "无法获取上金所基准价", "sge": sge_result}
    sge = PriceQuote.from_legacy_dict(sge_result)
    comparisons = []
    errors = []

    for label, quote_type, parameter in (
        ("银行金条", "bank_bar", bank),
        ("品牌金价", "brand", brand),
        ("黄金回收价", "recycle", ""),
    ):
        if quote_type == "brand" and not brand:
            continue
        result = get_gold_quote(
            quote_type,
            brand=parameter if quote_type == "brand" else "",
            bank=parameter if quote_type == "bank_bar" else "",
        )
        if result.get("status") != "success":
            errors.append({"label": label, "message": result.get("message", "获取失败")})
            continue
        comparison = compare_to_reference(label, result["price"], sge.price)
        comparisons.append(comparison.__dict__)

    rate_quote = None
    if usd_cny_rate is not None:
        rate = to_decimal(usd_cny_rate)
        if rate is not None:
            rate_quote = ExchangeRateQuote(
                pair="USD/CNY",
                rate=rate,
                source="user_supplied",
                timestamp=datetime.now().isoformat(timespec="seconds"),
                is_realtime=False,
                raw_payload=None,
            )
    else:
        try:
            rate_quote = get_usd_cny_rate()
            rate = rate_quote.rate
        except Exception as exc:
            rate = None
            errors.append({"label": "USD/CNY 汇率", "message": str(exc)})
    international_result = get_gold_quote("international")
    if rate and international_result.get("status") == "success":
        comparison = compare_international_to_sge(
            PriceQuote.from_legacy_dict(international_result),
            sge,
            rate,
        )
        comparisons.append(comparison.__dict__)
    elif international_result.get("status") == "success":
        errors.append({"label": "国际金价折算", "message": "缺少 USD/CNY 汇率，未执行折算"})
    else:
        errors.append({"label": "国际金价", "message": international_result.get("message", "获取失败")})

    return {
        "status": "success",
        "reference": sge.to_dict(),
        "usd_cny_rate": decimal_text(rate) if rate else None,
        "exchange_rate": rate_quote.to_dict() if rate_quote else None,
        "comparisons": [
            {
                **item,
                "compared_price_cny_g": decimal_text(item["compared_price_cny_g"]),
                "reference_price_cny_g": decimal_text(item["reference_price_cny_g"]),
                "spread_cny_g": decimal_text(item["spread_cny_g"]),
                "spread_pct": decimal_text(item["spread_pct"]),
            }
            for item in comparisons
        ],
        "warnings": errors,
    }
