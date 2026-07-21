from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from gold_agent.infra import database
from gold_agent.infra.decimal_utils import (
    ZERO,
    decimal_text,
    quantize_grams,
    quantize_money,
    to_decimal,
)
from gold_agent.infra.http import compact_text, json_loads_safe, now_ts
from gold_agent.security.audit import record_audit


def add_physical_item(
    user_id: str,
    name: str,
    quantity_grams: Any,
    **details: Any,
) -> int:
    quantity = to_decimal(quantity_grams)
    purchase_price = to_decimal(details.get("purchase_price_cny"))
    if not name.strip() or quantity is None or quantity <= ZERO:
        raise ValueError("物品名称和克重必须有效")
    ts = now_ts()
    with database.db_lock:
        database.c.execute(
            """
            INSERT INTO physical_gold_items (
                user_id, name, quantity_grams, purity, brand, channel,
                purchase_date, purchase_price_cny, certificate_no, invoice_no,
                storage_location, image_refs_json, note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                compact_text(name, 100),
                decimal_text(quantize_grams(quantity)),
                compact_text(str(details.get("purity", "") or ""), 50),
                compact_text(str(details.get("brand", "") or ""), 100),
                compact_text(str(details.get("channel", "") or ""), 100),
                compact_text(str(details.get("purchase_date", "") or ""), 20),
                decimal_text(purchase_price) if purchase_price is not None else "",
                compact_text(str(details.get("certificate_no", "") or ""), 100),
                compact_text(str(details.get("invoice_no", "") or ""), 100),
                compact_text(str(details.get("storage_location", "") or ""), 200),
                json.dumps(details.get("image_refs") or [], ensure_ascii=False),
                compact_text(str(details.get("note", "") or ""), 500),
                ts,
                ts,
            ),
        )
        item_id = int(database.c.lastrowid)
        database.conn.commit()
    record_audit(user_id, "physical_gold.create", "physical_gold_item", str(item_id))
    return item_id


def list_physical_items(user_id: str) -> list[dict[str, Any]]:
    with database.db_lock:
        database.c.execute(
            """
            SELECT id, name, quantity_grams, purity, brand, channel,
                   purchase_date, purchase_price_cny, certificate_no, invoice_no,
                   storage_location, image_refs_json, note
            FROM physical_gold_items WHERE user_id = ? ORDER BY id
            """,
            (user_id,),
        )
        rows = database.c.fetchall()
    return [
        {
            "id": row[0],
            "name": row[1],
            "quantity_grams": to_decimal(row[2], ZERO),
            "purity": row[3],
            "brand": row[4],
            "channel": row[5],
            "purchase_date": row[6],
            "purchase_price_cny": to_decimal(row[7]) if row[7] else None,
            "certificate_no": row[8],
            "invoice_no": row[9],
            "storage_location": row[10],
            "image_refs": json_loads_safe(row[11], []),
            "note": row[12],
        }
        for row in rows
    ]


def value_physical_items(
    user_id: str, market_price_cny_per_gram: Any, recycle_price_cny_per_gram: Any = None
) -> dict[str, Any]:
    market = to_decimal(market_price_cny_per_gram)
    recycle = to_decimal(recycle_price_cny_per_gram)
    if market is None or market <= ZERO:
        raise ValueError("当前金价必须大于 0")
    items = []
    total_market = ZERO
    total_recycle = ZERO
    for source in list_physical_items(user_id):
        item = dict(source)
        purity = to_decimal(item["purity"], Decimal("999.9"))
        purity_ratio = min(Decimal("1"), purity / Decimal("1000")) if purity else Decimal("1")
        fine_grams = item["quantity_grams"] * purity_ratio
        item["fine_gold_grams"] = quantize_grams(fine_grams)
        item["market_value_cny"] = quantize_money(fine_grams * market)
        item["recycle_value_cny"] = (
            quantize_money(fine_grams * recycle) if recycle is not None else None
        )
        total_market += item["market_value_cny"]
        if item["recycle_value_cny"] is not None:
            total_recycle += item["recycle_value_cny"]
        items.append(item)
    return {
        "items": items,
        "total_market_value_cny": quantize_money(total_market),
        "total_recycle_value_cny": quantize_money(total_recycle) if recycle is not None else None,
    }


def delete_physical_item(user_id: str, item_id: int) -> bool:
    with database.db_lock:
        database.c.execute(
            "DELETE FROM physical_gold_items WHERE id = ? AND user_id = ?",
            (item_id, user_id),
        )
        changed = database.c.rowcount > 0
        database.conn.commit()
    if changed:
        record_audit(user_id, "physical_gold.delete", "physical_gold_item", str(item_id))
    return changed
