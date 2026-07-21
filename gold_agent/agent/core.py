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

from gold_agent.agent.tools import call_tool, tools
from gold_agent.config import BASE_URL_DEEPSEEK, DEEPSEEK_API_KEY, LLM_MODEL_DEEPSEEK
from gold_agent.infra.database import c, db_lock
from gold_agent.infra.http import compact_text
from gold_agent.market.prices import (
    fetch_alltick_silver_price,
    format_all_gold_prices_reply,
    format_gold_quote_reply,
    format_silver_quote_reply,
    get_all_gold_prices,
    get_gold_quote,
)
from gold_agent.memory.service import build_memory_prompt, update_user_memory_from_text
from gold_agent.reports.service import create_word_report
from gold_agent.advice.personalized import (
    format_personalized_advice,
    generate_personalized_advice,
)
from gold_agent.user_profile.parser import parse_profile_updates
from gold_agent.user_profile.store import (
    format_investment_profile,
    get_investment_profile,
    update_investment_profile,
)
from gold_agent.router.service import help_text

SYSTEM_PROMPT = """
你是部署在飞书群里的黄金与大宗商品智能分析 Agent。

核心规则：
1. 金价查询必须按用户问题精确选择单一口径，不得一次返回全部口径。
2. 用户问“现在金价/国内金价/今日金价/上海金/中国金价”，默认只调用 get_gold_quote，quote_type="sge_spot"。
3. 用户问“周大福/老凤祥/中国黄金/周生生/六福/老庙/菜百”等品牌，只调用 quote_type="brand"，并传 brand。
4. 用户问“银行金条/积存金/中国银行/工商银行/建设银行/农行”等，只调用 quote_type="bank_bar"，并尽量传 bank。
5. 用户问“回收价/黄金回收/24K回收”，只调用 quote_type="recycle"。
6. 用户问“国际金/伦敦金/XAUUSD/COMEX/纽约金”，只调用 quote_type="international"。
7. 用户没问某个口径，就不要主动展示该口径。
8. 不得用国际金价折算国内金价冒充上海黄金交易所价格。
9. 目标口径查不到时，只说明查不到，不要自动切换到别的口径，除非用户要求。
10. 舆情分析必须走“搜索结果 -> 二次打开网页正文 -> 抽取日期/来源 -> 日期过滤 -> 基于证据分析”的爬虫链路。公开页面抓不到正文或日期时，必须说明材料不足，不能编造小红书/微博/雪球内容。
11. 用户明确说“百度/百度新闻/百度资讯”，analyze_market_context 的 platform 必须传“百度”，不要改成“新闻”。
12. 用户明确指定网站或平台时，只搜索该网站/平台；没有近期结果就说没有，不能自动换平台。
13. 舆情报告里的具体数字必须来自工具返回的检索材料；没有来源的数字不要写。
14. 用户要求 Word 报告时，可以先检索舆情/运行量化分析，再 create_word_report。生成 Word 前系统会自动质检；如果发现日期、证据、数字或来源有问题，必须自动补爬/重写，不能把低质量首版直接发出去。
15. 用户要求“跑代码/量化分析/技术指标/回测/统计”时，优先调用 run_quant_analysis；不要执行任意危险代码。
16. 如果 run_quant_analysis 返回 error，只能说明量化历史行情获取失败，并展示 attempted_sources；不要拿搜索引擎旧新闻、旧价格或舆情材料冒充近期量化数据。
17. 黄金实时价/量化数据优先走 AllTick；AllTick 失败时必须把失败原因简要展示出来，不要假装数据成功。
18. 用户用自然语言描述已经发生的黄金买卖、记账、持仓或盈亏时，调用 manage_gold_ledger。表达格式不固定，由你从上下文提取结构化字段。
19. 账本新增必须有明确买卖方向、黄金克重，以及每克价格或总金额。字段缺失、含义歧义时先追问，绝不猜数字；计划、假设、询价和投资建议不能写入账本。
20. 用户说“花了多少钱买了多少克”时，可把总金额和克重传给 manage_gold_ledger，由工具计算每克成本；手续费/工费只有用户明确提及时才填写。
16. 用户指定网站或搜索入口时，必须按用户指定入口搜索；用户说“百度”就走百度，不要改成新闻或全网。
15. 回复要短、直接、专业；金融内容只做信息分析，不承诺收益，不给绝对买卖指令。

价格回复格式：
- 第一行直接给价格。
- 第二行给来源和时间。
- 必要时一行说明口径。
"""

