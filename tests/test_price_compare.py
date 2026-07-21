from decimal import Decimal

from gold_agent.market.compare import compare_international_to_sge, compare_to_reference
from gold_agent.market.model import PriceQuote


def test_compare_brand_premium_to_sge():
    result = compare_to_reference("品牌金价", "750", "700", "5")

    assert result.spread_cny_g == Decimal("50.0000")
    assert result.spread_pct == Decimal("7.14")
    assert result.risk_level == "warning"


def test_compare_international_conversion_to_sge():
    international = PriceQuote(
        asset="gold",
        symbol="XAUUSD",
        price=Decimal("3000"),
        currency="USD",
        unit="oz",
        source="test",
        timestamp="2026-06-15 10:00:00",
        quote_type="spot",
        is_realtime=True,
    )
    sge = PriceQuote(
        asset="gold",
        symbol="Au99.99",
        price=Decimal("700"),
        currency="CNY",
        unit="g",
        source="test",
        timestamp="2026-06-15 10:00:00",
        quote_type="spot",
        is_realtime=True,
    )

    result = compare_international_to_sge(international, sge, "7.2")

    assert result.compared_price_cny_g == Decimal("694.4561")
    assert result.spread_pct == Decimal("-0.79")
