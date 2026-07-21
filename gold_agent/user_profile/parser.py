from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from gold_agent.infra.decimal_utils import to_decimal


GOAL_TERMS = {
    "保值": "preservation",
    "长期配置": "long_term",
    "长期投资": "long_term",
    "短线交易": "short_term",
    "短线": "short_term",
    "避险": "hedging",
    "实物收藏": "physical_collection",
    "收藏": "physical_collection",
}
RISK_TERMS = {
    "低风险": "low",
    "风险偏好低": "low",
    "风险偏好比较低": "low",
    "保守": "low",
    "中等风险": "medium",
    "稳健": "medium",
    "高风险": "high",
    "风险偏好高": "high",
    "激进": "high",
}
COST_TERMS = {
    "先进先出": "fifo",
    "FIFO": "fifo",
    "移动平均": "moving_average",
    "加权平均": "moving_average",
    "指定批次": "specific_batch",
}


def _percentage(text: str, labels: list[str]) -> Decimal | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    patterns = [
        rf"(?:{label_pattern})[^\d]{{0,8}}(\d+(?:\.\d+)?)\s*%",
        rf"(\d+(?:\.\d+)?)\s*%[^\n，。]{{0,8}}(?:{label_pattern})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = to_decimal(match.group(1))
            if value is not None:
                return value / Decimal("100")
    return None


def _money(text: str, labels: list[str]) -> Decimal | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:{label_pattern})[^\d]{{0,8}}(\d+(?:\.\d+)?)\s*(万|千)?元?",
        text,
    )
    if not match:
        return None
    value = to_decimal(match.group(1))
    if value is None:
        return None
    multiplier = {"万": Decimal("10000"), "千": Decimal("1000")}.get(
        match.group(2), Decimal("1")
    )
    return value * multiplier


def parse_profile_updates(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    updates: dict[str, Any] = {}
    for term, value in GOAL_TERMS.items():
        if term in text:
            updates["investment_goal"] = value
            break
    for term, value in RISK_TERMS.items():
        if term in text:
            updates["risk_level"] = value
            break
    for term, value in COST_TERMS.items():
        if term.lower() in text.lower():
            updates["default_cost_method"] = value
            break

    allocation = _percentage(text, ["目标仓位", "黄金仓位", "黄金占比"])
    if allocation is not None:
        updates["target_gold_allocation"] = allocation
    drawdown = _percentage(text, ["最大回撤", "回撤容忍", "回撤承受"])
    if drawdown is not None:
        updates["max_drawdown_tolerance"] = drawdown

    max_buy = _money(text, ["单笔最多", "单次最多", "最大单笔", "每次最多"])
    if max_buy is not None:
        updates["max_single_buy_amount_cny"] = max_buy
    total_assets = _money(text, ["总资产", "资产总额"])
    if total_assets is not None:
        updates["total_assets_cny"] = total_assets

    if any(term in text for term in ["简洁一点", "简洁报告", "简短报告"]):
        updates["report_style"] = "concise"
    elif any(term in text for term in ["详细一点", "详细报告"]):
        updates["report_style"] = "detailed"
    elif any(term in text for term in ["数据为主", "数据型报告"]):
        updates["report_style"] = "data_focused"

    channels = [
        channel
        for channel in (
            "银行积存金",
            "银行金条",
            "金店实物",
            "支付宝黄金",
            "微信黄金",
            "黄金ETF",
            "纸黄金",
        )
        if channel in text
    ]
    if channels and any(term in text for term in ["偏好", "喜欢", "优先", "常用"]):
        updates["preferred_channels"] = channels

    if any(term in text for term in ["不要给我操作建议", "不允许建议", "只提供信息"]):
        updates["allow_suggestion"] = False
    elif any(term in text for term in ["可以给建议", "允许建议", "给出操作建议"]):
        updates["allow_suggestion"] = True
    return updates
