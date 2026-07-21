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

from gold_agent.infra.decimal_utils import ZERO, decimal_text, to_decimal
from gold_agent.infra.http import compact_text, safe_float
from gold_agent.ledger.service import (
    estimate_sale,
    format_ledger_summary,
    get_batch_positions,
    save_pending_operation,
)
from gold_agent.market.prices import (
    analyze_price_relationships,
    fetch_alltick_silver_price,
    get_all_gold_prices,
    get_gold_quote,
)
from gold_agent.market.quant import run_quant_analysis
from gold_agent.market.search import analyze_market_context
from gold_agent.memory.service import persist_explicit_preference
from gold_agent.reports.service import create_word_report
from gold_agent.advice.personalized import (
    format_personalized_advice,
    generate_personalized_advice,
)
from gold_agent.user_profile.parser import parse_profile_updates
from gold_agent.user_profile.store import (
    get_investment_profile,
    update_investment_profile,
)
from gold_agent.agent.extensions import execute_extension


def add_gold_transaction(
    user_id: str,
    side: str,
    quantity_grams: Any,
    unit_price_cny: Any,
    fee_cny: Any = 0,
    note: str = "",
    **details: Any,
) -> Tuple[bool, str]:
    """Compatibility adapter: tool calls create drafts and never write directly."""
    save_pending_operation(
        user_id,
        "ledger.add",
        {
            "side": side,
            "quantity_grams": quantity_grams,
            "unit_price_cny": unit_price_cny,
            "fee_cny": fee_cny,
            "note": note,
            **details,
        },
    )
    return True, "已生成记账草稿，请用户回复“确认记账”后执行。"


