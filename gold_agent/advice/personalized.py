from __future__ import annotations

from decimal import Decimal
from typing import Any

from gold_agent.infra.decimal_utils import ZERO, quantize_money, to_decimal
from gold_agent.ledger.service import get_ledger_state
from gold_agent.user_profile.model import InvestmentProfile
from gold_agent.user_profile.store import get_investment_profile


RISK_NOTICE = "风险提示：黄金价格会波动，以上仅为基于当前档案和账本的信息分析，不构成收益承诺或绝对买卖指令。"


def _profile_summary(profile: InvestmentProfile) -> dict[str, Any]:
    return {
        "investment_goal": profile.investment_goal or "not_set",
        "risk_level": profile.risk_level,
        "target_gold_allocation": profile.target_gold_allocation,
        "max_single_buy_amount_cny": profile.max_single_buy_amount_cny,
        "max_drawdown_tolerance": profile.max_drawdown_tolerance,
        "preferred_channels": profile.preferred_channels,
        "report_style": profile.report_style,
        "default_cost_method": profile.default_cost_method,
        "allow_suggestion": profile.allow_suggestion,
        "total_assets_cny": profile.total_assets_cny,
    }


def generate_personalized_advice(
    user_id: str,
    current_gold_price_cny: Any = None,
) -> dict[str, Any]:
    profile = get_investment_profile(user_id)
    ledger = get_ledger_state(user_id)
    price = to_decimal(current_gold_price_cny)
    gold_value = None
    allocation = None
    if price is not None and price > ZERO:
        gold_value = quantize_money(ledger["quantity_grams"] * price)
        if profile.total_assets_cny and profile.total_assets_cny > ZERO:
            allocation = gold_value / profile.total_assets_cny

    observations: list[str] = []
    suggestions: list[str] = []
    if ledger["quantity_grams"] <= ZERO:
        observations.append("当前账本没有黄金持仓。")
    else:
        observations.append(
            f"当前持仓 {ledger['quantity_grams']:.4f} 克，"
            f"账面均价 {ledger['average_cost_cny_per_gram']:.4f} 元/克。"
        )

    if allocation is not None:
        observations.append(f"按输入价格估算，黄金占总资产约 {allocation:.2%}。")
        target = profile.target_gold_allocation
        if target is not None:
            deviation = allocation - target
            if abs(deviation) <= Decimal("0.02"):
                suggestions.append("当前黄金占比接近目标仓位，可优先维持并定期复核。")
            elif deviation > ZERO:
                suggestions.append("当前黄金占比高于目标仓位，新增买入前应先评估集中度风险。")
            else:
                suggestions.append("当前黄金占比低于目标仓位，可结合价格和预算分批评估配置。")
    elif profile.target_gold_allocation is not None:
        observations.append("已设置目标仓位，但缺少总资产或当前估值价格，暂不能计算偏离度。")

    if profile.risk_level == "low":
        suggestions.extend(
            ["优先分批、控制单笔金额，并关注回撤和流动性。", "避免在短期快速上涨后集中追价。"]
        )
    elif profile.risk_level == "high":
        suggestions.append("可以观察波段机会，但仍应预设最大损失和仓位上限。")
    else:
        suggestions.append("采用分批配置并保留调整空间，避免单次投入过度集中。")

    if profile.investment_goal == "long_term":
        suggestions.append("长期配置应重点关注目标仓位、持有成本和再平衡纪律。")
    elif profile.investment_goal == "physical_collection":
        suggestions.append("实物黄金需额外比较品牌溢价、回收折价、证书和保管成本。")
    elif profile.investment_goal == "short_term":
        suggestions.append("短线交易应明确交易成本、止损条件和计划失效条件。")

    if profile.max_single_buy_amount_cny is not None:
        suggestions.append(
            f"单笔投入不应超过档案上限 {profile.max_single_buy_amount_cny:.2f} 元。"
        )
    if not profile.allow_suggestion:
        suggestions = ["用户档案设置为只提供信息，因此不输出操作倾向。"]

    return {
        "status": "success",
        "profile": _profile_summary(profile),
        "ledger": {
            "quantity_grams": ledger["quantity_grams"],
            "average_cost_cny_per_gram": ledger["average_cost_cny_per_gram"],
            "realized_pnl_cny": ledger["realized_pnl_cny"],
            "estimated_gold_value_cny": gold_value,
            "estimated_allocation": allocation,
        },
        "observations": observations,
        "suggestions": suggestions,
        "risk_notice": RISK_NOTICE,
    }


def format_personalized_advice(result: dict[str, Any]) -> str:
    lines = ["【个性化黄金分析】"]
    lines.extend(f"- {item}" for item in result["observations"])
    lines.append("建议侧重点：")
    lines.extend(f"- {item}" for item in result["suggestions"])
    lines.append(result["risk_notice"])
    return "\n".join(lines)