SYSTEM_PROMPT += """

用户投资档案规则：
1. 用户明确设置目标仓位、风险等级、投资目标、单笔上限、回撤容忍、偏好渠道、
   报告风格、默认成本法或是否允许建议时，调用 manage_investment_profile。
2. “查看我的黄金投资档案”使用 action=show；自然语言配置优先使用
   action=parse_text 并传入原始文本。
3. 用户要求“根据我的持仓给建议”或询问仓位是否合理时，调用
   get_personalized_advice。建议必须服从结构化投资档案。
4. 若 allow_suggestion=false，只提供事实和风险信息，不输出操作倾向。
5. 个性化建议必须包含风险提示，不承诺收益，不给绝对化买卖指令。
"""


user_memory_cache: Dict[str, List[Dict[str, Any]]] = {}
memory_lock = threading.Lock()


def get_user_context(user_id: str) -> List[Dict[str, Any]]:
    # 每轮都刷新 system prompt，把 SQLite 长期记忆注入进去；进程重启后仍生效。
    system_content = SYSTEM_PROMPT + build_memory_prompt(user_id)
    with memory_lock:
        if user_id not in user_memory_cache:
            restored: List[Dict[str, Any]] = []
            with db_lock:
                c.execute(
                    """
                    SELECT user_text, assistant_text
                    FROM interaction_logs
                    WHERE user_id = ? AND assistant_text != ''
                    ORDER BY id DESC
                    LIMIT 12
                    """,
                    (user_id,),
                )
                rows = list(reversed(c.fetchall()))
            for user_text, assistant_text in rows:
                if user_text:
                    restored.append({"role": "user", "content": compact_text(user_text, 2000)})
                if assistant_text:
                    restored.append({"role": "assistant", "content": compact_text(assistant_text, 3000)})
            user_memory_cache[user_id] = [{"role": "system", "content": system_content}] + restored
        else:
            user_memory_cache[user_id][0] = {"role": "system", "content": system_content}
        if len(user_memory_cache[user_id]) > 31:
            system_prompt = user_memory_cache[user_id][0]
            user_memory_cache[user_id] = [system_prompt] + user_memory_cache[user_id][-30:]
        return user_memory_cache[user_id]


def ensure_llm_client() -> Optional[OpenAI]:
    if not DEEPSEEK_API_KEY:
        return None
    return OpenAI(api_key=DEEPSEEK_API_KEY, base_url=BASE_URL_DEEPSEEK)


def has_ledger_intent(user_text: str) -> bool:
    text = (user_text or "").strip()
    explicit = ["记账", "记一笔", "记到账", "记到帐", "账本", "持仓盈亏", "我的盈亏", "撤销上一笔"]
    if any(k in text for k in explicit):
        return True
    completed_trade = any(k in text for k in ["买了", "入手了", "拿了", "卖了", "出了", "出手了", "到账"])
    accounting_hint = any(k in text for k in ["帮我记", "记录一下", "算到成本", "算盈亏", "也记上"])
    return completed_trade and accounting_hint


