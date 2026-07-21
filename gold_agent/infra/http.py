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

from gold_agent.config import SEARCH_PROXY

def common_headers(referer: str = "") -> Dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def get_search_proxies() -> Optional[Dict[str, str]]:
    if not SEARCH_PROXY:
        return None
    return {"http": SEARCH_PROXY, "https": SEARCH_PROXY}


def http_get(
    url: str,
    params: Optional[dict] = None,
    timeout: int = 10,
    referer: str = "",
    use_search_proxy: bool = False,
) -> requests.Response:
    return requests.get(
        url,
        params=params,
        headers=common_headers(referer=referer),
        timeout=timeout,
        proxies=get_search_proxies() if use_search_proxy else None,
    )


def http_post(
    url: str,
    data: Optional[dict] = None,
    json_body: Optional[dict] = None,
    timeout: int = 10,
    referer: str = "",
    use_search_proxy: bool = False,
) -> requests.Response:
    return requests.post(
        url,
        data=data,
        json=json_body,
        headers=common_headers(referer=referer),
        timeout=timeout,
        proxies=get_search_proxies() if use_search_proxy else None,
    )


def now_ts() -> int:
    return int(time.time())


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        s = str(value).strip().replace(",", "")
        if not s or s in ["-", "--", "null", "None"]:
            return None
        return float(s)
    except Exception:
        return None


def compact_text(text: str, max_len: int = 4000) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:max_len]


def json_loads_safe(text: str, default: Any) -> Any:
    try:
        if not text:
            return default
        return json.loads(text)
    except Exception:
        return default
