from decimal import Decimal

from gold_agent.strategy.service import (
    build_buy_plan,
    calculate_rebalance,
    evaluate_rule,
)


def test_strategy_rules_and_buy_plan():
    assert evaluate_rule("price_below", {"target": "500"}, {"price": "490"})
    assert evaluate_rule(
        "rebalance_needed",
        {"target": "0.15", "target_allocation": "0.15", "tolerance": "0.02"},
        {"allocation": "0.20"},
    )
    plan = build_buy_plan(
        "20000",
        [
            {"trigger_price_cny": "500", "weight": "1"},
            {"trigger_price_cny": "480", "weight": "2"},
        ],
    )
    assert sum(item["amount_cny"] for item in plan["tranches"]) == Decimal("20000.00")


def test_rebalance_calculation():
    result = calculate_rebalance("10000", "100000", "0.15")
    assert result["current_allocation"] == Decimal("0.1")
    assert result["adjustment_cny"] == Decimal("5000.00")
    assert result["action"] == "buy"