def direct_route_gold_query(user_text: str) -> Optional[Dict[str, Any]]:
    """
    简单金价问题直接路由，不必每次都让 LLM 决策，速度更快且不跑偏。
    返回工具参数或 None。
    """
    t = user_text.strip()

    # 用户明确要汇总/所有口径时，允许一次性整理；普通“现在金价”仍然只查单一口径。
    if any(k in t for k in ["所有", "全部", "整理", "汇总", "一览"]) and any(k in t for k in ["金价", "黄金", "回收", "金店", "积存金", "上海金", "国际金"]):
        return {"quote_type": "all"}

    if any(k in t for k in ["白银", "银价", "国际银", "伦敦银", "xag", "XAG", "SILVER", "silver"]):
        return {"quote_type": "silver"}

    # 解释 AllTick GOLD 口径，不重新调用模型。
    if any(k in t for k in ["纽约还是伦敦", "是什么的国际金价", "这个4181", "这个价格是什么"]):
        return {"quote_type": "international_note"}

    brand_names = ["周大福", "老凤祥", "中国黄金", "周生生", "周六福", "六福", "六福珠宝", "老庙", "老庙黄金", "菜百", "菜百首饰", "潮宏基", "金至尊", "谢瑞麟"]
    for b in brand_names:
        if b in t:
            return {"quote_type": "brand", "brand": b}

    bank_names = ["中国银行", "中行", "工商银行", "工行", "建设银行", "建行", "农业银行", "农行", "浦发银行", "浦发", "银行金条", "积存金", "金条"]
    for b in bank_names:
        if b in t:
            bank = "" if b in ["银行金条", "金条", "积存金"] else b
            return {"quote_type": "bank_bar", "bank": bank}

    if any(k in t for k in ["回收", "回收价", "24K回收"]):
        return {"quote_type": "recycle"}

    if any(k.lower() in t.lower() for k in ["国际金", "伦敦金", "xau", "xauusd", "comex", "纽约金", "美金", "美元/盎司"]):
        return {"quote_type": "international"}

    if any(k in t for k in ["金价", "黄金价格", "国内金", "上海金", "上海黄金交易所", "现在黄金", "今日黄金"]):
        return {"quote_type": "sge_spot"}

    return None


def user_wants_word_report(user_text: str) -> bool:
    return any(k in user_text for k in ["Word", "word", "报告", "文档", "导出", "文件", "docx"])



