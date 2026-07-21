from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from gold_agent.infra import database
from gold_agent.infra.decimal_utils import decimal_text, to_decimal
from gold_agent.infra.http import json_loads_safe, now_ts
from gold_agent.security.audit import record_audit
from gold_agent.user_profile.model import InvestmentProfile


def _decimal_or_none(value: Any) -> Decimal | None:
    return to_decimal(value) if value not in (None, "") else None


def get_investment_profile(user_id: str) -> InvestmentProfile:
    with database.db_lock:
        database.c.execute(
            """
            SELECT investment_goal, risk_level, target_gold_allocation,
                   max_single_buy_amount_cny, max_drawdown_tolerance,
                   preferred_channels_json, report_style, alert_preference_json,
                   default_cost_method, allow_suggestion, total_assets_cny,
                   created_at, updated_at
            FROM investment_profiles WHERE user_id = ?
            """,
            (user_id,),
        )
        row = database.c.fetchone()
    if not row:
        return InvestmentProfile(user_id=user_id).validate()
    return InvestmentProfile(
        user_id=user_id,
        investment_goal=row[0] or "",
        risk_level=row[1] or "medium",
        target_gold_allocation=_decimal_or_none(row[2]),
        max_single_buy_amount_cny=_decimal_or_none(row[3]),
        max_drawdown_tolerance=_decimal_or_none(row[4]),
        preferred_channels=json_loads_safe(row[5], []),
        report_style=row[6] or "concise",
        alert_preference=json_loads_safe(row[7], {}),
        default_cost_method=row[8] or "moving_average",
        allow_suggestion=bool(row[9]),
        total_assets_cny=_decimal_or_none(row[10]),
        created_at=int(row[11] or 0),
        updated_at=int(row[12] or 0),
    ).validate()


def save_investment_profile(profile: InvestmentProfile) -> InvestmentProfile:
    profile = profile.validate()
    existing = get_investment_profile(profile.user_id)
    ts = now_ts()
    created_at = existing.created_at or ts

    def text(value: Decimal | None) -> str:
        return decimal_text(value) if value is not None else ""

    with database.db_lock:
        database.c.execute(
            """
            INSERT OR REPLACE INTO investment_profiles (
                user_id, investment_goal, risk_level, target_gold_allocation,
                max_single_buy_amount_cny, max_drawdown_tolerance,
                preferred_channels_json, report_style, alert_preference_json,
                default_cost_method, allow_suggestion, total_assets_cny,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile.user_id,
                profile.investment_goal,
                profile.risk_level,
                text(profile.target_gold_allocation),
                text(profile.max_single_buy_amount_cny),
                text(profile.max_drawdown_tolerance),
                json.dumps(profile.preferred_channels, ensure_ascii=False),
                profile.report_style,
                json.dumps(profile.alert_preference, ensure_ascii=False),
                profile.default_cost_method,
                int(profile.allow_suggestion),
                text(profile.total_assets_cny),
                created_at,
                ts,
            ),
        )
        database.conn.commit()
    record_audit(
        profile.user_id,
        "investment_profile.update",
        "investment_profile",
        profile.user_id,
        {"fields": sorted(profile.to_dict().keys())},
    )
    return get_investment_profile(profile.user_id)


def update_investment_profile(
    user_id: str, updates: dict[str, Any]
) -> InvestmentProfile:
    return save_investment_profile(
        get_investment_profile(user_id).with_updates(updates)
    )


def format_investment_profile(profile: InvestmentProfile) -> str:
    goal_names = {
        "": "未设置",
        "preservation": "保值",
        "long_term": "长期配置",
        "short_term": "短线交易",
        "hedging": "避险",
        "physical_collection": "实物收藏",
    }
    risk_names = {"low": "低", "medium": "中", "high": "高"}
    lines = [
        "【黄金投资档案】",
        f"投资目标：{goal_names.get(profile.investment_goal, profile.investment_goal)}",
        f"风险等级：{risk_names[profile.risk_level]}",
        "目标黄金仓位："
        + (
            f"{profile.target_gold_allocation:.2%}"
            if profile.target_gold_allocation is not None
            else "未设置"
        ),
        "单笔买入上限："
        + (
            f"{profile.max_single_buy_amount_cny:.2f} 元"
            if profile.max_single_buy_amount_cny is not None
            else "未设置"
        ),
        f"默认成本法：{profile.default_cost_method}",
        f"报告风格：{profile.report_style}",
        f"允许操作倾向建议：{'是' if profile.allow_suggestion else '否'}",
    ]
    if profile.preferred_channels:
        lines.append("偏好渠道：" + "、".join(profile.preferred_channels))
    return "\n".join(lines)
