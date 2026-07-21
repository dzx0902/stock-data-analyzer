from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from gold_agent.infra import database
from gold_agent.infra.decimal_utils import ZERO, quantize_money, to_decimal
from gold_agent.infra.http import now_ts
from gold_agent.security.audit import record_audit
import time


RULE_TYPES = {
    "price_above",
    "price_below",
    "profit_rate_above",
    "loss_rate_below",
    "drawdown_exceed",
    "allocation_above",
    "allocation_below",
    "buy_zone",
    "sell_zone",
    "rebalance_needed",
    "volatility_spike",
    "price_change_percent",
}


def create_strategy_alert(
    user_id: str, rule_type: str, parameters: dict[str, Any]
) -> int:
    if rule_type not in RULE_TYPES:
        raise ValueError("不支持的策略提醒类型")
    ts = now_ts()
    with database.db_lock:
        database.c.execute(
            """
            INSERT INTO strategy_alerts
                (user_id, rule_type, parameters_json, enabled, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (user_id, rule_type, json.dumps(parameters, ensure_ascii=False, default=str), ts, ts),
        )
        alert_id = int(database.c.lastrowid)
        database.conn.commit()
    record_audit(user_id, "strategy_alert.create", "strategy_alert", str(alert_id))
    return alert_id


def evaluate_rule(rule_type: str, parameters: dict[str, Any], metrics: dict[str, Any]) -> bool:
    target = to_decimal(parameters.get("target"))
    if target is None:
        return False
    metric_map = {
        "price_above": ("price", lambda x: x >= target),
        "price_below": ("price", lambda x: x <= target),
        "profit_rate_above": ("profit_rate", lambda x: x >= target),
        "loss_rate_below": ("profit_rate", lambda x: x <= target),
        "drawdown_exceed": ("drawdown", lambda x: x >= target),
        "allocation_above": ("allocation", lambda x: x >= target),
        "allocation_below": ("allocation", lambda x: x <= target),
        "volatility_spike": ("volatility", lambda x: x >= target),
        "price_change_percent": ("price_change_percent", lambda x: abs(x) >= target),
    }
    if rule_type in {"buy_zone", "sell_zone"}:
        low = to_decimal(parameters.get("low"))
        high = to_decimal(parameters.get("high"))
        price = to_decimal(metrics.get("price"))
        return all(value is not None for value in (low, high, price)) and low <= price <= high
    if rule_type == "rebalance_needed":
        current = to_decimal(metrics.get("allocation"))
        desired = to_decimal(parameters.get("target_allocation"))
        tolerance = to_decimal(parameters.get("tolerance"), Decimal("0.02"))
        return current is not None and desired is not None and abs(current - desired) >= tolerance
    spec = metric_map.get(rule_type)
    value = to_decimal(metrics.get(spec[0])) if spec else None
    return bool(spec and value is not None and spec[1](value))


def build_buy_plan(
    total_budget_cny: Any,
    levels: list[dict[str, Any]],
    target_grams: Any = None,
) -> dict[str, Any]:
    budget = to_decimal(total_budget_cny)
    if budget is None or budget <= ZERO or not levels:
        raise ValueError("总预算和买入档位不能为空")
    weights = [to_decimal(item.get("weight")) for item in levels]
    if any(weight is None or weight <= ZERO for weight in weights):
        raise ValueError("每档权重必须大于 0")
    total_weight = sum(weights, ZERO)
    tranches = []
    for item, weight in zip(levels, weights):
        tranches.append(
            {
                "trigger_price_cny": to_decimal(item.get("trigger_price_cny")),
                "amount_cny": quantize_money(budget * weight / total_weight),
                "weight": weight / total_weight,
            }
        )
    return {
        "total_budget_cny": quantize_money(budget),
        "target_grams": to_decimal(target_grams),
        "tranches": tranches,
        "invalidation": "价格或风险承受能力发生显著变化时重新评估",
    }


def calculate_rebalance(
    gold_value_cny: Any, total_assets_cny: Any, target_allocation: Any
) -> dict[str, Any]:
    gold = to_decimal(gold_value_cny)
    total = to_decimal(total_assets_cny)
    target = to_decimal(target_allocation)
    if gold is None or total is None or target is None or total <= ZERO:
        raise ValueError("市值、总资产和目标占比必须有效")
    current = gold / total
    target_value = total * target
    delta = target_value - gold
    return {
        "current_allocation": current,
        "target_allocation": target,
        "deviation": current - target,
        "target_gold_value_cny": quantize_money(target_value),
        "adjustment_cny": quantize_money(delta),
        "action": "buy" if delta > ZERO else "sell" if delta < ZERO else "hold",
    }


def monitor_strategy_alerts(interval_seconds: int = 60) -> None:
    while True:
        try:
            with database.db_lock:
                database.c.execute(
                    """
                    SELECT id, user_id, rule_type, parameters_json
                    FROM strategy_alerts WHERE enabled = 1
                    ORDER BY id
                    """
                )
                rows = database.c.fetchall()
            for alert_id, user_id, rule_type, payload in rows:
                parameters = json.loads(payload or "{}")
                metrics: dict[str, Any] = {}
                if rule_type.startswith("price_") or rule_type in {"buy_zone", "sell_zone"}:
                    from gold_agent.market.prices import get_gold_quote

                    quote = get_gold_quote("sge_spot")
                    if quote.get("status") != "success":
                        continue
                    metrics["price"] = quote.get("price")
                if not evaluate_rule(rule_type, parameters, metrics):
                    continue
                from gold_agent.integrations.feishu import send_feishu_message

                send_feishu_message(
                    user_id,
                    f"【策略提醒触发】\n类型：{rule_type}\n条件：{parameters}\n"
                    "请结合个人风险承受能力判断，提醒不构成绝对买卖指令。",
                )
                with database.db_lock:
                    database.c.execute(
                        """
                        UPDATE strategy_alerts
                        SET enabled = 0, triggered_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (now_ts(), now_ts(), alert_id),
                    )
                    database.conn.commit()
        except Exception as exc:
            print(f"[策略提醒监控异常] {exc}")
        time.sleep(max(10, interval_seconds))
