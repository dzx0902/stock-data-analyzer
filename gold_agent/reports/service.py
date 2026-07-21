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

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt

from gold_agent.market.search import analyze_market_context
from gold_agent.security.audit import record_audit

def _clean_markdown_inline(text: str) -> str:
    text = text or ""
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = text.replace("<br>", "\n")
    return text.strip()


def _add_markdown_paragraph(doc: Document, line: str) -> None:
    raw = line.rstrip()
    if not raw:
        doc.add_paragraph()
        return

    # horizontal rule
    if re.fullmatch(r"[-—_]{3,}", raw.strip()):
        p = doc.add_paragraph()
        p.add_run("—" * 32)
        return

    # headings
    m = re.match(r"^(#{1,6})\s+(.+)$", raw)
    if m:
        level = min(len(m.group(1)), 3)
        doc.add_heading(_clean_markdown_inline(m.group(2)), level=level)
        return

    # unordered list
    m = re.match(r"^\s*[-*]\s+(.+)$", raw)
    if m:
        p = doc.add_paragraph(style="List Bullet")
        p.add_run(_clean_markdown_inline(m.group(1))).font.size = Pt(10.5)
        return

    # ordered list
    m = re.match(r"^\s*\d+[.)、]\s+(.+)$", raw)
    if m:
        p = doc.add_paragraph(style="List Number")
        p.add_run(_clean_markdown_inline(m.group(1))).font.size = Pt(10.5)
        return

    p = doc.add_paragraph()
    # simple bold splitter for **text**
    parts = re.split(r"(\*\*.*?\*\*)", raw)
    for part in parts:
        if not part:
            continue
        run = p.add_run(part[2:-2] if part.startswith("**") and part.endswith("**") else _clean_markdown_inline(part))
        run.bold = part.startswith("**") and part.endswith("**")
        run.font.size = Pt(10.5)


def _try_add_markdown_table(doc: Document, lines: List[str], start_idx: int) -> int:
    """识别 Markdown 表格，成功返回消费行数，失败返回 0。"""
    if start_idx + 1 >= len(lines):
        return 0
    header = lines[start_idx].strip()
    sep = lines[start_idx + 1].strip()
    if not (header.startswith("|") and header.endswith("|") and sep.startswith("|") and re.search(r"[-:]{3,}", sep)):
        return 0

    rows = []
    i = start_idx
    while i < len(lines):
        line = lines[i].strip()
        if not (line.startswith("|") and line.endswith("|")):
            break
        if i == start_idx + 1:  # separator
            i += 1
            continue
        cells = [_clean_markdown_inline(c.strip()) for c in line.strip("|").split("|")]
        rows.append(cells)
        i += 1

    if not rows:
        return 0
    col_count = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=col_count)
    table.style = "Table Grid"
    for r_idx, row in enumerate(rows):
        for c_idx in range(col_count):
            cell_text = row[c_idx] if c_idx < len(row) else ""
            cell = table.cell(r_idx, c_idx)
            cell.text = cell_text
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(9)
                    if r_idx == 0:
                        run.bold = True
    doc.add_paragraph()
    return i - start_idx




# ==============================================================================
# 4.5 自检与自我迭代：报告生成前先做质量审查，必要时自动重写/补爬
# ==============================================================================


def _extract_dates_for_quality(text: str) -> List[datetime]:
    dates: List[datetime] = []
    now = datetime.now()
    for m in re.finditer(r"(20\d{2})[-/.年]\s*(\d{1,2})[-/.月]\s*(\d{1,2})\s*日?", text or ""):
        try:
            y, mo, d = m.groups()
            dates.append(datetime(int(y), int(mo), int(d)))
        except Exception:
            pass
    for m in re.finditer(r"(?<!\d)(\d{1,2})月\s*(\d{1,2})日", text or ""):
        try:
            mo, d = m.groups()
            dates.append(datetime(now.year, int(mo), int(d)))
        except Exception:
            pass
    return dates


