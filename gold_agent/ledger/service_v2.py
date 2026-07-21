from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from gold_agent.infra import database
from gold_agent.infra.decimal_utils import (
    ZERO,
    decimal_text,
    quantize_grams,
    quantize_money,
    quantize_price,
    to_decimal,
)
from gold_agent.infra.http import compact_text, json_loads_safe, now_ts
from gold_agent.ledger.cost_basis import (
    COST_METHODS,
    FEE_FIELDS,
    LedgerCalculationError,
    calculate_ledger,
    transaction_costs,
)
from gold_agent.ledger.realized_pnl import calculate_sale_proceeds
from gold_agent.market.prices import get_gold_quote
from gold_agent.security.audit import record_audit


LEDGER_COLUMNS = """
    id, side, quantity_grams, unit_price_cny, fee_cny, note, trade_time,
    account_type, channel, spread_cny, processing_fee_cny, tax_cny,
    delivery_fee_cny, storage_fee_cny, purity, batch_id, certificate_no,
    image_refs_json, cost_method, target_batch_id, cost_basis_cny,
    realized_pnl_cny
"""


def _row_to_transaction(row: tuple[Any, ...]) -> dict[str, Any]:
    values = dict(zip([name.strip() for name in LEDGER_COLUMNS.split(",")], row))
    for field in ("quantity_grams", "unit_price_cny", *FEE_FIELDS):
        values[field] = to_decimal(values.get(field), ZERO) or ZERO
    values["cost_basis_cny"] = to_decimal(values.get("cost_basis_cny"), ZERO) or ZERO
    values["realized_pnl_cny"] = to_decimal(values.get("realized_pnl_cny"), ZERO) or ZERO
    values["image_refs"] = json_loads_safe(values.pop("image_refs_json", "[]"), [])
    return values


def _load_transactions(user_id: str) -> list[dict[str, Any]]:
    with database.db_lock:
        database.c.execute(
            f"""
            SELECT {LEDGER_COLUMNS}
            FROM ledger_transactions_v2
            WHERE user_id = ?
            ORDER BY trade_time, id
            """,
            (user_id,),
        )
        return [_row_to_transaction(row) for row in database.c.fetchall()]


def _normalize_transaction(source: dict[str, Any]) -> dict[str, Any]:
    side = str(source.get("side", "")).strip().lower()
    quantity = to_decimal(source.get("quantity_grams"))
    price = to_decimal(source.get("unit_price_cny"))
    if side not in {"buy", "sell"}:
        raise LedgerCalculationError("交易方向必须是 buy 或 sell")
    if quantity is None or price is None or quantity <= ZERO or price <= ZERO:
        raise LedgerCalculationError("克重和每克价格必须大于 0")

    item: dict[str, Any] = {
        "side": side,
        "quantity_grams": quantize_grams(quantity),
        "unit_price_cny": quantize_price(price),
        "note": compact_text(str(source.get("note", "") or ""), 500),
        "trade_time": int(source.get("trade_time") or now_ts()),
        "account_type": compact_text(str(source.get("account_type", "") or ""), 100),
        "channel": compact_text(str(source.get("channel", "") or ""), 100),
        "purity": compact_text(str(source.get("purity", "") or ""), 50),
        "certificate_no": compact_text(str(source.get("certificate_no", "") or ""), 100),
        "image_refs": [
            compact_text(str(ref), 500)
            for ref in (source.get("image_refs") or [])
            if str(ref).strip()
        ][:20],
    }
    for field in FEE_FIELDS:
        value = to_decimal(source.get(field), ZERO) or ZERO
        if value < ZERO:
            raise LedgerCalculationError(f"{field} 不能为负数")
        item[field] = quantize_money(value)

    method = str(source.get("cost_method", "moving_average") or "moving_average")
    if method not in COST_METHODS:
        raise LedgerCalculationError(f"不支持的成本计算方式：{method}")
    item["cost_method"] = method
    item["target_batch_id"] = compact_text(
        str(source.get("target_batch_id", "") or ""), 100
    )
    item["batch_id"] = compact_text(str(source.get("batch_id", "") or ""), 100)
    if side == "buy" and not item["batch_id"]:
        item["batch_id"] = uuid.uuid4().hex
    return item


