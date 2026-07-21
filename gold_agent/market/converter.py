from __future__ import annotations

from decimal import Decimal

from gold_agent.infra.decimal_utils import quantize_price, to_decimal


TROY_OUNCE_GRAMS = Decimal("31.1034768")


def ounces_to_grams(ounces: Decimal | str | int) -> Decimal:
    value = to_decimal(ounces)
    if value is None:
        raise ValueError("invalid ounces")
    return value * TROY_OUNCE_GRAMS


def grams_to_ounces(grams: Decimal | str | int) -> Decimal:
    value = to_decimal(grams)
    if value is None:
        raise ValueError("invalid grams")
    return value / TROY_OUNCE_GRAMS


def usd_per_oz_to_cny_per_g(
    usd_per_oz: Decimal | str | int,
    usd_cny_rate: Decimal | str | int,
) -> Decimal:
    price = to_decimal(usd_per_oz)
    rate = to_decimal(usd_cny_rate)
    if price is None or rate is None or price <= 0 or rate <= 0:
        raise ValueError("price and exchange rate must be positive")
    return quantize_price(price * rate / TROY_OUNCE_GRAMS)


def cny_per_g_to_usd_per_oz(
    cny_per_g: Decimal | str | int,
    usd_cny_rate: Decimal | str | int,
) -> Decimal:
    price = to_decimal(cny_per_g)
    rate = to_decimal(usd_cny_rate)
    if price is None or rate is None or price <= 0 or rate <= 0:
        raise ValueError("price and exchange rate must be positive")
    return quantize_price(price * TROY_OUNCE_GRAMS / rate)


def price_spread(
    compared_price: Decimal | str | int,
    reference_price: Decimal | str | int,
) -> tuple[Decimal, Decimal]:
    compared = to_decimal(compared_price)
    reference = to_decimal(reference_price)
    if compared is None or reference is None or reference <= 0:
        raise ValueError("prices must be valid and reference must be positive")
    absolute = quantize_price(compared - reference)
    percent = ((compared - reference) / reference * Decimal("100")).quantize(Decimal("0.01"))
    return absolute, percent
