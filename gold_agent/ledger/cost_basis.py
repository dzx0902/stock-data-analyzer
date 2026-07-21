from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from gold_agent.infra.decimal_utils import (
    ZERO,
    quantize_grams,
    quantize_money,
    quantize_price,
    to_decimal,
)


COST_METHODS = {"moving_average", "fifo", "specific_batch"}
FEE_FIELDS = (
    "fee_cny",
    "spread_cny",
    "processing_fee_cny",
    "tax_cny",
    "delivery_fee_cny",
    "storage_fee_cny",
)


class LedgerCalculationError(ValueError):
    pass


@dataclass
class Lot:
    batch_id: str
    remaining_grams: Decimal
    unit_cost_cny: Decimal
    source_transaction_id: Any = None
    account_type: str = ""
    channel: str = ""
    purity: str = ""


def transaction_costs(transaction: dict[str, Any]) -> Decimal:
    return sum(
        (to_decimal(transaction.get(field), ZERO) or ZERO for field in FEE_FIELDS),
        ZERO,
    )


def _consume_lots(lots: list[Lot], quantity: Decimal, target_batch_id: str = "") -> Decimal:
    remaining = quantity
    allocated = ZERO
    candidates = lots
    if target_batch_id:
        candidates = [lot for lot in lots if lot.batch_id == target_batch_id]
        available = sum((lot.remaining_grams for lot in candidates), ZERO)
        if available < quantity:
            raise LedgerCalculationError(
                f"批次 {target_batch_id} 可用数量不足，仅剩 {available} 克"
            )
    for lot in candidates:
        if remaining <= ZERO:
            break
        used = min(remaining, lot.remaining_grams)
        allocated += used * lot.unit_cost_cny
        lot.remaining_grams -= used
        remaining -= used
    if remaining > ZERO:
        raise LedgerCalculationError("卖出数量超过当前持仓")
    return allocated


def _consume_lots_proportionally(lots: list[Lot], quantity: Decimal) -> None:
    active = [lot for lot in lots if lot.remaining_grams > ZERO]
    total = sum((lot.remaining_grams for lot in active), ZERO)
    if quantity > total:
        raise LedgerCalculationError("卖出数量超过当前持仓")
    ratio = quantity / total
    consumed = ZERO
    for index, lot in enumerate(active):
        if index == len(active) - 1:
            used = quantity - consumed
        else:
            used = lot.remaining_grams * ratio
            consumed += used
        lot.remaining_grams -= used


def calculate_ledger(
    transactions: Iterable[dict[str, Any]],
    market_price_cny: Any = None,
) -> dict[str, Any]:
    lots: list[Lot] = []
    quantity = ZERO
    inventory_cost = ZERO
    realized = ZERO
    calculated_transactions: list[dict[str, Any]] = []

    for source in transactions:
        item = dict(source)
        side = str(item.get("side", "")).lower()
        qty = to_decimal(item.get("quantity_grams"))
        price = to_decimal(item.get("unit_price_cny"))
        if side not in {"buy", "sell"} or qty is None or price is None or qty <= ZERO or price <= ZERO:
            raise LedgerCalculationError("交易方向、克重或价格无效")
        costs = transaction_costs(item)
        if costs < ZERO:
            raise LedgerCalculationError("交易费用不能为负数")

        if side == "buy":
            batch_id = str(item.get("batch_id", "")).strip()
            if not batch_id:
                raise LedgerCalculationError("买入交易缺少 batch_id")
            total_cost = qty * price + costs
            lots.append(
                Lot(
                    batch_id=batch_id,
                    remaining_grams=qty,
                    unit_cost_cny=total_cost / qty,
                    source_transaction_id=item.get("id"),
                    account_type=str(item.get("account_type", "") or ""),
                    channel=str(item.get("channel", "") or ""),
                    purity=str(item.get("purity", "") or ""),
                )
            )
            quantity += qty
            inventory_cost += total_cost
            cost_basis = total_cost
            transaction_pnl = ZERO
        else:
            if qty > quantity:
                raise LedgerCalculationError("卖出数量超过当前持仓")
            method = str(item.get("cost_method", "moving_average") or "moving_average")
            if method not in COST_METHODS:
                raise LedgerCalculationError(f"不支持的成本计算方式：{method}")
            average_cost = inventory_cost / quantity if quantity else ZERO
            target = str(item.get("target_batch_id", "") or "").strip()
            if method == "specific_batch" and not target:
                raise LedgerCalculationError("指定批次卖出必须提供 target_batch_id")

            if method == "moving_average":
                _consume_lots_proportionally(lots, qty)
                cost_basis = qty * average_cost
            else:
                cost_basis = _consume_lots(
                    lots, qty, target if method == "specific_batch" else ""
                )
            proceeds = qty * price - costs
            transaction_pnl = proceeds - cost_basis
            quantity -= qty
            inventory_cost -= cost_basis
            if quantity <= ZERO:
                quantity = ZERO
                inventory_cost = ZERO
            realized += transaction_pnl

        item["total_costs_cny"] = quantize_money(costs)
        item["cost_basis_cny"] = quantize_money(cost_basis)
        item["realized_pnl_cny"] = quantize_money(transaction_pnl)
        calculated_transactions.append(item)

    active_lots = []
    market_price = to_decimal(market_price_cny)
    for lot in lots:
        if lot.remaining_grams <= ZERO:
            continue
        unrealized = None
        if market_price is not None:
            unrealized = quantize_money(
                lot.remaining_grams * (market_price - lot.unit_cost_cny)
            )
        active_lots.append(
            {
                "batch_id": lot.batch_id,
                "remaining_grams": quantize_grams(lot.remaining_grams),
                "unit_cost_cny": quantize_price(lot.unit_cost_cny),
                "unrealized_pnl_cny": unrealized,
                "source_transaction_id": lot.source_transaction_id,
                "account_type": lot.account_type,
                "channel": lot.channel,
                "purity": lot.purity,
            }
        )

    average = inventory_cost / quantity if quantity else ZERO
    unrealized_total = (
        quantize_money(quantity * market_price - inventory_cost)
        if market_price is not None
        else None
    )
    return {
        "quantity_grams": quantize_grams(quantity),
        "inventory_cost_cny": quantize_money(inventory_cost),
        "average_cost_cny_per_gram": quantize_price(average),
        "realized_pnl_cny": quantize_money(realized),
        "unrealized_pnl_cny": unrealized_total,
        "lots": active_lots,
        "transactions": calculated_transactions,
    }
