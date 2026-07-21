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
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, quote_plus, urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from openai import OpenAI

from gold_agent.config import ALLTICK_GOLD_SYMBOL, ALLTICK_SILVER_SYMBOL
from gold_agent.infra.http import common_headers, http_get, safe_float
from gold_agent.market.prices import fetch_alltick_daily_history

def parse_chart_result(data: Dict[str, Any], range_days: int) -> List[Dict[str, Any]]:
    """Parse Yahoo chart JSON into OHLC rows."""
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return []
    r0 = result[0]
    timestamps = r0.get("timestamp") or []
    quote_obj = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote_obj.get("close") or []
    highs = quote_obj.get("high") or []
    lows = quote_obj.get("low") or []
    opens = quote_obj.get("open") or []
    rows: List[Dict[str, Any]] = []
    for i, ts in enumerate(timestamps):
        close = closes[i] if i < len(closes) else None
        if close is None:
            continue
        rows.append({
            "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
            "open": opens[i] if i < len(opens) else None,
            "high": highs[i] if i < len(highs) else None,
            "low": lows[i] if i < len(lows) else None,
            "close": float(close),
        })
    # 去重并按日期排序，防止接口返回重复/乱序
    dedup: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if r.get("date"):
            dedup[str(r["date"])] = r
    rows = [dedup[k] for k in sorted(dedup.keys())]
    return rows[-range_days:]


def fetch_yahoo_chart(symbol: str, range_days: int = 180, interval: str = "1d") -> List[Dict[str, Any]]:
    """Yahoo chart 公开接口兜底。

    国内服务器访问 Yahoo 经常触发 403/验证页，所以这里做三件事：
    1. 使用 requests.Session 保留 cookie；
    2. 使用更完整的浏览器 Header；
    3. 先访问 quote 页面暖 session，再请求 query1/query2 chart。

    这仍然只是兜底源；正式量化优先走 AllTick / 国内源。
    """
    range_days = max(30, min(int(range_days or 180), 730))
    rng = "1mo" if range_days <= 35 else "3mo" if range_days <= 100 else "6mo" if range_days <= 200 else "1y" if range_days <= 370 else "2y"
    now = int(time.time())
    period1 = now - (range_days + 10) * 86400
    encoded = quote(symbol, safe="")

    headers = common_headers(referer="https://finance.yahoo.com/")
    headers.update({
        "Accept": "application/json,text/plain,*/*",
        "Origin": "https://finance.yahoo.com",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    })

    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{encoded}",
    ]
    param_sets = [
        {"range": rng, "interval": interval, "includePrePost": "false", "events": "history"},
        {"period1": period1, "period2": now, "interval": interval, "includePrePost": "false", "events": "history"},
    ]

    with requests.Session() as sess:
        sess.headers.update(headers)
        try:
            # 暖 cookie；失败不影响后续。
            sess.get(f"https://finance.yahoo.com/quote/{encoded}", timeout=8)
            time.sleep(0.5)
        except Exception:
            pass

        for url in urls:
            for params in param_sets:
                try:
                    resp = sess.get(url, params=params, timeout=12)
                    ct = resp.headers.get("content-type", "")
                    if resp.status_code != 200:
                        print(f"[Yahoo历史行情失败] {symbol}: HTTP {resp.status_code}, ct={ct}, head={resp.text[:160]!r}")
                        continue
                    if "json" not in ct.lower() and not (resp.text or "").lstrip().startswith("{"):
                        print(f"[Yahoo历史行情失败] {symbol}: 非JSON响应 ct={ct}, head={resp.text[:160]!r}")
                        continue
                    data = resp.json()
                    rows = parse_chart_result(data, range_days)
                    if len(rows) >= 20:
                        return rows
                    err = ((data.get("chart") or {}).get("error") or {}) if isinstance(data, dict) else {}
                    if err:
                        print(f"[Yahoo历史行情失败] {symbol}: {err}")
                except Exception as e:
                    print(f"[Yahoo历史行情失败] {symbol}: {e}")
                time.sleep(0.4)
    return []


def stooq_symbol_candidates(symbol: str, asset_type: str = "gold") -> List[str]:
    """Map common gold symbols to Stooq symbols.

    Stooq 的代码和 Yahoo 不同，并且部分环境大小写/后缀会影响结果。
    因此同时尝试大写、小写、常见别名。
    """
    raw_original = (symbol or "").strip()
    raw = raw_original.lower()
    candidates: List[str] = []

    def add(x: str):
        if not x:
            return
        # 保留原样、大写、小写三种，便于排查 Stooq 返回空的问题。
        for y in [x, x.upper(), x.lower()]:
            if y and y not in candidates:
                candidates.append(y)

    add(raw_original)
    if raw in ["gc=f", "gc", "comex", "gold", "黄金", "xau", "xauusd=x", "xauusd", ""] or asset_type.lower() == "gold":
        for x in ["XAUUSD", "GC.F", "GLD.US", "xauusd", "gc.f", "gld.us"]:
            add(x)
    if "gld" in raw:
        for x in ["GLD.US", "gld.us", "GLD"]:
            add(x)
    if "xau" in raw:
        for x in ["XAUUSD", "xauusd"]:
            add(x)
    return candidates


