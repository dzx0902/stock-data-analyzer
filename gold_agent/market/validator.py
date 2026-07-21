from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from gold_agent.infra.decimal_utils import to_decimal
from gold_agent.market.model import PriceQuote


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    severity: str
    message: str


def _parse_timestamp(value: str, timezone: str) -> datetime | None:
    text = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=ZoneInfo(timezone))
        except ValueError:
            continue
    return None


def validate_quote(
    quote: PriceQuote,
    now: datetime | None = None,
    timezone: str = "Asia/Shanghai",
    realtime_max_age_seconds: int = 300,
    daily_max_age_seconds: int = 3 * 86400,
    previous_price: Decimal | str | None = None,
    jump_threshold_pct: Decimal | str = Decimal("10"),
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if quote.price <= 0:
        issues.append(ValidationIssue("price_nonpositive", "error", "价格必须大于 0"))
    if quote.currency not in {"CNY", "USD"} or quote.unit not in {"g", "oz"}:
        issues.append(ValidationIssue("unit_unknown", "error", "价格币种或单位不明确"))

    quote_time = _parse_timestamp(quote.timestamp, timezone)
    current = now or datetime.now(ZoneInfo(timezone))
    if quote_time is None:
        issues.append(ValidationIssue("timestamp_invalid", "warning", "时间戳无法解析"))
    else:
        max_age = realtime_max_age_seconds if quote.is_realtime else daily_max_age_seconds
        age = max(0, int((current - quote_time).total_seconds()))
        if age > max_age:
            issues.append(ValidationIssue("quote_stale", "warning", f"报价已过期 {age} 秒"))

    previous = to_decimal(previous_price)
    threshold = to_decimal(jump_threshold_pct, Decimal("10")) or Decimal("10")
    if previous is not None and previous > 0:
        jump = abs((quote.price - previous) / previous * Decimal("100"))
        if jump > threshold:
            issues.append(
                ValidationIssue(
                    "price_jump",
                    "warning",
                    f"单次价格跳变 {jump.quantize(Decimal('0.01'))}% 超过阈值 {threshold}%",
                )
            )
    return issues
