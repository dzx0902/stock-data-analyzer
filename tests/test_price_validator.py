from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from gold_agent.market.model import PriceQuote
from gold_agent.market.validator import validate_quote


def _quote(**overrides):
    values = {
        "asset": "gold",
        "symbol": "Au99.99",
        "price": Decimal("500"),
        "currency": "CNY",
        "unit": "g",
        "source": "test",
        "timestamp": "2026-06-15 10:00:00",
        "quote_type": "spot",
        "is_realtime": True,
        "raw_payload": {"price": 500},
        "confidence_score": Decimal("0.9"),
    }
    values.update(overrides)
    return PriceQuote(**values)


def test_quote_model_preserves_required_provenance():
    quote = _quote()
    payload = quote.to_dict()

    assert payload["source"] == "test"
    assert payload["timestamp"] == "2026-06-15 10:00:00"
    assert payload["raw_payload"] == {"price": 500}


def test_validator_detects_stale_and_jump():
    issues = validate_quote(
        _quote(),
        now=datetime(2026, 6, 15, 10, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
        previous_price="400",
        jump_threshold_pct="10",
    )
    codes = {issue.code for issue in issues}

    assert "quote_stale" in codes
    assert "price_jump" in codes
