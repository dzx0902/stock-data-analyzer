from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from gold_agent.infra.decimal_utils import ZERO, to_decimal


INVESTMENT_GOALS = {
    "preservation",
    "long_term",
    "short_term",
    "hedging",
    "physical_collection",
}
RISK_LEVELS = {"low", "medium", "high"}
COST_METHODS = {"moving_average", "fifo", "specific_batch"}
REPORT_STYLES = {"concise", "detailed", "data_focused"}


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    parsed = to_decimal(value)
    if parsed is None:
        raise ValueError(f"无效数值：{value}")
    return parsed


@dataclass(frozen=True)
class InvestmentProfile:
    user_id: str
    investment_goal: str = ""
    risk_level: str = "medium"
    target_gold_allocation: Decimal | None = None
    max_single_buy_amount_cny: Decimal | None = None
    max_drawdown_tolerance: Decimal | None = None
    preferred_channels: list[str] = field(default_factory=list)
    report_style: str = "concise"
    alert_preference: dict[str, Any] = field(default_factory=dict)
    default_cost_method: str = "moving_average"
    allow_suggestion: bool = True
    total_assets_cny: Decimal | None = None
    created_at: int = 0
    updated_at: int = 0

    def validate(self) -> "InvestmentProfile":
        if not self.user_id.strip():
            raise ValueError("user_id 不能为空")
        if self.investment_goal and self.investment_goal not in INVESTMENT_GOALS:
            raise ValueError("不支持的投资目标")
        if self.risk_level not in RISK_LEVELS:
            raise ValueError("风险等级必须是 low、medium 或 high")
        if self.report_style not in REPORT_STYLES:
            raise ValueError("不支持的报告风格")
        if self.default_cost_method not in COST_METHODS:
            raise ValueError("不支持的默认成本法")
        for name in ("target_gold_allocation", "max_drawdown_tolerance"):
            value = getattr(self, name)
            if value is not None and (value < ZERO or value > Decimal("1")):
                raise ValueError(f"{name} 必须在 0 到 1 之间")
        for name in ("max_single_buy_amount_cny", "total_assets_cny"):
            value = getattr(self, name)
            if value is not None and value < ZERO:
                raise ValueError(f"{name} 不能为负数")
        return self

    def with_updates(self, updates: dict[str, Any]) -> "InvestmentProfile":
        normalized = dict(updates)
        for field_name in (
            "target_gold_allocation",
            "max_single_buy_amount_cny",
            "max_drawdown_tolerance",
            "total_assets_cny",
        ):
            if field_name in normalized:
                normalized[field_name] = _optional_decimal(normalized[field_name])
        if "allow_suggestion" in normalized:
            normalized["allow_suggestion"] = bool(normalized["allow_suggestion"])
        if "preferred_channels" in normalized:
            normalized["preferred_channels"] = [
                str(item).strip()
                for item in normalized["preferred_channels"]
                if str(item).strip()
            ][:20]
        return replace(self, **normalized).validate()

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "investment_goal": self.investment_goal,
            "risk_level": self.risk_level,
            "target_gold_allocation": self.target_gold_allocation,
            "max_single_buy_amount_cny": self.max_single_buy_amount_cny,
            "max_drawdown_tolerance": self.max_drawdown_tolerance,
            "preferred_channels": list(self.preferred_channels),
            "report_style": self.report_style,
            "alert_preference": dict(self.alert_preference),
            "default_cost_method": self.default_cost_method,
            "allow_suggestion": self.allow_suggestion,
            "total_assets_cny": self.total_assets_cny,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
