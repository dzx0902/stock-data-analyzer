from decimal import Decimal

from gold_agent.market.converter import (
    TROY_OUNCE_GRAMS,
    cny_per_g_to_usd_per_oz,
    grams_to_ounces,
    ounces_to_grams,
    price_spread,
    usd_per_oz_to_cny_per_g,
)


def test_troy_ounce_and_gram_conversion():
    assert ounces_to_grams(1) == TROY_OUNCE_GRAMS
    assert grams_to_ounces(TROY_OUNCE_GRAMS) == Decimal("1")


def test_currency_unit_conversion_round_trip():
    cny_per_g = usd_per_oz_to_cny_per_g("3000", "7.2")
    restored = cny_per_g_to_usd_per_oz(cny_per_g, "7.2")

    assert cny_per_g == Decimal("694.4561")
    assert abs(restored - Decimal("3000")) < Decimal("0.001")


def test_price_spread_uses_decimal():
    absolute, percent = price_spread("525", "500")

    assert absolute == Decimal("25.0000")
    assert percent == Decimal("5.00")
