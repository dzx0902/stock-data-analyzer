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

from gold_agent.config import APP_TIMEZONE
from gold_agent.infra.database import c, conn, db_lock
from gold_agent.infra.decimal_utils import (
    ZERO,
    decimal_text,
    quantize_grams,
    quantize_money,
    quantize_price,
    to_decimal,
)
from gold_agent.infra.http import compact_text, json_loads_safe, now_ts, safe_float
from gold_agent.market.prices import _fmt_price_line, get_gold_quote
from gold_agent.security.audit import record_audit

def ensure_alerts_schema() -> None:
    """Create or migrate the alert table.

    SQLite's CREATE TABLE IF NOT EXISTS will not add new columns to an
    existing table. Older versions of this bot created alerts without
    created_at, so we explicitly migrate missing columns on startup.
    """
    with db_lock:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                condition_type TEXT,
                target_price REAL,
                gold_type TEXT,
                is_triggered INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT 0
            )
            """
        )

        c.execute("PRAGMA table_info(alerts)")
        existing_columns = {row[1] for row in c.fetchall()}

        migrations = {
            "user_id": "ALTER TABLE alerts ADD COLUMN user_id TEXT",
            "condition_type": "ALTER TABLE alerts ADD COLUMN condition_type TEXT",
            "target_price": "ALTER TABLE alerts ADD COLUMN target_price REAL",
            "gold_type": "ALTER TABLE alerts ADD COLUMN gold_type TEXT",
            "is_triggered": "ALTER TABLE alerts ADD COLUMN is_triggered INTEGER DEFAULT 0",
            "created_at": "ALTER TABLE alerts ADD COLUMN created_at INTEGER DEFAULT 0",
        }

        for column_name, ddl in migrations.items():
            if column_name not in existing_columns:
                print(f"[DB迁移] alerts 表缺少 {column_name}，正在补列")
                c.execute(ddl)

        # 老数据如果 created_at 为空，补一个当前时间，避免后续排序/展示异常。
        c.execute("UPDATE alerts SET created_at = ? WHERE created_at IS NULL OR created_at = 0", (now_ts(),))
        conn.commit()




def ensure_operational_schema() -> None:
    with db_lock:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS gold_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity_grams REAL NOT NULL,
                unit_price_cny REAL NOT NULL,
                fee_cny REAL DEFAULT 0,
                note TEXT DEFAULT '',
                trade_time INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS report_subscriptions (
                user_id TEXT PRIMARY KEY,
                enabled INTEGER DEFAULT 1,
                morning_time TEXT DEFAULT '08:30',
                noon_time TEXT DEFAULT '12:30',
                evening_time TEXT DEFAULT '20:30',
                timezone TEXT DEFAULT 'Asia/Shanghai',
                last_morning_date TEXT DEFAULT '',
                last_noon_date TEXT DEFAULT '',
                last_evening_date TEXT DEFAULT '',
                updated_at INTEGER DEFAULT 0
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id TEXT PRIMARY KEY,
                processed_at INTEGER NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_ledger_entries (
                user_id TEXT PRIMARY KEY,
                source_message_id TEXT DEFAULT '',
                payload_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_gold_transactions_user_time ON gold_transactions(user_id, trade_time, id)")
        c.execute("DELETE FROM processed_messages WHERE processed_at < ?", (now_ts() - 7 * 86400,))
        conn.commit()




def get_ledger_state(user_id: str) -> Dict[str, Any]:
    with db_lock:
        c.execute(
            """
            SELECT id, side, quantity_grams, unit_price_cny, fee_cny, note, trade_time
            FROM gold_transactions
            WHERE user_id = ?
            ORDER BY trade_time, id
            """,
            (user_id,),
        )
        rows = c.fetchall()

    quantity = 0.0
    average_cost = 0.0
    realized_pnl = 0.0
    normalized_rows: List[Dict[str, Any]] = []
    for row_id, side, qty, price, fee, note, trade_time in rows:
        qty = float(qty)
        price = float(price)
        fee = float(fee or 0)
        if side == "buy":
            total_cost = quantity * average_cost + qty * price + fee
            quantity += qty
            average_cost = total_cost / quantity if quantity else 0.0
        elif side == "sell":
            sold_qty = min(qty, quantity)
            realized_pnl += sold_qty * (price - average_cost) - fee
            quantity -= sold_qty
            if quantity <= 1e-9:
                quantity = 0.0
                average_cost = 0.0
        normalized_rows.append(
            {
                "id": row_id,
                "side": side,
                "quantity_grams": qty,
                "unit_price_cny": price,
                "fee_cny": fee,
                "note": note or "",
                "trade_time": trade_time,
            }
        )
    return {
        "quantity_grams": quantity,
        "average_cost_cny_per_gram": average_cost,
        "realized_pnl_cny": realized_pnl,
        "transactions": normalized_rows,
    }


def add_gold_transaction(
    user_id: str,
    side: str,
    quantity_grams: float,
    unit_price_cny: float,
    fee_cny: float = 0,
    note: str = "",
) -> Tuple[bool, str]:
    side = side.lower()
    if side not in ["buy", "sell"]:
        return False, "交易方向必须是买入或卖出"
    if quantity_grams <= 0 or unit_price_cny <= 0 or fee_cny < 0:
        return False, "克重、单价必须大于 0，费用不能为负数"
    if side == "sell":
        holding = get_ledger_state(user_id)["quantity_grams"]
        if quantity_grams > holding + 1e-9:
            return False, f"卖出克重超过当前持仓，当前持仓 {holding:.3f} 克"

    with db_lock:
        ts = now_ts()
        c.execute(
            """
            INSERT INTO gold_transactions
                (user_id, side, quantity_grams, unit_price_cny, fee_cny, note, trade_time, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, side, quantity_grams, unit_price_cny, fee_cny, compact_text(note, 200), ts, ts),
        )
        conn.commit()
    return True, "已记账"