def get_ledger_state(user_id: str) -> dict[str, Any]:
    transactions = _load_transactions(user_id)
    state = calculate_ledger(transactions)
    for item in state["transactions"]:
        item["quantity_grams"] = quantize_grams(item["quantity_grams"])
        item["unit_price_cny"] = quantize_price(item["unit_price_cny"])
        for field in FEE_FIELDS:
            item[field] = quantize_money(item[field])
    return state


def get_batch_positions(user_id: str, market_price_cny: Any = None) -> list[dict[str, Any]]:
    state = calculate_ledger(_load_transactions(user_id), market_price_cny)
    return state["lots"]


def estimate_sale(
    user_id: str,
    quantity_grams: Any,
    unit_price_cny: Any,
    cost_method: str = "moving_average",
    target_batch_id: str = "",
    **costs: Any,
) -> dict[str, Any]:
    sale = _normalize_transaction(
        {
            "side": "sell",
            "quantity_grams": quantity_grams,
            "unit_price_cny": unit_price_cny,
            "cost_method": cost_method,
            "target_batch_id": target_batch_id,
            **costs,
        }
    )
    result = calculate_ledger([*_load_transactions(user_id), sale])
    calculated_sale = result["transactions"][-1]
    return {
        "quantity_grams": sale["quantity_grams"],
        "unit_price_cny": sale["unit_price_cny"],
        "cost_method": sale["cost_method"],
        "target_batch_id": sale["target_batch_id"],
        "gross_amount_cny": quantize_money(
            sale["quantity_grams"] * sale["unit_price_cny"]
        ),
        "total_costs_cny": quantize_money(transaction_costs(sale)),
        "net_proceeds_cny": calculate_sale_proceeds(sale),
        "cost_basis_cny": calculated_sale["cost_basis_cny"],
        "realized_pnl_cny": calculated_sale["realized_pnl_cny"],
        "remaining_grams": result["quantity_grams"],
    }


def add_gold_transaction(
    user_id: str,
    side: str,
    quantity_grams: Any,
    unit_price_cny: Any,
    fee_cny: Any = 0,
    note: str = "",
    **details: Any,
) -> tuple[bool, str]:
    transaction = {
        "side": side,
        "quantity_grams": quantity_grams,
        "unit_price_cny": unit_price_cny,
        "fee_cny": fee_cny,
        "note": note,
        **details,
    }
    ok, message = add_gold_transactions(user_id, [transaction])
    return (True, "已记账") if ok else (False, message)


