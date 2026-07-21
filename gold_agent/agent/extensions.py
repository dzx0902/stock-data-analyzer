from __future__ import annotations

from typing import Any

from gold_agent.backtest.engine import run_backtest
from gold_agent.ledger.service import save_pending_operation
from gold_agent.macro.service import build_factor_panel, list_macro_events
from gold_agent.physical_gold.service import (
    add_physical_item,
    list_physical_items,
    value_physical_items,
)
from gold_agent.review.service import generate_period_review
from gold_agent.router.service import classify_intent, help_text
from gold_agent.strategy.service import (
    build_buy_plan,
    calculate_rebalance,
    create_strategy_alert,
)


def execute_extension(user_id: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
    if action == "help":
        return {"status": "success", "message": help_text(str(args.get("topic", "")))}
    if action == "classify_intent":
        return {"status": "success", **classify_intent(str(args.get("text", "")))}
    if action == "create_strategy_alert":
        alert_id = create_strategy_alert(
            user_id, str(args["rule_type"]), dict(args.get("parameters") or {})
        )
        return {"status": "success", "alert_id": alert_id}
    if action == "build_buy_plan":
        return {
            "status": "success",
            "plan": build_buy_plan(
                args.get("total_budget_cny"),
                list(args.get("levels") or []),
                args.get("target_grams"),
            ),
        }
    if action == "rebalance":
        return {
            "status": "success",
            "result": calculate_rebalance(
                args.get("gold_value_cny"),
                args.get("total_assets_cny"),
                args.get("target_allocation"),
            ),
        }
    if action == "macro_panel":
        return {"status": "success", "panel": build_factor_panel(dict(args.get("factors") or {}))}
    if action == "macro_events":
        return {
            "status": "success",
            "events": list_macro_events(int(args["start_time"]), int(args["end_time"])),
        }
    if action in {"weekly_review", "monthly_review"}:
        kind = "weekly" if action == "weekly_review" else "monthly"
        return {"status": "success", "review": generate_period_review(user_id, kind)}
    if action == "backtest":
        return {
            "status": "success",
            "backtest": run_backtest(
                list(args.get("rows") or []),
                str(args.get("strategy", "buy_and_hold")),
                args.get("initial_cash_cny", "10000"),
                args.get("fee_rate", "0.001"),
                dict(args.get("parameters") or {}),
            ),
        }
    if action == "physical_add":
        item_id = add_physical_item(
            user_id,
            str(args.get("name", "")),
            args.get("quantity_grams"),
            **dict(args.get("details") or {}),
        )
        return {"status": "success", "item_id": item_id}
    if action == "physical_list":
        return {"status": "success", "items": list_physical_items(user_id)}
    if action == "physical_value":
        return {
            "status": "success",
            "valuation": value_physical_items(
                user_id,
                args.get("market_price_cny_per_gram"),
                args.get("recycle_price_cny_per_gram"),
            ),
        }
    if action == "physical_delete":
        save_pending_operation(
            user_id, "physical.delete", {"item_id": int(args["item_id"])}
        )
        return {
            "status": "pending_confirmation",
            "message": "已生成删除草稿，请回复“确认操作”后执行。",
        }
    return {"status": "error", "message": f"未知扩展操作：{action}"}
