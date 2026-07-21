from __future__ import annotations

from decimal import Decimal
from typing import Any

from gold_agent.infra.decimal_utils import quantize_money, to_decimal


def value_batch_positions(
    lots: list[dict[str, Any]], market_price_cny: Any
) -> list[dict[str, Any]]:
    market_price = to_decimal(market_price_cny)
    if market_price is None or market_price <= Decimal("0"):
        raise ValueError("market_price_cny 必须大于 0")
    valued = []
    for source in lots:
        item = dict(source)
        quantity = to_decimal(item.get("remaining_grams"), Decimal("0")) or Decimal("0")
        unit_cost = to_decimal(item.get("unit_cost_cny"), Decimal("0")) or Decimal("0")
        item["market_value_cny"] = quantize_money(quantity * market_price)
        item["unrealized_pnl_cny"] = quantize_money(
            quantity * (market_price - unit_cost)
        )
        valued.append(item)
    return valued