def add_gold_transactions(
    user_id: str, transactions: list[dict[str, Any]]
) -> tuple[bool, str]:
    if not transactions:
        return False, "没有可入账的交易"
    try:
        normalized = [_normalize_transaction(item) for item in transactions]
        existing = _load_transactions(user_id)
        calculated = calculate_ledger([*existing, *normalized])
        calculated_new = calculated["transactions"][-len(normalized) :]
    except (LedgerCalculationError, ValueError, TypeError) as exc:
        return False, str(exc)

    with database.db_lock:
        try:
            created_at = now_ts()
            for item, result in zip(normalized, calculated_new):
                database.c.execute(
                    """
                    INSERT INTO ledger_transactions_v2 (
                        user_id, side, quantity_grams, unit_price_cny, fee_cny,
                        note, trade_time, created_at, account_type, channel,
                        spread_cny, processing_fee_cny, tax_cny,
                        delivery_fee_cny, storage_fee_cny, purity, batch_id,
                        certificate_no, image_refs_json, cost_method,
                        target_batch_id, cost_basis_cny, realized_pnl_cny
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        user_id,
                        item["side"],
                        decimal_text(item["quantity_grams"]),
                        decimal_text(item["unit_price_cny"]),
                        decimal_text(item["fee_cny"]),
                        item["note"],
                        item["trade_time"],
                        created_at,
                        item["account_type"],
                        item["channel"],
                        decimal_text(item["spread_cny"]),
                        decimal_text(item["processing_fee_cny"]),
                        decimal_text(item["tax_cny"]),
                        decimal_text(item["delivery_fee_cny"]),
                        decimal_text(item["storage_fee_cny"]),
                        item["purity"],
                        item["batch_id"],
                        item["certificate_no"],
                        json.dumps(item["image_refs"], ensure_ascii=False),
                        item["cost_method"],
                        item["target_batch_id"],
                        decimal_text(result["cost_basis_cny"]),
                        decimal_text(result["realized_pnl_cny"]),
                    ),
                )
            database.conn.commit()
        except Exception as exc:
            database.conn.rollback()
            return False, f"批量入账失败：{exc}"

    record_audit(
        user_id,
        "ledger.transaction.batch_create",
        "ledger_transaction",
        details={
            "count": len(normalized),
            "cost_methods": sorted({item["cost_method"] for item in normalized}),
        },
    )
    return True, f"已批量记账 {len(normalized)} 笔"


def undo_last_gold_transaction(user_id: str) -> str:
    with database.db_lock:
        database.c.execute(
            """
            SELECT id FROM ledger_transactions_v2
            WHERE user_id = ? ORDER BY trade_time DESC, id DESC LIMIT 1
            """,
            (user_id,),
        )
        row = database.c.fetchone()
        if not row:
            return "账本中没有可以撤销的记录。"
        database.c.execute(
            "DELETE FROM ledger_transactions_v2 WHERE id = ? AND user_id = ?",
            (row[0], user_id),
        )
        database.conn.commit()
    record_audit(
        user_id, "ledger.transaction.undo", "ledger_transaction", str(row[0])
    )
    return f"已撤销最近一笔记录（ID {row[0]}）。"


def format_ledger_summary(user_id: str, include_recent: bool = True) -> str:
    state = get_ledger_state(user_id)
    quantity = state["quantity_grams"]
    average = state["average_cost_cny_per_gram"]
    realized = state["realized_pnl_cny"]
    lines = [
        "【黄金账本】",
        f"当前持仓：{quantity:.4f} 克",
        f"库存成本：{state['inventory_cost_cny']:.2f} 元",
        f"持仓均价：{average:.4f} 元/克",
        f"累计已实现盈亏：{realized:+.2f} 元",
        f"当前批次数：{len(state['lots'])}",
    ]
    quote = get_gold_quote("sge_spot")
    if quantity > ZERO and quote.get("status") == "success":
        current = to_decimal(quote.get("price"))
        if current is not None:
            valued = calculate_ledger(state["transactions"], current)
            lines.append(f"参考现价：{current:.2f} 元/克")
            lines.append(f"浮动盈亏：{valued['unrealized_pnl_cny']:+.2f} 元")
            lines.append(
                f"累计盈亏：{realized + valued['unrealized_pnl_cny']:+.2f} 元"
            )
    if include_recent and state["transactions"]:
        lines.extend(["", "最近记录："])
        for item in reversed(state["transactions"][-8:]):
            side = "买入" if item["side"] == "buy" else "卖出"
            dt = datetime.fromtimestamp(item["trade_time"]).strftime("%Y-%m-%d %H:%M")
            suffix = (
                f"，已实现 {item['realized_pnl_cny']:+.2f} 元"
                if item["side"] == "sell"
                else f"，批次 {item['batch_id']}"
            )
            lines.append(
                f"- #{item['id']} {dt} {side} {item['quantity_grams']:.4f} 克，"
                f"{item['unit_price_cny']:.4f} 元/克，"
                f"总费用 {item['total_costs_cny']:.2f} 元{suffix}"
            )
    lines.append("成本法支持移动平均、FIFO 和指定批次；结果已计入各类交易费用。")
    return "\n".join(lines)
