from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from gold_agent.infra.decimal_utils import to_decimal
from gold_agent.market.converter import price_spread, usd_per_oz_to_cny_per_g
from gold_agent.market.model import PriceQuote


@dataclass(frozen=True)
class PriceComparison:
    label: str
    compared_price_cny_g: Decimal
    reference_price_cny_g: Decimal
    spread_cny_g: Decimal
    spread_pct: Decimal
    risk_level: str
    message: str


def compare_to_reference(
    label: str,
    compared_price_cny_g: Decimal | str,
    reference_price_cny_g: Decimal | str,
    warning_threshold_pct: Decimal | str = Decimal("5"),
) -> PriceComparison:
    compared = to_decimal(compared_price_cny_g)
    reference = to_decimal(reference_price_cny_g)
    threshold = to_decimal(warning_threshold_pct, Decimal("5")) or Decimal("5")
    if compared is None or reference is None:
        raise ValueError("comparison prices are required")
    spread, percent = price_spread(compared, reference)
    risk = "warning" if abs(percent) > threshold else "normal"
    direction = "溢价" if spread >= 0 else "折价"
    return PriceComparison(
        label=label,
        compared_price_cny_g=compared,
        reference_price_cny_g=reference,
        spread_cny_g=spread,
        spread_pct=percent,
        risk_level=risk,
        message=f"{label}相对基准{direction} {abs(spread):.4f} 元/克（{abs(percent):.2f}%）",
    )


def compare_international_to_sge(
    international: PriceQuote,
    sge: PriceQuote,
    usd_cny_rate: Decimal | str,
    warning_threshold_pct: Decimal | str = Decimal("3"),
) -> PriceComparison:
    if international.currency != "USD" or international.unit != "oz":
        raise ValueError("international quote must be USD/oz")
    if sge.currency != "CNY" or sge.unit != "g":
        raise ValueError("SGE quote must be CNY/g")
    converted = usd_per_oz_to_cny_per_g(international.price, usd_cny_rate)
    return compare_to_reference(
        "国际金价折算价",
        converted,
        sge.price,
        warning_threshold_pct,
    )