tools = [
    {
        "type": "function",
        "function": {
            "name": "get_gold_quote",
            "description": (
                "按用户问题精确查询一个黄金价格口径。必须问什么查什么，不得一次返回全部价格。"
                "用户问国内金价/现在金价/上海金，只查 sge_spot；"
                "问周大福/老凤祥等品牌，只查 brand；"
                "问银行金条/积存金，只查 bank_bar；"
                "问回收价，只查 recycle；"
                "问国际金/伦敦金/XAUUSD/COMEX，只查 international。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "quote_type": {
                        "type": "string",
                        "enum": ["sge_spot", "brand", "bank_bar", "recycle", "international"],
                        "description": "要查询的金价口径。",
                    },
                    "brand": {
                        "type": "string",
                        "description": "品牌名，例如 周大福、老凤祥、中国黄金。仅 quote_type=brand 时填写。",
                    },
                    "bank": {
                        "type": "string",
                        "description": "银行名，例如 中国银行、工商银行。仅 quote_type=bank_bar 时填写。",
                    },
                },
                "required": ["quote_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_all_gold_prices",
            "description": "只有当用户明确要求所有金价、全部口径、汇总整理时调用。汇总国际金、上海金、银行金条/积存金、品牌金店、回收价。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_silver_quote",
            "description": "查询白银/银价的 AllTick 实时国际白银报价。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_market_context",
            "description": (
                "根据用户指定平台或范围，对黄金相关新闻、公开网页索引、投资者讨论做舆情提炼。"
                "优先使用国内搜索入口。适用于：小红书舆情、微博舆情、雪球分析、新闻分析、全网分析。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "description": "平台或范围，例如 全网、小红书、微博、雪球、新闻。"},
                    "keyword": {"type": "string", "description": "关键词，例如 黄金 金价。默认黄金 金价。"},
                    "time_range": {"type": "string", "description": "时间范围，例如 1h、24h、7d。默认 24h。"},
                },
                "required": ["platform", "keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_quant_analysis",
            "description": "抓取公开金融历史行情并运行受控量化分析，输出收益率、年化波动、最大回撤、均线、RSI等指标。适用于：跑代码分析、量化分析、技术指标、趋势统计。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "行情代码，例如 GC=F（COMEX黄金期货）、GLD、XAUUSD=X。默认 GC=F。"},
                    "asset_type": {"type": "string", "description": "资产类型，例如 gold、etf、stock、fx。默认 gold。"},
                    "lookback_days": {"type": "integer", "description": "回看天数，30到730之间。默认180。"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_user_preference",
            "description": "当用户明确说记住、以后都这样、纠正机器人行为或表达稳定偏好时，写入长期记忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "preference": {"type": "string", "description": "要记住的用户偏好或纠正"},
                    "category": {"type": "string", "description": "偏好类别，例如 price/search/crawler/report/quant/style"},
                },
                "required": ["preference"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_gold_ledger",
            "description": (
                "管理用户自己的黄金交易账本。用户用任意自然语言描述已经发生的黄金买入、卖出、"
                "记一笔交易、查看持仓盈亏或撤销最近记录时调用。"
                "只有已经发生或用户明确要求入账的交易才能 action=add；"
                "投资建议、假设、计划买卖、询价不能记账。"
                "添加交易时必须可靠提取买卖方向和克重，并提供每克单价或交易总金额；"
                "缺失字段时不要猜测，应先向用户追问。公斤必须换算为克。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "summary", "batches", "estimate_sale", "undo"],
                        "description": "add=新增交易，summary=查看账本和盈亏，undo=撤销最近一笔。",
                    },
                    "side": {
                        "type": "string",
                        "enum": ["buy", "sell"],
                        "description": "交易方向，仅 action=add 时填写。",
                    },
                    "quantity_grams": {
                        "type": "number",
                        "description": "交易黄金克重，统一换算为克，仅 action=add 时填写。",
                    },
                    "unit_price_cny": {
                        "type": "number",
                        "description": "每克成交价格，单位元/克。与 total_amount_cny 至少提供一个。",
                    },
                    "total_amount_cny": {
                        "type": "number",
                        "description": "交易总金额，单位元。缺少每克价格时用于除以克重计算单价。",
                    },
                    "fee_cny": {
                        "type": "number",
                        "description": "手续费、工费等交易费用，单位元；未提及则为 0。",
                    },
                    "account_type": {
                        "type": "string",
                        "description": "账户类型，如银行积存金、银行金条、金店实物、黄金ETF。",
                    },
                    "channel": {
                        "type": "string",
                        "description": "购买或卖出渠道。",
                    },
                    "spread_cny": {"type": "number", "description": "本次点差成本，元。"},
                    "processing_fee_cny": {"type": "number", "description": "加工费，元。"},
                    "tax_cny": {"type": "number", "description": "税费，元。"},
                    "delivery_fee_cny": {"type": "number", "description": "交付或物流费，元。"},
                    "storage_fee_cny": {"type": "number", "description": "仓储费，元。"},
                    "purity": {"type": "string", "description": "黄金纯度，如 999.9。"},
                    "batch_id": {"type": "string", "description": "买入批次编号；不填则自动生成。"},
                    "certificate_no": {"type": "string", "description": "证书编号。"},
                    "cost_method": {
                        "type": "string",
                        "enum": ["moving_average", "fifo", "specific_batch"],
                        "description": "卖出成本法，默认 moving_average。",
                    },
                    "target_batch_id": {
                        "type": "string",
                        "description": "cost_method=specific_batch 时必填。",
                    },
                    "market_price_cny": {
                        "type": "number",
                        "description": "查看批次浮动盈亏时使用的当前每克价格。",
                    },
                    "note": {
                        "type": "string",
                        "description": "品牌、渠道、品种等备注，只记录用户明确提供的信息。",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_price_relationships",
            "description": "对比上金所、国际折算、银行金条、品牌金店和回收价格，计算溢价或折价并提示异常价差。",
            "parameters": {
                "type": "object",
                "properties": {
                    "brand": {"type": "string", "description": "可选品牌名"},
                    "bank": {"type": "string", "description": "可选银行名"},
                    "usd_cny_rate": {"type": "number", "description": "可选 USD/CNY 汇率"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_investment_profile",
            "description": "查看或更新用户的黄金投资档案，包括目标仓位、风险等级、投资目标、单笔上限、回撤容忍、偏好渠道、报告风格和默认成本法。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["show", "update", "parse_text"],
                    },
                    "text": {
                        "type": "string",
                        "description": "action=parse_text 时传入用户原始配置语句。",
                    },
                    "updates": {
                        "type": "object",
                        "properties": {
                            "investment_goal": {"type": "string", "enum": ["preservation", "long_term", "short_term", "hedging", "physical_collection"]},
                            "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
                            "target_gold_allocation": {"type": "number", "description": "0 到 1。"},
                            "max_single_buy_amount_cny": {"type": "number"},
                            "max_drawdown_tolerance": {"type": "number", "description": "0 到 1。"},
                            "preferred_channels": {"type": "array", "items": {"type": "string"}},
                            "report_style": {"type": "string", "enum": ["concise", "detailed", "data_focused"]},
                            "default_cost_method": {"type": "string", "enum": ["moving_average", "fifo", "specific_batch"]},
                            "allow_suggestion": {"type": "boolean"},
                            "total_assets_cny": {"type": "number"},
                        },
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_personalized_advice",
            "description": "根据用户投资档案和账本生成个性化黄金分析、仓位观察与风险提示。",
            "parameters": {
                "type": "object",
                "properties": {
                    "current_gold_price_cny": {
                        "type": "number",
                        "description": "可选当前黄金价格，元/克。",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extended_gold_assistant",
            "description": "处理策略提醒、分批计划、再平衡、宏观面板、周月复盘、回测、实物黄金和帮助。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "help", "classify_intent", "create_strategy_alert",
                            "build_buy_plan", "rebalance", "macro_panel",
                            "macro_events", "weekly_review", "monthly_review",
                            "backtest", "physical_add", "physical_list",
                            "physical_value", "physical_delete"
                        ]
                    },
                    "payload": {"type": "object"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_word_report",
            "description": "只有当用户明确要求生成 Word、导出报告、写成文件时才调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "report_title": {"type": "string", "description": "报告标题。"},
                    "report_content": {"type": "string", "description": "报告正文。"},
                },
                "required": ["report_title", "report_content"],
            },
        },
    },
]


def call_tool(func_name: str, args: Dict[str, Any]) -> str:
    try:
        if func_name == "get_gold_quote":
            result = get_gold_quote(
                quote_type=args.get("quote_type", "sge_spot"),
                brand=args.get("brand", ""),
                bank=args.get("bank", ""),
            )
            return json.dumps(result, ensure_ascii=False)

        if func_name == "get_all_gold_prices":
            return json.dumps(get_all_gold_prices(), ensure_ascii=False)

        if func_name == "get_silver_quote":
            return json.dumps(fetch_alltick_silver_price(), ensure_ascii=False)

        if func_name == "analyze_market_context":
            result = analyze_market_context(
                platform=args.get("platform", "全网"),
                keyword=args.get("keyword", "黄金 金价"),
                time_range=args.get("time_range", "24h"),
            )
            return json.dumps(result, ensure_ascii=False)

        if func_name == "run_quant_analysis":
            result = run_quant_analysis(
                symbol=args.get("symbol", "GC=F"),
                asset_type=args.get("asset_type", "gold"),
                lookback_days=int(args.get("lookback_days", 180) or 180),
            )
            return json.dumps(result, ensure_ascii=False)

        if func_name == "analyze_price_relationships":
            result = analyze_price_relationships(
                brand=args.get("brand", ""),
                bank=args.get("bank", ""),
                usd_cny_rate=args.get("usd_cny_rate"),
            )
            return json.dumps(result, ensure_ascii=False)

        if func_name == "remember_user_preference":
            pref = args.get("preference", "")
            user_id = getattr(threading.current_thread(), "current_user_id", "")
            if not user_id:
                return json.dumps({"status": "error", "message": "当前线程缺少 user_id，无法持久化偏好"}, ensure_ascii=False)
            persist_explicit_preference(user_id, pref, args.get("category", "general"))
            return json.dumps({"status": "success", "message": "偏好已持久化到 SQLite", "preference": pref}, ensure_ascii=False)

        if func_name == "manage_gold_ledger":
            user_id = getattr(threading.current_thread(), "current_user_id", "")
            if not user_id:
                return json.dumps({"status": "error", "message": "当前线程缺少 user_id，无法操作账本"}, ensure_ascii=False)
            action = str(args.get("action", "")).strip().lower()
            if action == "summary":
                return json.dumps({"status": "success", "message": format_ledger_summary(user_id)}, ensure_ascii=False)
            if action == "batches":
                lots = get_batch_positions(user_id, args.get("market_price_cny"))
                return json.dumps(
                    {"status": "success", "batches": lots},
                    ensure_ascii=False,
                    default=str,
                )
            if action == "estimate_sale":
                try:
                    result = estimate_sale(
                        user_id=user_id,
                        quantity_grams=args.get("quantity_grams"),
                        unit_price_cny=args.get("unit_price_cny"),
                        cost_method=args.get(
                            "cost_method",
                            get_investment_profile(user_id).default_cost_method,
                        ),
                        target_batch_id=args.get("target_batch_id", ""),
                        fee_cny=args.get("fee_cny", 0),
                        spread_cny=args.get("spread_cny", 0),
                        processing_fee_cny=args.get("processing_fee_cny", 0),
                        tax_cny=args.get("tax_cny", 0),
                        delivery_fee_cny=args.get("delivery_fee_cny", 0),
                        storage_fee_cny=args.get("storage_fee_cny", 0),
                    )
                except ValueError as exc:
                    return json.dumps(
                        {"status": "error", "message": str(exc)},
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {"status": "success", "estimate": result},
                    ensure_ascii=False,
                    default=str,
                )
            if action == "undo":
                save_pending_operation(user_id, "ledger.undo", {})
                return json.dumps(
                    {
                        "status": "pending_confirmation",
                        "message": "已生成撤销草稿，请用户回复“确认操作”后执行。",
                    },
                    ensure_ascii=False,
                )
            if action != "add":
                return json.dumps({"status": "error", "message": "未知账本操作"}, ensure_ascii=False)

            side = str(args.get("side", "")).strip().lower()
            default_cost_method = get_investment_profile(
                user_id
            ).default_cost_method
            quantity = to_decimal(args.get("quantity_grams"))
            unit_price = to_decimal(args.get("unit_price_cny"))
            total_amount = to_decimal(args.get("total_amount_cny"))
            fee = to_decimal(args.get("fee_cny"), ZERO) or ZERO
            if side not in ["buy", "sell"]:
                return json.dumps({"status": "error", "message": "缺少明确的买入或卖出方向，请向用户追问"}, ensure_ascii=False)
            if quantity is None or quantity <= 0:
                return json.dumps({"status": "error", "message": "缺少有效黄金克重，请向用户追问"}, ensure_ascii=False)
            if unit_price is None and total_amount is not None and total_amount > 0:
                unit_price = total_amount / quantity
            if unit_price is None or unit_price <= 0:
                return json.dumps({"status": "error", "message": "缺少每克价格或交易总金额，请向用户追问"}, ensure_ascii=False)

            note_parts = []
            note = compact_text(str(args.get("note", "") or ""), 200)
            if note:
                note_parts.append(note)
            if total_amount is not None:
                note_parts.append(f"总金额 {total_amount:.2f} 元")
            ok, message = add_gold_transaction(
                user_id=user_id,
                side=side,
                quantity_grams=quantity,
                unit_price_cny=unit_price,
                fee_cny=fee,
                account_type=args.get("account_type", ""),
                channel=args.get("channel", ""),
                spread_cny=args.get("spread_cny", 0),
                processing_fee_cny=args.get("processing_fee_cny", 0),
                tax_cny=args.get("tax_cny", 0),
                delivery_fee_cny=args.get("delivery_fee_cny", 0),
                storage_fee_cny=args.get("storage_fee_cny", 0),
                purity=args.get("purity", ""),
                batch_id=args.get("batch_id", ""),
                certificate_no=args.get("certificate_no", ""),
                cost_method=args.get("cost_method", default_cost_method),
                target_batch_id=args.get("target_batch_id", ""),
                note="；".join(note_parts),
            )
            if not ok:
                return json.dumps({"status": "error", "message": message}, ensure_ascii=False)
            return json.dumps(
                {
                    "status": "pending_confirmation",
                    "message": message,
                    "record": {
                        "side": side,
                        "quantity_grams": decimal_text(quantity),
                        "unit_price_cny": decimal_text(unit_price),
                        "total_amount_cny": decimal_text(total_amount) if total_amount is not None else None,
                        "fee_cny": decimal_text(fee),
                        "note": note,
                    },
                },
                ensure_ascii=False,
            )

        if func_name == "manage_investment_profile":
            user_id = getattr(threading.current_thread(), "current_user_id", "")
            if not user_id:
                return json.dumps(
                    {"status": "error", "message": "当前线程缺少 user_id"},
                    ensure_ascii=False,
                )
            action = str(args.get("action", "")).strip().lower()
            if action == "show":
                profile = get_investment_profile(user_id)
                return json.dumps(
                    {"status": "success", "profile": profile.to_dict()},
                    ensure_ascii=False,
                    default=str,
                )
            if action == "parse_text":
                updates = parse_profile_updates(str(args.get("text", "") or ""))
            elif action == "update":
                updates = args.get("updates") or {}
            else:
                return json.dumps(
                    {"status": "error", "message": "未知档案操作"},
                    ensure_ascii=False,
                )
            if not isinstance(updates, dict) or not updates:
                return json.dumps(
                    {"status": "error", "message": "没有识别到可更新的投资档案字段"},
                    ensure_ascii=False,
                )
            profile = update_investment_profile(user_id, updates)
            return json.dumps(
                {
                    "status": "success",
                    "message": "投资档案已更新",
                    "updated_fields": sorted(updates),
                    "profile": profile.to_dict(),
                },
                ensure_ascii=False,
                default=str,
            )

        if func_name == "get_personalized_advice":
            user_id = getattr(threading.current_thread(), "current_user_id", "")
            if not user_id:
                return json.dumps(
                    {"status": "error", "message": "当前线程缺少 user_id"},
                    ensure_ascii=False,
                )
            result = generate_personalized_advice(
                user_id, args.get("current_gold_price_cny")
            )
            result["message"] = format_personalized_advice(result)
            return json.dumps(result, ensure_ascii=False, default=str)

        if func_name == "extended_gold_assistant":
            user_id = getattr(threading.current_thread(), "current_user_id", "")
            if not user_id:
                return json.dumps(
                    {"status": "error", "message": "当前线程缺少 user_id"},
                    ensure_ascii=False,
                )
            result = execute_extension(
                user_id,
                str(args.get("action", "")),
                dict(args.get("payload") or {}),
            )
            return json.dumps(result, ensure_ascii=False, default=str)

        if func_name == "create_word_report":
            # 兼容模型偶尔把参数写成 title/content 的情况。
            return create_word_report(
                report_title=args.get("report_title") or args.get("title") or "黄金市场分析报告",
                report_content=args.get("report_content") or args.get("content") or "",
            )

        return json.dumps({"status": "error", "message": f"未知工具：{func_name}"}, ensure_ascii=False)
    except Exception as e:
        traceback.print_exc()
        return json.dumps({"status": "error", "tool": func_name, "message": str(e)}, ensure_ascii=False)