def review_report_quality(report_title: str, report_content: str, user_query: str = "") -> Dict[str, Any]:
    """用规则先做一遍机器自检，避免明显烂报告直接发出去。

    这个函数不依赖模型，负责拦截最常见问题：
    - 近24小时报告用了旧日期材料
    - 没有证据来源列表/链接/材料编号
    - 出现“材料不足”却还写强结论
    - 段落过短、内容空泛
    - 明显二进制/乱码或 DSML 残留
    """
    text = (report_content or "").strip()
    title = report_title or "黄金市场分析报告"
    query = user_query or ""
    issues: List[str] = []
    score = 100

    if len(text) < 800:
        issues.append("报告正文过短，可能没有形成有效分析。")
        score -= 25

    if "b'PK" in text or "\\x00" in text or "[Content_Types].xml" in text:
        issues.append("报告内容疑似混入 docx 二进制/乱码。")
        score -= 60

    if "<｜｜DSML" in text or "tool_calls" in text or "invoke name=" in text:
        issues.append("报告内容混入工具调用标记，说明工具调用没有真正执行。")
        score -= 45

    wants_recent = any(k in (query + title + text) for k in ["24小时", "近24", "今天", "今日", "最新", "本次", "这次"])
    dates = _extract_dates_for_quality(text)
    if wants_recent and dates:
        now = datetime.now()
        old_dates = [d.strftime("%Y-%m-%d") for d in dates if (now - d).days > 3]
        if old_dates:
            issues.append("近24小时/最新报告中出现旧日期材料：" + ", ".join(sorted(set(old_dates))[:8]))
            score -= 35

    # 证据约束：舆情/新闻/爬虫报告必须有来源、链接或材料编号。
    is_sentiment_report = any(k in (query + title + text) for k in ["舆情", "新闻", "百度", "小红书", "微博", "雪球", "爬虫", "评论", "报告"])
    has_evidence_list = any(k in text for k in ["证据来源", "来源列表", "材料[", "链接=", "http://", "https://"])
    has_material_mark = bool(re.search(r"\[\d+\]", text))
    if is_sentiment_report and not (has_evidence_list or has_material_mark):
        issues.append("舆情/新闻报告缺少证据来源列表或材料编号。")
        score -= 35

    if "材料不足" in text and any(k in text for k in ["极度", "确定", "核心驱动", "必然", "已经确认"]):
        issues.append("报告一边说材料不足，一边给出过强结论。")
        score -= 25

    if re.search(r"\d+(?:\.\d+)?\s*(美元/盎司|元/克|%|万吨|吨)", text) and is_sentiment_report and not has_material_mark:
        issues.append("报告包含具体金融数字，但没有材料编号支撑。")
        score -= 30

    return {
        "score": max(0, score),
        "pass": score >= 75,
        "issues": issues,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _guess_platform_from_query(user_query: str) -> str:
    t = (user_query or "").lower()
    if "百度" in t:
        return "百度"
    if "小红书" in t or "小红薯" in t or "xhs" in t:
        return "小红书"
    if "微博" in t or "weibo" in t:
        return "微博"
    if "雪球" in t or "xueqiu" in t:
        return "雪球"
    if "东方财富" in t or "股吧" in t:
        return "东方财富"
    return "百度" if any(k in t for k in ["新闻", "舆情", "评论", "报告", "大跌", "暴跌"]) else "全网"


def _guess_keyword_from_query(user_query: str) -> str:
    q = user_query or ""
    kws = []
    for k in ["黄金", "金价", "大跌", "暴跌", "回调", "避险", "美联储", "降息", "加息", "非农", "CPI", "关税", "中东"]:
        if k in q:
            kws.append(k)
    return " ".join(kws) if kws else "黄金 金价"


def auto_improve_report_content(report_title: str, report_content: str) -> Tuple[str, Dict[str, Any]]:
    """报告写入 Word 前的 self-review / self-repair。

    如果质量不合格，它会：
    1. 根据用户原问题再尝试补爬一次；
    2. 用证据材料重写报告；
    3. 如果仍找不到证据，则生成“质检未通过/材料不足”的诚实报告，避免把错误报告发出去。
    """
    user_query = getattr(threading.current_thread(), "latest_user_query", "")
    first_review = review_report_quality(report_title, report_content, user_query)
    if first_review.get("pass"):
        return report_content, {"status": "pass", "review": first_review, "iterations": 0}

    platform = _guess_platform_from_query(user_query)
    keyword = _guess_keyword_from_query(user_query)
    improved_source_note = ""
    improved_analysis = ""

    try:
        # 第一轮：严格按用户原来的时间范围，默认 24h。
        ctx = analyze_market_context(platform=platform, keyword=keyword, time_range="24h")
        if ctx.get("status") == "success" and ctx.get("analysis"):
            improved_analysis = ctx.get("analysis", "")
            improved_source_note = f"\n\n## 自动质检与二次迭代说明\n\n首版报告质检未通过，问题：{'; '.join(first_review.get('issues', []))}。系统已按用户原问题重新执行爬虫链路：平台={platform}，关键词={keyword}，时间范围=24h，并用二次抓取材料重写。"
        else:
            # 第二轮：24h 没有足够材料时，放宽到 7d，但必须明确标注不是 24h。
            ctx7 = analyze_market_context(platform=platform, keyword=keyword, time_range="7d")
            if ctx7.get("status") == "success" and ctx7.get("analysis"):
                improved_analysis = ctx7.get("analysis", "")
                improved_source_note = f"\n\n## 自动质检与二次迭代说明\n\n首版报告质检未通过，问题：{'; '.join(first_review.get('issues', []))}。近24小时可验证材料不足，系统放宽到近7天重新爬取并重写；以下结论不能视为近24小时实时舆情。"
            else:
                improved_analysis = ""
                improved_source_note = ""
    except Exception as e:
        improved_source_note = f"\n\n## 自动质检说明\n\n首版报告质检未通过，且二次补爬失败：{e}。"

    if improved_analysis:
        candidate = improved_analysis + improved_source_note
        second_review = review_report_quality(report_title, candidate, user_query)
        # 即使二次分数不高，只要它有证据/材料不足说明，也优于首版乱写。
        if second_review.get("score", 0) >= max(55, first_review.get("score", 0)):
            return candidate, {
                "status": "improved",
                "review_before": first_review,
                "review_after": second_review,
                "iterations": 1,
            }

    honest = f"""# {report_title or '黄金市场分析报告'}

## 自动质检结论

本报告在生成前未通过质量检查，因此没有直接发送首版内容。

### 发现的问题
{chr(10).join('- ' + x for x in first_review.get('issues', [])) or '- 未知质量问题'}

### 处理结果
系统尝试按用户原问题重新检索和二次爬取，但仍未获得足够可靠、带日期和来源的材料。为避免把旧闻、无日期网页或模型猜测写成事实，本次只输出材料不足结论。

### 建议
请换一个更明确的数据入口或时间范围，例如：
- “去百度资讯查近7天黄金大跌评论报告”
- “去雪球查黄金近7天讨论，生成 Word”
- “用 AllTick 跑黄金近180天量化分析，生成报告”

*本页由自动质检模块生成，说明首版报告被拦截。*
"""
    return honest, {"status": "blocked", "review": first_review, "iterations": 1}

def create_word_report(report_title: str, report_content: str) -> str:
    filename = f"Gold_Agent_Report_{int(time.time())}.docx"
    try:
        report_content, quality = auto_improve_report_content(report_title, report_content)
        threading.current_thread().last_report_quality = quality
        doc = Document()
        section = doc.sections[0]
        section.left_margin = Inches(0.85)
        section.right_margin = Inches(0.85)
        section.top_margin = Inches(0.75)
        section.bottom_margin = Inches(0.75)

        title_p = doc.add_paragraph()
        title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title_run = title_p.add_run(report_title or "黄金市场分析报告")
        title_run.font.size = Pt(18)
        title_run.bold = True

        meta_p = doc.add_paragraph()
        meta_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        meta_run = meta_p.add_run(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
        meta_run.font.size = Pt(9)

        doc.add_paragraph("—" * 32)

        content = (report_content or "").replace("\r\n", "\n")
        # 清理模型偶发的工具标记/DSML 残留
        content = re.sub(r"<｜｜DSML｜｜.*?>", "", content)
        content = re.sub(r"</｜｜DSML｜｜.*?>", "", content)
        lines = content.split("\n")

        i = 0
        while i < len(lines):
            consumed = _try_add_markdown_table(doc, lines, i)
            if consumed:
                i += consumed
                continue
            _add_markdown_paragraph(doc, lines[i])
            i += 1

        doc.save(filename)
        threading.current_thread().uploaded_file_path = filename
        user_id = str(getattr(threading.current_thread(), "current_user_id", "") or "system")
        record_audit(
            user_id,
            "report.create",
            "word_report",
            filename,
            {"quality_status": quality.get("status", "unknown")},
        )
        q = getattr(threading.current_thread(), "last_report_quality", {}) or {}
        if q.get("status") == "pass":
            quality_msg = "已通过自动质检。"
        elif q.get("status") == "improved":
            quality_msg = "首版质检未通过，已自动补爬/重写后生成。"
        elif q.get("status") == "blocked":
            quality_msg = "首版质检未通过，已改为材料不足说明，避免发送错误报告。"
        else:
            quality_msg = "已执行自动质检。"
        return f"Word 报告已生成：{filename}。{quality_msg} 系统将尝试通过飞书发送文件。"
    except Exception as e:
        traceback.print_exc()
        return f"Word 生成失败：{e}"
