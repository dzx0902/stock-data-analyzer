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

from gold_agent.config import BASE_URL_QWEN, LLM_MODEL_QWEN, QWEN_API_KEY
from gold_agent.infra.http import compact_text, http_get

class SearchItem:
    title: str
    snippet: str
    url: str
    source: str


def parse_baidu_results(html: str, source_name: str = "百度") -> List[SearchItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: List[SearchItem] = []
    for block in soup.select("div.result, div.c-container")[:10]:
        a = block.select_one("h3 a") or block.select_one("a")
        if not a:
            continue
        title = compact_text(a.get_text(" "), 120)
        snippet = compact_text(block.get_text(" "), 360)
        url = a.get("href", "")
        if title:
            items.append(SearchItem(title=title, snippet=snippet, url=url, source=source_name))
    return items


def parse_sogou_results(html: str) -> List[SearchItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: List[SearchItem] = []
    for block in soup.select(".results .vrwrap, .results .rb, .result")[:10]:
        a = block.select_one("h3 a") or block.select_one("a")
        if not a:
            continue
        title = compact_text(a.get_text(" "), 120)
        snippet = compact_text(block.get_text(" "), 360)
        url = a.get("href", "")
        if title:
            items.append(SearchItem(title=title, snippet=snippet, url=url, source="搜狗"))
    return items


def parse_bing_results(html: str) -> List[SearchItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: List[SearchItem] = []
    for li in soup.select("li.b_algo")[:10]:
        a = li.select_one("h2 a")
        if not a:
            continue
        title = compact_text(a.get_text(" "), 120)
        snippet_node = li.select_one(".b_caption p")
        snippet = compact_text(snippet_node.get_text(" ") if snippet_node else li.get_text(" "), 360)
        url = a.get("href", "")
        if title:
            items.append(SearchItem(title=title, snippet=snippet, url=url, source="必应中国"))
    return items


def domestic_web_search(query: str, max_results: int = 12, engines: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """国内入口优先：百度、百度资讯、搜狗、必应中国。

    改进点：
    - 对百度同时跑普通搜索和资讯搜索；
    - 对“近24小时”类 query 自动加入当天日期变体，减少旧新闻污染；
    - 不再只取很少结果，给后续正文爬取更多候选。
    """
    query = compact_text(query, 180)
    results: List[SearchItem] = []
    engines_set = set(engines or ["百度资讯", "百度", "搜狗", "必应中国"])

    today = datetime.now()
    today_cn = f"{today.year}年{today.month}月{today.day}日"
    today_dash = today.strftime("%Y-%m-%d")

    query_variants = [query]
    if any(x in query for x in ["24小时", "今日", "今天", "最新", "大跌", "暴跌"]):
        query_variants.extend([
            f"{query} {today_cn}",
            f"{query} {today_dash}",
        ])

    all_jobs = [
        ("百度资讯", "https://www.baidu.com/s", lambda q: {"wd": q, "tn": "news", "rn": "20"}, lambda h: parse_baidu_results(h, "百度资讯")),
        ("百度", "https://www.baidu.com/s", lambda q: {"wd": q, "rn": "20"}, parse_baidu_results),
        ("搜狗", "https://www.sogou.com/web", lambda q: {"query": q}, lambda h: parse_sogou_results(h)),
        ("必应中国", "https://cn.bing.com/search", lambda q: {"q": q, "ensearch": "0"}, lambda h: parse_bing_results(h)),
    ]

    for q in query_variants:
        for name, url, params_fn, parser in all_jobs:
            if name not in engines_set:
                continue
            try:
                resp = http_get(url, params=params_fn(q), timeout=10, use_search_proxy=False)
                html = resp.text or ""
                parsed = parser(html)
                results.extend(parsed)
            except Exception as e:
                print(f"[{name}搜索失败] {e}")
                continue
            if len(results) >= max_results * 2:
                break
        if len(results) >= max_results * 2:
            break

    # 去重：百度跳转链接不同但标题相同，优先保留更靠前结果
    dedup: Dict[str, SearchItem] = {}
    for item in results:
        norm_title = re.sub(r"\\s+", "", item.title or "")[:80]
        key = norm_title or (item.url or "")
        if key and key not in dedup:
            dedup[key] = item

    return [
        {"title": x.title, "snippet": x.snippet, "url": x.url, "source": x.source}
        for x in list(dedup.values())[:max_results]
    ]

PLATFORM_SITE_MAP = {
    "小红书": "xiaohongshu.com",
    "小红薯": "xiaohongshu.com",
    "xhs": "xiaohongshu.com",
    "微博": "weibo.com",
    "weibo": "weibo.com",
    "雪球": "xueqiu.com",
    "xueqiu": "xueqiu.com",
    "东方财富": "guba.eastmoney.com",
    "股吧": "guba.eastmoney.com",
    "知乎": "zhihu.com",
    "百度贴吧": "tieba.baidu.com",
}


def parse_date_from_text(text: str) -> str:
    """从搜索标题/摘要中尽量提取日期。提取不到返回空字符串。"""
    text = text or ""
    now = datetime.now()

    # 相对时间
    if re.search(r"(刚刚|分钟前|小时前|今天|今日)", text):
        return now.strftime("%Y-%m-%d")
    if re.search(r"(昨天|昨日|1天前)", text):
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*天前", text)
    if m:
        return (now - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")

    # 2026-06-12 / 2026年6月12日 / 2026/06/12
    m = re.search(r"(20\d{2})[-/.年]\s*(\d{1,2})[-/.月]\s*(\d{1,2})\s*日?", text)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

    # 6月12日 / 06-12
    m = re.search(r"(?<!\d)(\d{1,2})[-/.月]\s*(\d{1,2})\s*日?", text)
    if m:
        mo, d = m.groups()
        return f"{now.year:04d}-{int(mo):02d}-{int(d):02d}"

    return ""


def filter_recent_items(items: List[Dict[str, str]], time_range: str = "24h") -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """按检索摘要日期做硬过滤。没有日期的保留为 low_confidence，不作为硬事实。"""
    now = datetime.now()
    days = 1 if time_range in ["1h", "24h", "today", "今日"] else 7
    cutoff = now - timedelta(days=days)
    recent: List[Dict[str, str]] = []
    low_conf: List[Dict[str, str]] = []

    for item in items:
        blob = f"{item.get('title','')} {item.get('snippet','')}"
        d = parse_date_from_text(blob)
        item["detected_date"] = d
        if not d:
            item["date_confidence"] = "unknown"
            low_conf.append(item)
            continue
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            if dt >= cutoff:
                item["date_confidence"] = "recent"
                recent.append(item)
            else:
                item["date_confidence"] = "stale"
        except Exception:
            item["date_confidence"] = "unknown"
            low_conf.append(item)

    return recent, low_conf



# ==============================================================================
# 4.1 真爬虫层：搜索结果必须二次打开正文、抽取日期和来源
# ==============================================================================


def resolve_possible_redirect(url: str) -> str:
    """尽量解析百度/搜狗等搜索结果跳转链接，失败时返回原链接。"""
    if not url:
        return ""
    try:
        resp = http_get(url, timeout=8, use_search_proxy=False)
        return resp.url or url
    except Exception:
        return url


def extract_article_date(text: str, html: str = "") -> str:
    """从正文或 HTML meta 中提取发布时间，返回 YYYY-MM-DD 或空。"""
    html = html or ""
    text = text or ""

    # meta/pubdate 常见格式
    meta_patterns = [
        r'(?:datePublished|publishDate|pubdate|article:published_time)["\']?\s*[:=]\s*["\'](20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})',
        r'(?:发布时间|发布日期|发稿时间|更新时间|来源时间)[:：\s]*(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})',
    ]
    blob = html[:20000] + "\n" + text[:5000]
    for pat in meta_patterns:
        m = re.search(pat, blob, re.I)
        if m:
            y, mo, d = m.groups()[:3]
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

    d = parse_date_from_text(blob)
    return d


def extract_source_name(url: str, title: str, text: str) -> str:
    host = ""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.replace("www.", "")
    except Exception:
        host = ""
    # 尝试从正文提取来源
    m = re.search(r"来源[:：]\s*([^\s｜|，。]{2,30})", text or "")
    if m:
        return m.group(1)
    return host or "未知来源"


def fetch_article(url: str, timeout: int = 10) -> Dict[str, Any]:
    """打开搜索结果页，抽取标题、正文、日期、来源。"""
    if not url:
        return {"status": "error", "url": url, "message": "empty url"}

    final_url = resolve_possible_redirect(url)
    try:
        resp = http_get(final_url, timeout=timeout, use_search_proxy=False)
        resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
        html = resp.text or ""
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "canvas"]):
            tag.decompose()

        title = ""
        if soup.title:
            title = compact_text(soup.title.get_text(" "), 180)
        h1 = soup.select_one("h1")
        if h1 and len(h1.get_text(strip=True)) >= 4:
            title = compact_text(h1.get_text(" "), 180)

        # 优先常见正文容器，失败再全页文本
        containers = soup.select("article, .article, .article-content, .content, .main, #article, #content, .rich_media_content")
        texts = []
        for ctn in containers[:5]:
            t = ctn.get_text("\n", strip=True)
            if len(t) > 200:
                texts.append(t)
        text = max(texts, key=len) if texts else soup.get_text("\n", strip=True)
        text = re.sub(r"\n{2,}", "\n", text)
        text = compact_text(text, 12000)

        date = extract_article_date(text, html)
        source_name = extract_source_name(resp.url or final_url, title, text)
        return {
            "status": "success",
            "url": url,
            "final_url": resp.url or final_url,
            "title": title,
            "text": text,
            "text_len": len(text),
            "date": date,
            "source_name": source_name,
        }
    except Exception as e:
        return {"status": "error", "url": url, "final_url": final_url, "message": str(e)}


def crawl_search_results(items: List[Dict[str, str]], max_articles: int = 8) -> List[Dict[str, Any]]:
    """对搜索结果进行二次抓取。保留搜索摘要 + 正文。"""
    articles: List[Dict[str, Any]] = []
    for idx, item in enumerate(items[:max_articles], 1):
        url = item.get("url", "")
        article = fetch_article(url)
        article["search_rank"] = idx
        article["search_title"] = item.get("title", "")
        article["search_snippet"] = item.get("snippet", "")
        article["search_source"] = item.get("source", "")
        # 如果正文抓取失败，至少保留搜索结果本身，但标记低置信
        if article.get("status") != "success":
            blob = f"{item.get('title','')} {item.get('snippet','')}"
            article.update({
                "title": item.get("title", ""),
                "text": item.get("snippet", ""),
                "text_len": len(item.get("snippet", "")),
                "date": parse_date_from_text(blob),
                "source_name": item.get("source", "搜索结果"),
                "crawler_confidence": "low_search_snippet_only",
            })
        else:
            article["crawler_confidence"] = "high_article_body" if article.get("text_len", 0) > 500 else "medium_short_body"
        articles.append(article)
    return articles


def filter_recent_articles(articles: List[Dict[str, Any]], time_range: str = "24h") -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按正文日期过滤；无日期文章保留为 low_confidence。"""
    now = datetime.now()
    days = 1 if time_range in ["1h", "24h", "today", "今日"] else 7
    cutoff = now - timedelta(days=days)
    recent: List[Dict[str, Any]] = []
    unknown: List[Dict[str, Any]] = []
    for art in articles:
        d = art.get("date") or ""
        if not d:
            art["date_confidence"] = "unknown"
            unknown.append(art)
            continue
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            if dt >= cutoff:
                art["date_confidence"] = "recent"
                recent.append(art)
            else:
                art["date_confidence"] = "stale"
        except Exception:
            art["date_confidence"] = "unknown"
            unknown.append(art)
    return recent, unknown

def build_platform_query(platform: str, keyword: str, time_range: str = "24h") -> str:
    platform = (platform or "全网").strip()
    keyword = (keyword or "黄金 金价").strip()
    time_hint = "今天 最新 24小时" if time_range in ["1h", "24h", "today", "今日"] else "本周 最新 7天"

    site = PLATFORM_SITE_MAP.get(platform)
    if site:
        return f"site:{site} {keyword} 黄金 金价 {time_hint}"

    # 用户说百度/百度新闻，是搜索引擎/入口，不是内容平台；不把它改成“新闻”。
    if platform in ["百度", "百度新闻", "百度资讯"]:
        return f"{keyword} 黄金 金价 评论 报告 舆情 {time_hint}"

    if platform in ["新闻", "财经媒体", "财经新闻"]:
        return f"{keyword} 黄金 金价 财经新闻 评论 报告 {time_hint}"

    return f"{platform} {keyword} 黄金 金价 评论 报告 舆情 {time_hint}"


def fetch_platform_context(platform: str, keyword: str = "黄金 金价", time_range: str = "24h") -> Dict[str, Any]:
    platform = (platform or "全网").strip()
    query = build_platform_query(platform, keyword, time_range)

    if platform in ["百度", "百度新闻", "百度资讯"]:
        engines = ["百度资讯", "百度"]
    else:
        engines = None

    raw_items = domestic_web_search(query, max_results=18, engines=engines)
    crawled_articles = crawl_search_results(raw_items, max_articles=12)
    recent_articles, unknown_articles = filter_recent_articles(crawled_articles, time_range)

    # 近24小时报告必须有“日期确认近期”的正文证据；无日期材料只进入附录，不再喂给模型当事实。
    if time_range in ["1h", "24h", "today", "今日"]:
        articles_for_analysis = recent_articles
    else:
        articles_for_analysis = recent_articles if recent_articles else unknown_articles[:5]

    return {
        "platform": platform,
        "keyword": keyword,
        "time_range": time_range,
        "query": query,
        "search_items": raw_items[:18],
        "articles": articles_for_analysis,
        "low_confidence_articles": unknown_articles[:8],
        "recent_articles_count": len(recent_articles),
        "unknown_date_articles_count": len(unknown_articles),
        "crawled_articles_before_date_filter": crawled_articles[:12],
        "warning": (
            "已执行真爬虫链路：搜索结果 -> 打开结果页 -> 抽取正文/日期/来源 -> 日期过滤。"
            "近24小时场景只使用日期确认近期的正文；无日期/登录墙/验证码页面只进低置信附录，不作为事实。"
        ),
    }

def analyze_market_context(platform: str, keyword: str = "黄金 金价", time_range: str = "24h") -> Dict[str, Any]:
    raw_context = fetch_platform_context(platform, keyword, time_range)
    articles = raw_context.get("articles") or []

    if not articles:
        search_titles = []
        for i, item in enumerate((raw_context.get("search_items") or [])[:8], 1):
            search_titles.append(f"[{i}] {item.get('source','')}｜{item.get('title','')}｜{item.get('snippet','')[:120]}")
        return {
            "status": "no_data",
            "platform": platform,
            "keyword": keyword,
            "time_range": time_range,
            "message": (
                "搜索和二次爬取后，没有拿到日期确认属于目标时间范围的正文材料。"
                "我不会用旧闻或无日期网页冒充近期舆情。\n\n"
                "可见搜索候选如下（仅作线索，不作事实结论）：\n" + "\n".join(search_titles)
            ),
            "raw_context": raw_context,
        }

    if not QWEN_API_KEY:
        return {
            "status": "success",
            "platform": platform,
            "keyword": keyword,
            "time_range": time_range,
            "raw_count": len(articles),
            "analysis": "已完成搜索与正文爬取，但未配置 QWEN_API_KEY，无法调用模型做舆情归纳。",
            "raw_context": raw_context,
        }

    evidence_lines = []
    for i, art in enumerate(articles, 1):
        text_preview = compact_text(art.get("text", ""), 1800)
        evidence_lines.append(
            f"[{i}] 来源={art.get('source_name','')} 搜索入口={art.get('search_source','')} 日期={art.get('date','未知')} "
            f"日期置信={art.get('date_confidence','unknown')} 抓取置信={art.get('crawler_confidence','')}\n"
            f"标题={art.get('title') or art.get('search_title','')}\n"
            f"链接={art.get('final_url') or art.get('url','')}\n"
            f"搜索摘要={art.get('search_snippet','')}\n"
            f"正文摘录={text_preview}"
        )
    evidence_text = "\n\n".join(evidence_lines)

    prompt = f"""
你是严谨的大宗商品与黄金市场舆情分析师。你只能基于下方【爬虫证据材料】分析【{platform}】范围内关于【{keyword}】的舆情。

硬性规则：
1. 不能编造任何新闻、评论、日期、价格、跌幅、CPI、非农、美联储概率、ETF流出等事实。
2. 所有具体数字必须出现在证据材料原文里，并在句末标注材料编号，例如“[2]”。证据里没有的数字一律不要写。
3. 日期置信为 unknown 的材料只能说“公开页面未提取到日期”，不得当成“24小时内事实”。
4. 抓取置信为 low_search_snippet_only 的材料只能作为线索，不能支撑强结论。
5. 不要把品牌金店零售价、上海黄金交易所价格、国际金价、回收价混为一谈。
6. 如果证据不足，就直接写“材料不足，无法确认”，不要补脑。
7. 输出必须包含“证据来源列表”，列出每条材料编号、标题、日期/日期置信、链接。
8. 金融内容只做信息分析，不给确定性买卖指令。

爬虫证据材料：
{evidence_text}

请输出：核心结论、事实依据、情绪判断、多空因素、材料不足、证据来源列表。
"""
    try:
        qwen_client = OpenAI(api_key=QWEN_API_KEY, base_url=BASE_URL_QWEN)
        resp = qwen_client.chat.completions.create(
            model=LLM_MODEL_QWEN,
            messages=[
                {"role": "system", "content": "你是严谨的金融舆情分析模型。只能基于用户给出的爬虫证据材料分析。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.15,
        )
        return {
            "status": "success",
            "platform": platform,
            "keyword": keyword,
            "time_range": time_range,
            "raw_count": len(articles),
            "analysis": resp.choices[0].message.content,
            "raw_context": raw_context,
        }
    except Exception as e:
        return {
            "status": "error",
            "platform": platform,
            "keyword": keyword,
            "message": f"舆情模型分析失败：{e}",
            "raw_context": raw_context,
        }
