from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from gold_agent.ledger.service import get_ledger_state


def analyze_trading_behavior(transactions: list[dict[str, Any]]) -> list[str]:
    findings: list[str] = []
    buys = [item for item in transactions if item["side"] == "buy"]
    sells = [item for item in transactions if item["side"] == "sell"]
    if len(transactions) >= 10:
        times = [int(item["trade_time"]) for item in transactions]
        if max(times) - min(times) <= 30 * 86400:
            findings.append("近 30 天交易频率较高，应检查手续费和决策一致性。")
    if len(buys) >= 3:
        prices = [Decimal(str(item["unit_price_cny"])) for item in buys]
        if prices[-1] > min(prices) * Decimal("1.08"):
            findings.append("近期买入价明显高于较早买入价，存在追高迹象。")
    total_fees = sum(
        (Decimal(str(item.get("total_costs_cny", 0))) for item in transactions),
        Decimal("0"),
    )
    if total_fees > Decimal("0"):
        findings.append(f"累计交易费用约 {total_fees:.2f} 元。")
    if not sells and len(buys) >= 5:
        findings.append("连续买入但没有减仓记录，应复核仓位上限和退出计划。")
    return findings or ["暂未发现明显的交易行为风险模式。"]


def generate_period_review(user_id: str, review_type: str = "weekly") -> dict[str, Any]:
    if review_type not in {"weekly", "monthly"}:
        raise ValueError("review_type 必须是 weekly 或 monthly")
    state = get_ledger_state(user_id)
    days = 7 if review_type == "weekly" else 31
    cutoff = int(datetime.now().timestamp()) - days * 86400
    period_transactions = [
        item for item in state["transactions"] if item["trade_time"] >= cutoff
    ]
    return {
        "review_type": review_type,
        "transaction_count": len(period_transactions),
        "current_quantity_grams": state["quantity_grams"],
        "average_cost_cny_per_gram": state["average_cost_cny_per_gram"],
        "realized_pnl_cny": state["realized_pnl_cny"],
        "behavior_findings": analyze_trading_behavior(period_transactions),
        "risk_notice": "历史行为分析不代表未来收益，交易计划应结合风险承受能力。",
    }
