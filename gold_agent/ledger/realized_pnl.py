from __future__ import annotations

from typing import Any

from gold_agent.infra.decimal_utils import ZERO, quantize_money, to_decimal
from gold_agent.ledger.cost_basis import transaction_costs


def calculate_sale_proceeds(transaction: dict[str, Any]):
    quantity = to_decimal(transaction.get("quantity_grams"), ZERO) or ZERO
    price = to_decimal(transaction.get("unit_price_cny"), ZERO) or ZERO
    return quantize_money(quantity * price - transaction_costs(transaction))


def calculate_realized_pnl(transaction: dict[str, Any], cost_basis_cny: Any):
    basis = to_decimal(cost_basis_cny, ZERO) or ZERO
    return quantize_money(calculate_sale_proceeds(transaction) - basis)
