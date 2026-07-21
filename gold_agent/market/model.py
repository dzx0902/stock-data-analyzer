from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from gold_agent.infra.decimal_utils import decimal_text, to_decimal


@dataclass(frozen=True)
class PriceQuote:
    asset: str
    symbol: str
    price: Decimal
    currency: str
    unit: str
    source: str
    timestamp: str
    quote_type: str
    is_realtime: bool
    raw_payload: Any = None
    confidence_score: Decimal = Decimal("0.8")
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        price = to_decimal(self.price)
        confidence = to_decimal(self.confidence_score)
        if price is None or price <= 0:
            raise ValueError("price must be positive")
        if confidence is None or not Decimal("0") <= confidence <= Decimal("1"):
            raise ValueError("confidence_score must be between 0 and 1")
        if self.currency not in {"CNY", "USD"}:
            raise ValueError(f"unsupported currency: {self.currency}")
        if self.unit not in {"g", "oz"}:
            raise ValueError(f"unsupported unit: {self.unit}")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "confidence_score", confidence)

    @property
    def composite_unit(self) -> str:
        return f"{self.currency}/{self.unit}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "symbol": self.symbol,
            "price": decimal_text(self.price),
            "currency": self.currency,
            "unit": self.unit,
            "source": self.source,
            "timestamp": self.timestamp,
            "quote_type": self.quote_type,
            "is_realtime": self.is_realtime,
            "raw_payload": self.raw_payload,
            "confidence_score": decimal_text(self.confidence_score),
            "metadata": self.metadata,
        }

    def to_legacy_dict(self) -> dict[str, Any]:
        extra = dict(self.metadata)
        extra.setdefault("is_realtime", self.is_realtime)
        return {
            "status": "success",
            "asset": self.asset,
            "source": self.source,
            "symbol": self.symbol,
            "price": float(self.price),
            "currency": self.currency,
            "unit": self.composite_unit,
            "timestamp": self.timestamp,
            "quote_type": self.quote_type,
            "is_realtime": self.is_realtime,
            "raw_payload": self.raw_payload,
            "confidence": float(self.confidence_score),
            "confidence_score": decimal_text(self.confidence_score),
            "extra": extra,
        }

    @classmethod
    def from_legacy_dict(cls, payload: dict[str, Any]) -> "PriceQuote":
        composite_unit = str(payload.get("unit", ""))
        if "/" not in composite_unit:
            raise ValueError("legacy quote unit must include currency/unit")
        currency, unit = composite_unit.split("/", 1)
        return cls(
            asset=str(payload.get("asset") or "gold"),
            symbol=str(payload.get("symbol") or ""),
            price=to_decimal(payload.get("price")) or Decimal("0"),
            currency=currency,
            unit=unit,
            source=str(payload.get("source") or ""),
            timestamp=str(payload.get("timestamp") or ""),
            quote_type=str(payload.get("quote_type") or "spot"),
            is_realtime=bool(payload.get("is_realtime", False)),
            raw_payload=payload.get("raw_payload"),
            confidence_score=to_decimal(
                payload.get("confidence_score", payload.get("confidence", "0.8"))
            )
            or Decimal("0.8"),
            metadata=dict(payload.get("extra") or {}),
        )
