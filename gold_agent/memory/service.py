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

from gold_agent.infra.database import c, conn, db_lock
from gold_agent.infra.http import compact_text, json_loads_safe, now_ts
from gold_agent.security.audit import record_audit

DEFAULT_USER_PREFERENCES: Dict[str, Any] = {
    "gold_quote_style": "only_answer_requested_quote_type",
    "do_not_mix_price_types": True,
    "do_not_derive_domestic_from_international": True,
    "platform_search_style": "use_user_specified_site_or_engine_first",
    "crawler_required_for_sentiment": True,
    "evidence_required": True,
    "word_report_when_requested": True,
    "quant_analysis_allowed": True,
    "reply_style": "short_direct_professional",
}


def ensure_memory_schema() -> None:
    """Create or migrate long-term memory tables.

    user_profiles stores stable user preferences and a short summary.
    interaction_logs stores lightweight conversation history for later review.
    """
    with db_lock:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                preferences_json TEXT DEFAULT '{}',
                facts_json TEXT DEFAULT '{}',
                summary TEXT DEFAULT '',
                updated_at INTEGER DEFAULT 0
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS interaction_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                user_text TEXT,
                assistant_text TEXT,
                created_at INTEGER DEFAULT 0
            )
            """
        )
        conn.commit()


def get_user_profile(user_id: str) -> Dict[str, Any]:
    ensure_memory_schema()
    with db_lock:
        c.execute(
            "SELECT preferences_json, facts_json, summary, updated_at FROM user_profiles WHERE user_id = ?",
            (user_id,),
        )
        row = c.fetchone()
        if not row:
            prefs = dict(DEFAULT_USER_PREFERENCES)
            c.execute(
                "INSERT OR REPLACE INTO user_profiles (user_id, preferences_json, facts_json, summary, updated_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, json.dumps(prefs, ensure_ascii=False), "{}", "", now_ts()),
            )
            conn.commit()
            return {"preferences": prefs, "facts": {}, "summary": "", "updated_at": now_ts()}

        prefs = dict(DEFAULT_USER_PREFERENCES)
        prefs.update(json_loads_safe(row[0], {}))
        return {
            "preferences": prefs,
            "facts": json_loads_safe(row[1], {}),
            "summary": row[2] or "",
            "updated_at": row[3] or 0,
        }


def save_user_profile(user_id: str, profile: Dict[str, Any]) -> None:
    ensure_memory_schema()
    with db_lock:
        c.execute(
            "INSERT OR REPLACE INTO user_profiles (user_id, preferences_json, facts_json, summary, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                user_id,
                json.dumps(profile.get("preferences", {}), ensure_ascii=False),
                json.dumps(profile.get("facts", {}), ensure_ascii=False),
                profile.get("summary", ""),
                now_ts(),
            ),
        )
        conn.commit()


def build_memory_prompt(user_id: str) -> str:
    profile = get_user_profile(user_id)
    prefs = profile.get("preferences", {})
    summary = compact_text(profile.get("summary", ""), 1200)
    investment_profile_text = "not configured"
    try:
        from gold_agent.user_profile.store import get_investment_profile

        investment_profile_text = json.dumps(
            get_investment_profile(user_id).to_dict(),
            ensure_ascii=False,
            default=str,
        )
    except Exception:
        pass
    summary = compact_text(
        f"{summary}\nStructured investment profile: {investment_profile_text}", 2400
    )
    return (
        "\n\n【长期记忆 / 用户偏好】\n"
        f"- 偏好 JSON：{json.dumps(prefs, ensure_ascii=False)}\n"
        f"- 历史摘要：{summary or '暂无'}\n"
        "执行要求：这些偏好优先级高于普通对话上下文；除非用户明确要求改变，否则持续遵守。"
    )


def persist_explicit_preference(user_id: str, preference: str, category: str = "general") -> None:
    preference = compact_text(preference, 500)
    category = compact_text(category or "general", 50)
    if not preference:
        return
    profile = get_user_profile(user_id)
    prefs = profile.setdefault("preferences", dict(DEFAULT_USER_PREFERENCES))
    custom = prefs.setdefault("custom_preferences", [])
    item = {"category": category, "preference": preference}
    if item not in custom:
        custom.append(item)
        prefs["custom_preferences"] = custom[-30:]
    old_summary = profile.get("summary", "")
    event = f"{time.strftime('%Y-%m-%d %H:%M')} 明确偏好[{category}]：{preference}"
    profile["summary"] = (old_summary + "\n" + event).strip()[-1800:]
    save_user_profile(user_id, profile)
    record_audit(
        user_id,
        "user.preference.update",
        "user_profile",
        user_id,
        {"category": category},
    )


def update_user_memory_from_text(
    user_id: str,
    user_text: str,
    assistant_text: str = "",
    log_interaction: bool = True,
) -> None:
    """Deterministically learn stable preferences from user corrections.

    This avoids relying on a model call for memory and survives process restarts.
    """
    profile = get_user_profile(user_id)
    prefs = profile.setdefault("preferences", dict(DEFAULT_USER_PREFERENCES))
    facts = profile.setdefault("facts", {})
    text = user_text or ""

    changed = False
    if any(k in text for k in ["不要每次都返回一大堆", "不要返回一大堆", "问什么查什么", "不用一开始把", "只查我问的"]):
        prefs["gold_quote_style"] = "only_answer_requested_quote_type"
        prefs["do_not_mix_price_types"] = True
        changed = True

    if any(k in text for k in ["不要折算", "国内的不对", "不要用国际", "别用国际金价折算"]):
        prefs["do_not_derive_domestic_from_international"] = True
        changed = True

    if any(k in text for k in ["我问什么网站", "去什么网站搜", "百度就", "只去那个网站", "用户说哪个网站"]):
        prefs["platform_search_style"] = "use_user_specified_site_or_engine_first"
        changed = True

    if any(k in text for k in ["必须爬虫", "能不能爬虫", "打开网页", "二次打开", "抓正文", "真爬虫"]):
        prefs["crawler_required_for_sentiment"] = True
        prefs["evidence_required"] = True
        changed = True

    if any(k in text for k in ["不能编", "不准编", "搜得不准", "证据", "来源", "引用"]):
        prefs["evidence_required"] = True
        changed = True

    if any(k in text for k in ["word", "Word", "报告", "发给我", "发不出去"]):
        prefs["word_report_when_requested"] = True
        changed = True

    if any(k in text for k in ["跑代码", "量化", "金融分析接口", "技术指标", "回测"]):
        prefs["quant_analysis_allowed"] = True
        changed = True

    if any(k in text for k in ["记住", "以后", "长期记忆", "越来越聪明"]):
        facts["memory_requested"] = True
        changed = True

    if changed:
        old_summary = profile.get("summary", "")
        event = compact_text(text, 220)
        summary = (old_summary + "\n" + f"{time.strftime('%Y-%m-%d %H:%M')} 用户偏好/纠正：{event}").strip()
        profile["summary"] = summary[-1800:]
        save_user_profile(user_id, profile)

    if not log_interaction:
        return

    # Log all interactions lightly; keep table from growing without bound.
    try:
        with db_lock:
            c.execute(
                "INSERT INTO interaction_logs (user_id, user_text, assistant_text, created_at) VALUES (?, ?, ?, ?)",
                (user_id, compact_text(user_text, 1200), compact_text(assistant_text, 1200), now_ts()),
            )
            c.execute(
                "DELETE FROM interaction_logs WHERE id NOT IN (SELECT id FROM interaction_logs WHERE user_id = ? ORDER BY id DESC LIMIT 200) AND user_id = ?",
                (user_id, user_id),
            )
            conn.commit()
    except Exception as e:
        print(f"[长期记忆写入失败] {e}")