def add_gold_transactions(user_id: str, transactions: List[Dict[str, Any]]) -> Tuple[bool, str]:
    if not transactions:
        return False, "没有可入账的交易"

    normalized: List[Dict[str, Any]] = []
    state = get_ledger_state(user_id)
    available_quantity = float(state["quantity_grams"])

    for index, item in enumerate(transactions, start=1):
        side = str(item.get("side", "")).lower()
        quantity = safe_float(item.get("quantity_grams"))
        unit_price = safe_float(item.get("unit_price_cny"))
        fee = safe_float(item.get("fee_cny"))
        fee = 0.0 if fee is None else fee
        if side not in ["buy", "sell"]:
            return False, f"第 {index} 笔交易方向无效"
        if quantity is None or quantity <= 0 or unit_price is None or unit_price <= 0 or fee < 0:
            return False, f"第 {index} 笔交易的克重、价格或费用无效"
        if side == "buy":
            available_quantity += quantity
        elif quantity > available_quantity + 1e-9:
            return False, f"第 {index} 笔卖出克重超过当时可用持仓"
        else:
            available_quantity -= quantity
        normalized.append(
            {
                "side": side,
                "quantity": quantity,
                "unit_price": unit_price,
                "fee": fee,
                "note": compact_text(str(item.get("note", "") or ""), 200),
            }
        )

    with db_lock:
        try:
            ts = now_ts()
            c.executemany(
                """
                INSERT INTO gold_transactions
                    (user_id, side, quantity_grams, unit_price_cny, fee_cny, note, trade_time, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        user_id,
                        item["side"],
                        item["quantity"],
                        item["unit_price"],
                        item["fee"],
                        item["note"],
                        ts,
                        ts,
                    )
                    for item in normalized
                ],
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            return False, f"批量入账失败：{exc}"
    return True, f"已批量记账 {len(normalized)} 笔"


def undo_last_gold_transaction(user_id: str) -> str:
    with db_lock:
        c.execute("SELECT id FROM gold_transactions WHERE user_id = ? ORDER BY trade_time DESC, id DESC LIMIT 1", (user_id,))
        row = c.fetchone()
        if not row:
            return "账本中没有可以撤销的记录。"
        c.execute("DELETE FROM gold_transactions WHERE id = ? AND user_id = ?", (row[0], user_id))
        conn.commit()
    return f"已撤销最近一笔记录（ID {row[0]}）。"


def format_ledger_summary(user_id: str, include_recent: bool = True) -> str:
    state = get_ledger_state(user_id)
    quantity = state["quantity_grams"]
    average_cost = state["average_cost_cny_per_gram"]
    realized = state["realized_pnl_cny"]
    quote = get_gold_quote("sge_spot")
    lines = ["【黄金账本】"]
    if quantity > 0:
        lines.append(f"当前持仓：{quantity:.3f} 克")
        lines.append(f"持仓均价：{average_cost:.2f} 元/克（已计买入费用）")
        if quote.get("status") == "success":
            current = float(quote["price"])
            market_value = quantity * current
            unrealized = quantity * (current - average_cost)
            total_pnl = realized + unrealized
            lines.append(f"参考现价：{current:.2f} 元/克")
            lines.append(f"参考市值：{market_value:.2f} 元")
            lines.append(f"浮动盈亏：{unrealized:+.2f} 元")
            lines.append(f"累计盈亏：{total_pnl:+.2f} 元（已实现 {realized:+.2f} 元）")
            lines.append(f"行情时间：{quote.get('timestamp')}，{(quote.get('extra') or {}).get('market_status', '')}")
        else:
            lines.append(f"已实现盈亏：{realized:+.2f} 元")
            lines.append("当前行情获取失败，暂时无法计算浮动盈亏。")
    else:
        lines.append("当前持仓：0 克")
        lines.append(f"累计已实现盈亏：{realized:+.2f} 元")

    if include_recent:
        recent = state["transactions"][-8:]
        if recent:
            lines.append("")
            lines.append("最近记录：")
            for item in reversed(recent):
                side = "买入" if item["side"] == "buy" else "卖出"
                dt = datetime.fromtimestamp(item["trade_time"]).strftime("%Y-%m-%d %H:%M")
                lines.append(
                    f"- #{item['id']} {dt} {side} {item['quantity_grams']:.3f} 克，"
                    f"{item['unit_price_cny']:.2f} 元/克，费用 {item['fee_cny']:.2f} 元"
                )
    lines.append("说明：盈亏按移动加权平均成本估算，参考价使用 Au99.99，不等于品牌首饰实际回收价。")
    return "\n".join(lines)


def parse_ledger_request(user_text: str) -> Optional[Dict[str, Any]]:
    text = user_text.strip()
    if any(k in text for k in ["撤销上一笔", "删除上一笔", "撤销最近一笔"]):
        return {"action": "undo"}
    if text in ["账本", "盈亏", "持仓"] or any(
        k in text for k in ["我的账本", "查看账本", "持仓盈亏", "我的盈亏", "收益情况", "持仓情况"]
    ):
        return {"action": "summary"}

    side = "buy" if "买入" in text else "sell" if "卖出" in text else ""
    if not side or not any(k in text for k in ["记账", "记一笔", "买入", "卖出"]):
        return None
    qty_match = re.search(r"(\d+(?:\.\d+)?)\s*(公斤|千克|kg|KG|克|g)", text)
    price_match = re.search(r"(?:单价|价格|价|按)?\s*(\d+(?:\.\d+)?)\s*(?:元\s*/?\s*克|元每克)", text)
    total_match = re.search(r"(?:总价|共计|花了|收到)\s*(\d+(?:\.\d+)?)\s*元", text)
    fee_match = re.search(r"(?:手续费|工费|费用)\s*(\d+(?:\.\d+)?)\s*元", text)
    if not qty_match:
        return None
    quantity = float(qty_match.group(1))
    if qty_match.group(2).lower() in ["公斤", "千克", "kg"]:
        quantity *= 1000
    price = float(price_match.group(1)) if price_match else None
    if price is None and total_match and quantity > 0:
        price = float(total_match.group(1)) / quantity
    if price is None:
        return None
    return {
        "action": "add",
        "side": side,
        "quantity_grams": quantity,
        "unit_price_cny": price,
        "fee_cny": float(fee_match.group(1)) if fee_match else 0.0,
        "note": text,
    }


def save_pending_ledger_entry(user_id: str, message_id: str, payload: Dict[str, Any]) -> None:
    with db_lock:
        c.execute(
            """
            INSERT INTO pending_ledger_entries (user_id, source_message_id, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                source_message_id = excluded.source_message_id,
                payload_json = excluded.payload_json,
                created_at = excluded.created_at
            """,
            (user_id, message_id, json.dumps(payload, ensure_ascii=False), now_ts()),
        )
        conn.commit()


def get_pending_ledger_entry(user_id: str) -> Optional[Dict[str, Any]]:
    with db_lock:
        c.execute(
            "SELECT payload_json, created_at FROM pending_ledger_entries WHERE user_id = ?",
            (user_id,),
        )
        row = c.fetchone()
    if not row:
        return None
    if now_ts() - int(row[1]) > 3 * 86400:
        clear_pending_ledger_entry(user_id)
        return None
    payload = json_loads_safe(row[0], {})
    return payload if isinstance(payload, dict) else None


def clear_pending_ledger_entry(user_id: str) -> None:
    with db_lock:
        c.execute("DELETE FROM pending_ledger_entries WHERE user_id = ?", (user_id,))
        conn.commit()


def handle_pending_ledger_confirmation(user_id: str, user_text: str) -> Optional[str]:
    text = (user_text or "").strip()
    if not any(k in text for k in ["确认记账", "确认入账", "就按这个记", "取消记账", "不要记了", "放弃记账"]):
        return None
    pending = get_pending_ledger_entry(user_id)
    if not pending:
        return "当前没有等待确认的截图记账草稿。"
    if any(k in text for k in ["取消", "不要", "放弃"]):
        clear_pending_ledger_entry(user_id)
        return "已取消这笔截图记账草稿。"

    transactions = pending.get("transactions")
    if isinstance(transactions, list):
        ok, message = add_gold_transactions(user_id, transactions)
    else:
        # Backward compatibility for pending drafts created before batch support.
        ok, message = add_gold_transaction(
            user_id=user_id,
            side=str(pending.get("side", "")),
            quantity_grams=float(pending.get("quantity_grams") or 0),
            unit_price_cny=float(pending.get("unit_price_cny") or 0),
            fee_cny=float(pending.get("fee_cny") or 0),
            note=compact_text(str(pending.get("note", "") or ""), 200),
        )
    if not ok:
        return "截图草稿未能入账：" + message
    clear_pending_ledger_entry(user_id)
    return message + "\n\n" + format_ledger_summary(user_id, include_recent=False)


def parse_report_subscription_request(user_text: str) -> Optional[Dict[str, Any]]:
    text = user_text.strip()
    schedule_words = ["定时汇报", "定期汇报", "早报", "午报", "晚报", "早中晚", "每日汇报"]
    if not any(k in text for k in schedule_words) and not ("汇报" in text and any(k in text for k in ["每天", "每日", "定时"])):
        return None
    if any(k in text for k in ["关闭", "取消", "停止", "不要"]):
        return {"action": "disable"}
    if any(k in text for k in ["查看", "几点", "状态"]):
        return {"action": "status"}

    times = re.findall(r"(?<!\d)([01]?\d|2[0-3])[:：点时]([0-5]\d)?", text)
    parsed_times = []
    for hour, minute in times:
        parsed_times.append(f"{int(hour):02d}:{int(minute or 0):02d}")
    defaults = ["08:30", "12:30", "20:30"]
    while len(parsed_times) < 3:
        parsed_times.append(defaults[len(parsed_times)])
    return {"action": "enable", "times": parsed_times[:3]}


def update_report_subscription(user_id: str, request: Dict[str, Any]) -> str:
    action = request["action"]
    with db_lock:
        if action == "disable":
            c.execute(
                """
                INSERT INTO report_subscriptions (user_id, enabled, updated_at)
                VALUES (?, 0, ?)
                ON CONFLICT(user_id) DO UPDATE SET enabled = 0, updated_at = excluded.updated_at
                """,
                (user_id, now_ts()),
            )
            conn.commit()
            return "已关闭每日定时汇报，设置已持久化。"
        if action == "enable":
            morning, noon, evening = request["times"]
            c.execute(
                """
                INSERT INTO report_subscriptions
                    (user_id, enabled, morning_time, noon_time, evening_time, timezone, updated_at)
                VALUES (?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    enabled = 1,
                    morning_time = excluded.morning_time,
                    noon_time = excluded.noon_time,
                    evening_time = excluded.evening_time,
                    timezone = excluded.timezone,
                    updated_at = excluded.updated_at
                """,
                (user_id, morning, noon, evening, APP_TIMEZONE, now_ts()),
            )
            conn.commit()
            return f"已开启每日定时汇报：{morning}、{noon}、{evening}（{APP_TIMEZONE}）。设置重启后仍保留。"

        c.execute(
            "SELECT enabled, morning_time, noon_time, evening_time, timezone FROM report_subscriptions WHERE user_id = ?",
            (user_id,),
        )
        row = c.fetchone()
    if not row:
        return "当前未设置每日定时汇报。"
    return f"定时汇报：{'已开启' if row[0] else '已关闭'}；{row[1]}、{row[2]}、{row[3]}（{row[4]}）。"


def build_scheduled_report(user_id: str, slot_name: str) -> str:
    domestic = get_gold_quote("sge_spot")
    international = get_gold_quote("international")
    labels = {"morning": "早报", "noon": "午报", "evening": "晚报"}
    lines = [f"【黄金市场{labels.get(slot_name, '定时简报')}】", time.strftime("%Y-%m-%d %H:%M:%S")]
    lines.append(_fmt_price_line(domestic, "上海金 Au99.99"))
    lines.append(_fmt_price_line(international, "国际黄金"))
    state = get_ledger_state(user_id)
    if state["quantity_grams"] > 0 and domestic.get("status") == "success":
        current = float(domestic["price"])
        unrealized = state["quantity_grams"] * (current - state["average_cost_cny_per_gram"])
        lines.append(
            f"个人持仓：{state['quantity_grams']:.3f} 克，均价 {state['average_cost_cny_per_gram']:.2f} 元/克，"
            f"浮动盈亏 {unrealized:+.2f} 元"
        )
    with db_lock:
        c.execute("SELECT COUNT(*) FROM alerts WHERE user_id = ? AND is_triggered = 0", (user_id,))
        alert_count = c.fetchone()[0]
    lines.append(f"未触发价格提醒：{alert_count} 条")
    lines.append("不同报价口径不可直接替代；以上为信息汇总，不构成买卖建议。")
    return "\n".join(lines)


def monitor_scheduled_reports() -> None:
    print(f"定时汇报线程已启动，时区={APP_TIMEZONE}")
    slot_columns = [
        ("morning", "morning_time", "last_morning_date"),
        ("noon", "noon_time", "last_noon_date"),
        ("evening", "evening_time", "last_evening_date"),
    ]
    while True:
        try:
            now_local = datetime.now(ZoneInfo(APP_TIMEZONE))
            today = now_local.strftime("%Y-%m-%d")
            current_minutes = now_local.hour * 60 + now_local.minute
            with db_lock:
                c.execute(
                    """
                    SELECT user_id, morning_time, noon_time, evening_time,
                           last_morning_date, last_noon_date, last_evening_date
                    FROM report_subscriptions WHERE enabled = 1
                    """
                )
                subscriptions = c.fetchall()
            for row in subscriptions:
                user_id = row[0]
                values = {
                    "morning_time": row[1],
                    "noon_time": row[2],
                    "evening_time": row[3],
                    "last_morning_date": row[4],
                    "last_noon_date": row[5],
                    "last_evening_date": row[6],
                }
                for slot, time_column, last_column in slot_columns:
                    hour, minute = [int(x) for x in values[time_column].split(":")]
                    target_minutes = hour * 60 + minute
                    if values[last_column] == today or not (0 <= current_minutes - target_minutes < 10):
                        continue
                    from gold_agent.integrations.feishu import send_feishu_message
                    send_feishu_message(user_id, build_scheduled_report(user_id, slot))
                    with db_lock:
                        c.execute(
                            f"UPDATE report_subscriptions SET {last_column} = ?, updated_at = ? WHERE user_id = ?",
                            (today, now_ts(), user_id),
                        )
                        conn.commit()
        except Exception as e:
            print(f"[定时汇报线程异常] {e}")
        time.sleep(30)


def mark_message_processed(message_id: str) -> bool:
    with db_lock:
        c.execute(
            "INSERT OR IGNORE INTO processed_messages (message_id, processed_at) VALUES (?, ?)",
            (message_id, now_ts()),
        )
        inserted = c.rowcount == 1
        conn.commit()
    return inserted


def parse_alert_request(user_text: str) -> Optional[Dict[str, Any]]:
    if not any(kw in user_text for kw in ["提醒我", "检测", "监控", "报警", "警报", "盯着"]):
        return None

    below_match = re.search(r"(?:低于|跌破|小于|低到)\s*(\d+(?:\.\d+)?)\s*(美元|美金|元|块)?", user_text)
    above_match = re.search(r"(?:高于|涨破|突破|大于|涨到)\s*(\d+(?:\.\d+)?)\s*(美元|美金|元|块)?", user_text)

    condition_type = None
    target_price = None
    unit_hint = None
    if below_match:
        condition_type = "below"
        target_price = float(below_match.group(1))
        unit_hint = below_match.group(2)
    elif above_match:
        condition_type = "above"
        target_price = float(above_match.group(1))
        unit_hint = above_match.group(2)

    if not condition_type or target_price is None:
        return None

    if unit_hint in ["元", "块"] or target_price < 1500:
        gold_type = "sge_spot"
    else:
        gold_type = "international"

    return {"condition_type": condition_type, "target_price": target_price, "gold_type": gold_type}


def add_alert(user_id: str, condition_type: str, target_price: float, gold_type: str) -> None:
    with db_lock:
        c.execute(
            """
            INSERT INTO alerts (user_id, condition_type, target_price, gold_type, is_triggered, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (user_id, condition_type, target_price, gold_type, now_ts()),
        )
        conn.commit()


def monitor_alerts() -> None:
    print("黄金价格监控报警线程已启动")
    while True:
        try:
            with db_lock:
                c.execute(
                    """
                    SELECT id, user_id, condition_type, target_price, gold_type
                    FROM alerts
                    WHERE is_triggered = 0
                    """
                )
                active_alerts = c.fetchall()

            for alert_id, uid, cond, target, g_type in active_alerts:
                result = get_gold_quote("international" if g_type == "international" else "sge_spot")
                if result.get("status") != "success":
                    continue

                current_price = result["price"]
                unit = "美元/盎司" if result.get("unit") == "USD/oz" else "元/克"
                name = (result.get("extra") or {}).get("display_name", result.get("symbol", "黄金价格"))

                triggered = (cond == "below" and current_price <= target) or (cond == "above" and current_price >= target)
                if not triggered:
                    continue

                cond_zh = "低于" if cond == "below" else "高于"
                from gold_agent.integrations.feishu import send_feishu_message
                send_feishu_message(
                    uid,
                    (
                        "【黄金价格警报触发】\n\n"
                        f"监控对象：{name}\n"
                        f"监控条件：{cond_zh} {target} {unit}\n"
                        f"当前价格：{current_price} {unit}\n"
                        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                        "请结合自身仓位和风险承受能力判断。"
                    ),
                )
                with db_lock:
                    c.execute("UPDATE alerts SET is_triggered = 1 WHERE id = ?", (alert_id,))
                    conn.commit()

        except Exception as e:
            print(f"[报警线程异常] {e}")

        time.sleep(30)


# Versioned ledger implementation. These definitions intentionally replace the
# legacy float-based functions above while preserving their public API.
def get_ledger_state(user_id: str) -> Dict[str, Any]:
    with db_lock:
        c.execute(
            """
            SELECT id, side, quantity_grams, unit_price_cny, fee_cny, note, trade_time
            FROM ledger_transactions_v2
            WHERE user_id = ?
            ORDER BY trade_time, id
            """,
            (user_id,),
        )
        rows = c.fetchall()

    quantity = ZERO
    average_cost = ZERO
    realized_pnl = ZERO
    normalized_rows: List[Dict[str, Any]] = []
    for row_id, side, qty_raw, price_raw, fee_raw, note, trade_time in rows:
        qty = to_decimal(qty_raw, ZERO) or ZERO
        price = to_decimal(price_raw, ZERO) or ZERO
        fee = to_decimal(fee_raw, ZERO) or ZERO
        if side == "buy":
            total_cost = quantity * average_cost + qty * price + fee
            quantity += qty
            average_cost = total_cost / quantity if quantity else ZERO
        elif side == "sell":
            sold_qty = min(qty, quantity)
            realized_pnl += sold_qty * (price - average_cost) - fee
            quantity -= sold_qty
            if quantity <= ZERO:
                quantity = ZERO
                average_cost = ZERO
        normalized_rows.append(
            {
                "id": row_id,
                "side": side,
                "quantity_grams": quantize_grams(qty),
                "unit_price_cny": quantize_price(price),
                "fee_cny": quantize_money(fee),
                "note": note or "",
                "trade_time": trade_time,
            }
        )
    return {
        "quantity_grams": quantize_grams(quantity),
        "average_cost_cny_per_gram": quantize_price(average_cost),
        "realized_pnl_cny": quantize_money(realized_pnl),
        "transactions": normalized_rows,
    }


def add_gold_transaction(
    user_id: str,
    side: str,
    quantity_grams: Any,
    unit_price_cny: Any,
    fee_cny: Any = 0,
    note: str = "",
) -> Tuple[bool, str]:
    side = side.lower()
    quantity = to_decimal(quantity_grams)
    unit_price = to_decimal(unit_price_cny)
    fee = to_decimal(fee_cny, ZERO) or ZERO
    if side not in {"buy", "sell"}:
        return False, "交易方向必须是买入或卖出"
    if quantity is None or unit_price is None or quantity <= ZERO or unit_price <= ZERO or fee < ZERO:
        return False, "克重、单价必须大于 0，费用不能为负数"
    if side == "sell":
        holding = get_ledger_state(user_id)["quantity_grams"]
        if quantity > holding:
            return False, f"卖出克重超过当前持仓，当前持仓 {holding:.3f} 克"

    with db_lock:
        ts = now_ts()
        c.execute(
            """
            INSERT INTO ledger_transactions_v2
                (user_id, side, quantity_grams, unit_price_cny, fee_cny, note, trade_time, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                side,
                decimal_text(quantize_grams(quantity)),
                decimal_text(quantize_price(unit_price)),
                decimal_text(quantize_money(fee)),
                compact_text(note, 200),
                ts,
                ts,
            ),
        )
        transaction_id = str(c.lastrowid)
        conn.commit()
    record_audit(
        user_id,
        "ledger.transaction.create",
        "ledger_transaction",
        transaction_id,
        {"side": side},
    )
    return True, "已记账"


def add_gold_transactions(user_id: str, transactions: List[Dict[str, Any]]) -> Tuple[bool, str]:
    if not transactions:
        return False, "没有可入账的交易"
    available_quantity = get_ledger_state(user_id)["quantity_grams"]
    normalized: List[Dict[str, Any]] = []
    for index, item in enumerate(transactions, start=1):
        side = str(item.get("side", "")).lower()
        quantity = to_decimal(item.get("quantity_grams"))
        unit_price = to_decimal(item.get("unit_price_cny"))
        fee = to_decimal(item.get("fee_cny"), ZERO) or ZERO
        if side not in {"buy", "sell"}:
            return False, f"第 {index} 笔交易方向无效"
        if quantity is None or unit_price is None or quantity <= ZERO or unit_price <= ZERO or fee < ZERO:
            return False, f"第 {index} 笔交易的克重、价格或费用无效"
        if side == "buy":
            available_quantity += quantity
        elif quantity > available_quantity:
            return False, f"第 {index} 笔卖出克重超过当时可用持仓"
        else:
            available_quantity -= quantity
        normalized.append(
            {
                "side": side,
                "quantity": quantize_grams(quantity),
                "unit_price": quantize_price(unit_price),
                "fee": quantize_money(fee),
                "note": compact_text(str(item.get("note", "") or ""), 200),
            }
        )

    with db_lock:
        try:
            ts = now_ts()
            c.executemany(
                """
                INSERT INTO ledger_transactions_v2
                    (user_id, side, quantity_grams, unit_price_cny, fee_cny, note, trade_time, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        user_id,
                        item["side"],
                        decimal_text(item["quantity"]),
                        decimal_text(item["unit_price"]),
                        decimal_text(item["fee"]),
                        item["note"],
                        ts,
                        ts,
                    )
                    for item in normalized
                ],
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            return False, f"批量入账失败：{exc}"
    record_audit(
        user_id,
        "ledger.transaction.batch_create",
        "ledger_transaction",
        details={"count": len(normalized)},
    )
    return True, f"已批量记账 {len(normalized)} 笔"


def undo_last_gold_transaction(user_id: str) -> str:
    with db_lock:
        c.execute(
            "SELECT id FROM ledger_transactions_v2 "
            "WHERE user_id = ? ORDER BY trade_time DESC, id DESC LIMIT 1",
            (user_id,),
        )
        row = c.fetchone()
        if not row:
            return "账本中没有可以撤销的记录。"
        c.execute("DELETE FROM ledger_transactions_v2 WHERE id = ? AND user_id = ?", (row[0], user_id))
        conn.commit()
    record_audit(user_id, "ledger.transaction.undo", "ledger_transaction", str(row[0]))
    return f"已撤销最近一笔记录（ID {row[0]}）。"


def format_ledger_summary(user_id: str, include_recent: bool = True) -> str:
    state = get_ledger_state(user_id)
    quantity = state["quantity_grams"]
    average_cost = state["average_cost_cny_per_gram"]
    realized = state["realized_pnl_cny"]
    quote = get_gold_quote("sge_spot")
    lines = ["【黄金账本】"]
    if quantity > ZERO:
        lines.append(f"当前持仓：{quantity:.3f} 克")
        lines.append(f"持仓均价：{average_cost:.2f} 元/克（已计买入费用）")
        if quote.get("status") == "success":
            current = to_decimal(quote["price"], ZERO) or ZERO
            market_value = quantize_money(quantity * current)
            unrealized = quantize_money(quantity * (current - average_cost))
            total_pnl = quantize_money(realized + unrealized)
            lines.extend(
                [
                    f"参考现价：{current:.2f} 元/克",
                    f"参考市值：{market_value:.2f} 元",
                    f"浮动盈亏：{unrealized:+.2f} 元",
                    f"累计盈亏：{total_pnl:+.2f} 元（已实现 {realized:+.2f} 元）",
                    f"行情时间：{quote.get('timestamp')}，{(quote.get('extra') or {}).get('market_status', '')}",
                ]
            )
        else:
            lines.extend([f"已实现盈亏：{realized:+.2f} 元", "当前行情获取失败，暂时无法计算浮动盈亏。"])
    else:
        lines.extend(["当前持仓：0 克", f"累计已实现盈亏：{realized:+.2f} 元"])
    if include_recent and state["transactions"]:
        lines.extend(["", "最近记录："])
        for item in reversed(state["transactions"][-8:]):
            side_text = "买入" if item["side"] == "buy" else "卖出"
            dt = datetime.fromtimestamp(item["trade_time"]).strftime("%Y-%m-%d %H:%M")
            lines.append(
                f"- #{item['id']} {dt} {side_text} {item['quantity_grams']:.3f} 克，"
                f"{item['unit_price_cny']:.2f} 元/克，费用 {item['fee_cny']:.2f} 元"
            )
    lines.append("说明：盈亏按移动加权平均成本估算，参考价为 Au99.99，不等于品牌首饰实际回收价。")
    return "\n".join(lines)


def save_pending_operation(
    user_id: str,
    operation_type: str,
    payload: Dict[str, Any],
    message_id: str = "",
    ttl_seconds: int = 3 * 86400,
) -> int:
    with db_lock:
        c.execute(
            "UPDATE pending_operations SET status = 'superseded' "
            "WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        )
        ts = now_ts()
        c.execute(
            """
            INSERT INTO pending_operations
                (user_id, operation_type, payload_json, source_message_id,
                 status, created_at, expires_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                user_id,
                operation_type,
                json.dumps(payload, ensure_ascii=False, default=str),
                message_id,
                ts,
                ts + ttl_seconds,
            ),
        )
        operation_id = int(c.lastrowid)
        conn.commit()
    record_audit(
        user_id,
        "operation.pending.create",
        "pending_operation",
        str(operation_id),
        {"operation_type": operation_type},
    )
    return operation_id


def save_pending_ledger_entry(user_id: str, message_id: str, payload: Dict[str, Any]) -> None:
    operation_type = "ledger.batch_add" if isinstance(payload.get("transactions"), list) else "ledger.add"
    save_pending_operation(user_id, operation_type, payload, message_id)


def get_pending_ledger_entry(user_id: str) -> Optional[Dict[str, Any]]:
    with db_lock:
        c.execute(
            """
            SELECT id, operation_type, payload_json, expires_at
            FROM pending_operations
            WHERE user_id = ? AND status = 'pending'
            ORDER BY id DESC LIMIT 1
            """,
            (user_id,),
        )
        row = c.fetchone()
    if not row:
        return None
    if now_ts() > int(row[3]):
        clear_pending_ledger_entry(user_id)
        return None
    payload = json_loads_safe(row[2], {})
    if not isinstance(payload, dict):
        return None
    payload["_operation_id"] = row[0]
    payload["_operation_type"] = row[1]
    return payload


def clear_pending_ledger_entry(user_id: str) -> None:
    with db_lock:
        c.execute(
            "UPDATE pending_operations SET status = 'cancelled' "
            "WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        )
        conn.commit()


def handle_pending_ledger_confirmation(user_id: str, user_text: str) -> Optional[str]:
    text = (user_text or "").strip()
    confirmation_terms = ["确认记账", "确认入账", "就按这个记", "确认操作"]
    cancellation_terms = ["取消记账", "不要记了", "放弃记账", "取消操作"]
    if not any(term in text for term in confirmation_terms + cancellation_terms):
        return None
    pending = get_pending_ledger_entry(user_id)
    if not pending:
        return "当前没有等待确认的操作。"
    if any(term in text for term in cancellation_terms):
        clear_pending_ledger_entry(user_id)
        return "已取消待确认操作。"

    operation_type = pending.get("_operation_type")
    if operation_type == "physical.delete":
        from gold_agent.physical_gold.service import delete_physical_item

        ok = delete_physical_item(user_id, int(pending.get("item_id", 0)))
        message = "已删除实物黄金记录。" if ok else "没有找到对应实物黄金记录。"
    elif operation_type == "ledger.undo":
        message = undo_last_gold_transaction(user_id)
        ok = not message.startswith("账本中没有")
    elif isinstance(pending.get("transactions"), list):
        ok, message = add_gold_transactions(user_id, pending["transactions"])
    else:
        ok, message = add_gold_transaction(
            user_id=user_id,
            side=str(pending.get("side", "")),
            quantity_grams=pending.get("quantity_grams"),
            unit_price_cny=pending.get("unit_price_cny"),
            fee_cny=pending.get("fee_cny", 0),
            note=compact_text(str(pending.get("note", "") or ""), 200),
            account_type=pending.get("account_type", ""),
            channel=pending.get("channel", ""),
            spread_cny=pending.get("spread_cny", 0),
            processing_fee_cny=pending.get("processing_fee_cny", 0),
            tax_cny=pending.get("tax_cny", 0),
            delivery_fee_cny=pending.get("delivery_fee_cny", 0),
            storage_fee_cny=pending.get("storage_fee_cny", 0),
            purity=pending.get("purity", ""),
            batch_id=pending.get("batch_id", ""),
            certificate_no=pending.get("certificate_no", ""),
            image_refs=pending.get("image_refs", []),
            cost_method=pending.get("cost_method", "moving_average"),
            target_batch_id=pending.get("target_batch_id", ""),
            trade_time=pending.get("trade_time"),
        )
    if not ok:
        return "待确认操作未能执行：" + message
    with db_lock:
        c.execute(
            "UPDATE pending_operations SET status = 'confirmed', confirmed_at = ? WHERE id = ?",
            (now_ts(), pending.get("_operation_id")),
        )
        conn.commit()
    return message + "\n\n" + format_ledger_summary(user_id, include_recent=False)


def mark_message_processed(message_id: str) -> bool:
    with db_lock:
        ts = now_ts()
        c.execute(
            "SELECT status, attempts, updated_at FROM processed_messages WHERE message_id = ?",
            (message_id,),
        )
        row = c.fetchone()
        if row and row[0] == "succeeded":
            return False
        if row and row[0] == "processing" and ts - int(row[2] or 0) < 300:
            return False
        if row:
            c.execute(
                """
                UPDATE processed_messages
                SET status = 'processing', attempts = ?, last_error = '', updated_at = ?
                WHERE message_id = ?
                """,
                (int(row[1]) + 1, ts, message_id),
            )
        else:
            c.execute(
                """
                INSERT INTO processed_messages
                    (message_id, status, attempts, last_error, processed_at, updated_at)
                VALUES (?, 'processing', 1, '', ?, ?)
                """,
                (message_id, ts, ts),
            )
        conn.commit()
    return True


def complete_message_processing(message_id: str) -> None:
    with db_lock:
        c.execute(
            "UPDATE processed_messages SET status = 'succeeded', updated_at = ? WHERE message_id = ?",
            (now_ts(), message_id),
        )
        conn.commit()


def fail_message_processing(message_id: str, error: str) -> None:
    with db_lock:
        c.execute(
            """
            UPDATE processed_messages
            SET status = 'failed', last_error = ?, updated_at = ?
            WHERE message_id = ?
            """,
            (compact_text(error, 500), now_ts(), message_id),
        )
        conn.commit()


# Stage 2 ledger implementation. Keep this import at the end so existing
# integrations retain their public imports while using the enhanced engine.
from gold_agent.ledger.service_v2 import (  # noqa: E402,F401
    add_gold_transaction,
    add_gold_transactions,
    format_ledger_summary,
    get_batch_positions,
    get_ledger_state,
    estimate_sale,
    undo_last_gold_transaction,
)