def sanitize_history_for_api(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """修复长期会话里残留的 orphan tool 消息，避免 OpenAI/DeepSeek 400。"""
    out: List[Dict[str, Any]] = []
    pending_tool_ids = set()
    for m in messages:
        role = m.get("role")
        if role == "tool":
            tid = m.get("tool_call_id")
            if tid in pending_tool_ids:
                out.append(m)
                pending_tool_ids.discard(tid)
            # orphan tool 直接丢弃
            continue
        if role == "assistant" and m.get("tool_calls"):
            out.append(m)
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    pending_tool_ids.add(tc.get("id"))
            continue
        # 普通文本消息不要带 tool_calls 残留字段
        clean = {"role": role, "content": m.get("content", "")}
        if role in ["system", "user", "assistant"]:
            out.append(clean)
    return out


def reset_user_runtime_memory(user_id: str) -> None:
    """模型消息结构损坏时只清短期会话，不清 SQLite 长期记忆。"""
    with memory_lock:
        system_content = SYSTEM_PROMPT + build_memory_prompt(user_id)
        user_memory_cache[user_id] = [{"role": "system", "content": system_content}]

def run_agent(user_query: str, user_id: str) -> str:
    if hasattr(threading.current_thread(), "uploaded_file_path"):
        delattr(threading.current_thread(), "uploaded_file_path")
    threading.current_thread().latest_user_query = user_query
    threading.current_thread().current_user_id = user_id
    if hasattr(threading.current_thread(), "last_report_quality"):
        delattr(threading.current_thread(), "last_report_quality")

    # 先从用户纠正/偏好中学习，保证下一轮和本轮 system prompt 都能用上。
    update_user_memory_from_text(user_id, user_query, "", log_interaction=False)

    profile_updates = parse_profile_updates(user_query)
    explicit_profile_update = any(
        term in user_query
        for term in ["记住", "以后", "修改我的", "设置我的", "改成"]
    )
    if profile_updates and explicit_profile_update:
        profile = update_investment_profile(user_id, profile_updates)
        return "投资档案已更新。\n" + format_investment_profile(profile)
    if any(
        term in user_query
        for term in ["查看我的黄金投资档案", "查看我的投资档案", "我的黄金投资档案"]
    ):
        return format_investment_profile(get_investment_profile(user_id))
    if "根据我的持仓" in user_query and any(
        term in user_query for term in ["建议", "分析", "仓位"]
    ):
        return format_personalized_advice(
            generate_personalized_advice(user_id)
        )
    if any(term in user_query for term in ["你能做什么", "怎么用", "帮助菜单"]):
        return help_text()

    # 价格类简单问答直接走工具，避免 LLM 乱调全量。
    routed = None if has_ledger_intent(user_query) else direct_route_gold_query(user_query)
    if routed and not any(k in user_query for k in ["分析", "报告", "为什么", "原因", "舆情", "新闻", "小红书", "微博", "雪球"]):
        qt = routed.get("quote_type", "")
        if qt == "all":
            return format_all_gold_prices_reply(get_all_gold_prices())
        if qt == "silver":
            return format_silver_quote_reply(fetch_alltick_silver_price())
        if qt == "international_note":
            return (
                "这个价格来自 AllTick 的 GOLD 商品/贵金属 code，单位是美元/盎司。\n"
                "更准确地说：它是 AllTick 提供的国际黄金报价，偏现货/贵金属商品口径；不是上海黄金交易所价格，也不要直接理解成品牌金店价格。\n"
                "它也不应被我说成纽约 COMEX 期货；如果你要纽约期货，应明确查 COMEX/GC；如果你要伦敦现货，应明确查伦敦金/XAUUSD。"
            )
        result = get_gold_quote(**routed)
        return format_gold_quote_reply(result, routed.get("quote_type", ""))

    llm_client = ensure_llm_client()
    if not llm_client:
        return "未配置 DEEPSEEK_API_KEY，不能调用 Agent 模型。简单金价查询可用，但复杂分析/报告需要配置模型 Key。"

    history_messages = get_user_context(user_id)
    history_messages.append({"role": "user", "content": user_query})

    try:
        # 关键修复：不要只允许第一轮 function calling。
        # 之前的问题是：第一轮调用 analyze_market_context 后，第二轮没有继续传 tools，
        # 模型想调用 create_word_report 时只能把 DSML/tool_calls 当普通文本吐到飞书，
        # 所以 Word 根本没有生成。这里改为多轮工具循环，直到模型不再请求工具。
        max_tool_rounds = 6
        called_tools: List[str] = []
        last_plain_answer = ""

        for round_idx in range(max_tool_rounds):
            resp = llm_client.chat.completions.create(
                model=LLM_MODEL_DEEPSEEK,
                messages=sanitize_history_for_api(history_messages),
                tools=tools,
                tool_choice="auto",
                temperature=0.2,
            )
            msg = resp.choices[0].message

            tool_calls = getattr(msg, "tool_calls", None)
            if not tool_calls:
                answer = msg.content or "数据整理完成。"
                last_plain_answer = answer
                history_messages.append({"role": "assistant", "content": answer})

                # 兜底修复：用户明确要求 Word，但模型只给了正文、没有调用 create_word_report。
                # 这时直接把最终正文生成 Word，避免再次出现“说生成了但没文件”。
                if user_wants_word_report(user_query) and not getattr(threading.current_thread(), "uploaded_file_path", None):
                    title = "黄金市场舆情分析报告"
                    if "大跌" in user_query or "暴跌" in user_query:
                        title = "金价大跌舆情分析报告"
                    report_msg = create_word_report(title, answer)
                    called_tools.append("create_word_report")
                    return report_msg

                return answer

            assistant_tool_calls = []
            for tc in tool_calls:
                assistant_tool_calls.append(
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                )

            history_messages.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": assistant_tool_calls,
                }
            )

            for tc in tool_calls:
                func_name = tc.function.name
                called_tools.append(func_name)
                try:
                    args_dict = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args_dict = {}

                print(f"[工具调用] {func_name} args={args_dict}")
                result = call_tool(func_name, args_dict)
                history_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                )

        # 超过工具轮数时的兜底，防止死循环。
        if user_wants_word_report(user_query) and not getattr(threading.current_thread(), "uploaded_file_path", None):
            return create_word_report("黄金市场舆情分析报告", last_plain_answer or "工具调用轮数过多，未能生成完整分析正文。")

        return "工具调用轮数过多，已中止。请缩小问题范围后重试。"

    except Exception as e:
        traceback.print_exc()
        if "Messages with role 'tool'" in str(e) or "tool_calls" in str(e):
            reset_user_runtime_memory(user_id)
            return "刚才短期会话里有一条工具调用上下文损坏，我已自动清理短期会话记忆。长期偏好还在，你再问一次就可以继续。"
        return f"Agent 执行失败：{e}"
