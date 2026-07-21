from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


ZERO = Decimal("0")
GRAM_QUANTUM = Decimal("0.0001")
MONEY_QUANTUM = Decimal("0.01")
PRICE_QUANTUM = Decimal("0.0001")


def to_decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        text = str(value).strip().replace(",", "")
        if not text or text in {"-", "--", "null", "None"}:
            return default
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return default


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def quantize_grams(value: Decimal) -> Decimal:
    return value.quantize(GRAM_QUANTUM, rounding=ROUND_HALF_UP)


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def quantize_price(value: Decimal) -> Decimal:
    return value.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)