def fetch_stooq_daily(symbol: str, range_days: int = 180) -> List[Dict[str, Any]]:
    """Stooq CSV 公开接口。常用于 XAUUSD/GLD 等兜底，失败返回空。

    注意：这里不再强制 lower()，会按调用方传入的大小写请求；
    fetch_history_rows 会同时尝试 GC.F / gc.f / GLD.US / gld.us。
    """
    urls = [
        f"https://stooq.com/q/d/l/?s={quote(symbol, safe='')}&i=d",
        f"https://stooq.com/q/d/l/?s={quote(symbol.lower(), safe='')}&i=d",
        f"https://stooq.com/q/d/l/?s={quote(symbol.upper(), safe='')}&i=d",
    ]
    tried = []
    for url in list(dict.fromkeys(urls)):
        try:
            tried.append(url)
            resp = http_get(url, timeout=12, referer="https://stooq.com/", use_search_proxy=False)
            text = resp.text or ""
            if resp.status_code != 200 or "No data" in text[:200] or "Date," not in text[:200]:
                print(f"[Stooq历史行情失败] {symbol}: HTTP {resp.status_code}, url={url}, head={text[:100]!r}")
                continue
            reader = csv.DictReader(io.StringIO(text))
            rows: List[Dict[str, Any]] = []
            for row in reader:
                close = safe_float(row.get("Close"))
                if close is None:
                    continue
                rows.append({
                    "date": row.get("Date"),
                    "open": safe_float(row.get("Open")),
                    "high": safe_float(row.get("High")),
                    "low": safe_float(row.get("Low")),
                    "close": close,
                })
            rows = [r for r in rows if r.get("date") and r.get("close") is not None]
            rows.sort(key=lambda r: str(r.get("date")))
            if len(rows) >= 20:
                return rows[-range_days:]
        except Exception as e:
            print(f"[Stooq历史行情失败] {symbol}: {e}")
    return []


def fetch_history_rows(symbol: str, asset_type: str, lookback_days: int) -> Tuple[List[Dict[str, Any]], str, List[str]]:
    """Robust historical data fetcher with multiple providers and aliases.

    黄金量化优先使用 AllTick（如果配置了 key），再用 Yahoo/Stooq 兜底。
    """
    attempts: List[str] = []
    symbol = (symbol or "GC=F").strip()
    asset_type = (asset_type or "gold").strip()
    raw_l = symbol.lower()

    is_gold = asset_type.lower() == "gold" or raw_l in ["gc=f", "gold", "黄金", "xau", "xauusd", "xauusd=x"]
    is_silver = asset_type.lower() in ["silver", "白银"] or raw_l in ["si=f", "silver", "白银", "xag", "xagusd", "xagusd=x"]

    if is_gold:
        attempts.append(f"AllTick:{ALLTICK_GOLD_SYMBOL or 'GOLD'}")
        rows, at_attempts = fetch_alltick_daily_history(ALLTICK_GOLD_SYMBOL or "GOLD", lookback_days)
        if len(rows) >= 20:
            return rows, f"AllTick kline ({ALLTICK_GOLD_SYMBOL or 'GOLD'})", attempts
        if at_attempts:
            attempts.append(f"AllTick failed endpoints={len(at_attempts)} last={at_attempts[-1].get('status_code') or at_attempts[-1].get('error')}")

    if is_silver:
        attempts.append(f"AllTick:{ALLTICK_SILVER_SYMBOL or 'SILVER'}")
        rows, at_attempts = fetch_alltick_daily_history(ALLTICK_SILVER_SYMBOL or "SILVER", lookback_days)
        if len(rows) >= 20:
            return rows, f"AllTick kline ({ALLTICK_SILVER_SYMBOL or 'SILVER'})", attempts
        if at_attempts:
            attempts.append(f"AllTick silver failed endpoints={len(at_attempts)} last={at_attempts[-1].get('status_code') or at_attempts[-1].get('error')}")

    yahoo_candidates: List[str] = []
    if is_gold:
        yahoo_candidates.extend([symbol, "GC=F", "XAUUSD=X", "GLD"])
    elif is_silver:
        yahoo_candidates.extend([symbol, "SI=F", "XAGUSD=X", "SLV"])
    else:
        yahoo_candidates.append(symbol)
    yahoo_candidates = list(dict.fromkeys([x for x in yahoo_candidates if x]))

    for ysym in yahoo_candidates:
        attempts.append(f"Yahoo:{ysym}")
        rows = fetch_yahoo_chart(ysym, lookback_days)
        if len(rows) >= 20:
            return rows, f"Yahoo Finance chart ({ysym})", attempts

    for ssym in stooq_symbol_candidates(symbol, asset_type):
        attempts.append(f"Stooq:{ssym}")
        rows = fetch_stooq_daily(ssym, lookback_days)
        if len(rows) >= 20:
            return rows, f"Stooq CSV ({ssym})", attempts

    return [], "", attempts

def calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) <= period:
        return None
    gains = []
    losses = []
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def max_drawdown(closes: List[float]) -> Optional[float]:
    if not closes:
        return None
    peak = closes[0]
    mdd = 0.0
    for x in closes:
        peak = max(peak, x)
        dd = (x / peak - 1) if peak else 0
        mdd = min(mdd, dd)
    return round(mdd * 100, 2)


def run_quant_analysis(symbol: str = "GC=F", asset_type: str = "gold", lookback_days: int = 180) -> Dict[str, Any]:
    """受控量化分析：抓历史行情并计算收益、波动、均线、RSI、回撤。

    改进点：
    - 黄金默认不只查 GC=F，还会自动尝试 XAUUSD=X、GLD、Stooq xauusd/gld.us。
    - 失败时返回尝试过的数据源，方便定位是网络问题、接口问题还是符号问题。
    - 如果没有近期历史数据，不再用旧新闻/舆情材料冒充量化分析。
    """
    symbol = (symbol or "GC=F").strip()
    asset_type = (asset_type or "gold").strip()
    lookback_days = max(30, min(int(lookback_days or 180), 730))

    rows, data_source, attempts = fetch_history_rows(symbol, asset_type, lookback_days)

    if len(rows) < 20:
        return {
            "status": "error",
            "message": (
                f"没有获取到足够的近期历史行情：{symbol}。"
                "已尝试多个公开数据源，但均未返回足够日线。请检查服务器外网访问，"
                "或改用 GLD / XAUUSD=X / GC=F 再试。"
            ),
            "symbol": symbol,
            "asset_type": asset_type,
            "rows": len(rows),
            "attempted_sources": attempts,
            "note": "量化分析失败时，不应使用旧新闻数字替代历史行情。",
        }

    closes = [float(r["close"]) for r in rows if r.get("close") is not None]
    returns = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes)) if closes[i - 1] != 0]
    last = closes[-1]
    first = closes[0]
    ret_total = (last / first - 1) * 100 if first else None
    vol_ann = statistics.pstdev(returns) * math.sqrt(252) * 100 if len(returns) > 2 else None
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
    rsi14 = calc_rsi(closes, 14)
    mdd = max_drawdown(closes)

    signal_parts: List[str] = []
    if ma20 and last > ma20:
        signal_parts.append("价格在20日均线上方，短线偏强")
    elif ma20:
        signal_parts.append("价格在20日均线下方，短线偏弱")
    if ma60 and ma20:
        signal_parts.append("20日均线高于60日均线，中期趋势偏多" if ma20 > ma60 else "20日均线低于60日均线，中期趋势偏空")
    if rsi14 is not None:
        if rsi14 >= 70:
            signal_parts.append("RSI接近/进入超买区")
        elif rsi14 <= 30:
            signal_parts.append("RSI接近/进入超卖区")
        else:
            signal_parts.append("RSI处于中性区间")

    return {
        "status": "success",
        "symbol": symbol,
        "asset_type": asset_type,
        "source": data_source,
        "attempted_sources": attempts,
        "start_date": rows[0].get("date"),
        "end_date": rows[-1].get("date"),
        "observations": len(rows),
        "last_close": round(last, 4),
        "total_return_pct": round(ret_total, 2) if ret_total is not None else None,
        "annualized_volatility_pct": round(vol_ann, 2) if vol_ann is not None else None,
        "max_drawdown_pct": mdd,
        "ma20": round(ma20, 4) if ma20 else None,
        "ma60": round(ma60, 4) if ma60 else None,
        "rsi14": rsi14,
        "signal_summary": "；".join(signal_parts) if signal_parts else "样本不足，无法形成指标判断",
        "recent_rows": rows[-5:],
        "note": "量化指标只反映历史价格统计，不构成投资建议。",
    }
