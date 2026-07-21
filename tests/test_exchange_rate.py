from decimal import Decimal

import pytest

import gold_agent.market.exchange_rate as exchange_rate


def setup_function():
    exchange_rate.clear_exchange_rate_cache()


def test_live_provider_quote_is_cached():
    calls = {"count": 0}

    def provider():
        calls["count"] += 1
        return exchange_rate.ExchangeRateQuote(
            pair="USD/CNY",
            rate=Decimal("7.2015"),
            source="fixture-live",
            timestamp="2026-06-15T12:00:00+00:00",
            is_realtime=True,
        )

    first = exchange_rate.get_usd_cny_rate(providers=[provider])
    second = exchange_rate.get_usd_cny_rate(providers=[provider])

    assert first.rate == Decimal("7.2015")
    assert second.source == "fixture-live"
    assert calls["count"] == 1


def test_fallback_is_explicitly_marked(monkeypatch):
    monkeypatch.setattr(exchange_rate, "USD_CNY_FALLBACK_RATE", "7.19")

    def failed_provider():
        raise RuntimeError("provider unavailable")

    quote = exchange_rate.get_usd_cny_rate(
        force_refresh=True, providers=[failed_provider]
    )

    assert quote.rate == Decimal("7.19")
    assert quote.is_realtime is False
    assert quote.is_fallback is True


def test_missing_live_and_fallback_rate_fails_clearly(monkeypatch):
    monkeypatch.setattr(exchange_rate, "USD_CNY_FALLBACK_RATE", "")

    def failed_provider():
        raise RuntimeError("offline")

    with pytest.raises(RuntimeError, match="无法获取实时 USD/CNY 汇率"):
        exchange_rate.get_usd_cny_rate(
            force_refresh=True, providers=[failed_provider]
        )


def test_invalid_exchange_rate_is_rejected():
    assert exchange_rate._valid_rate("0") is None
    assert exchange_rate._valid_rate("20") is None
    assert exchange_rate._valid_rate("7.2") == Decimal("7.2")


def test_eastmoney_provider_parses_scaled_quote(monkeypatch):
    class Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "data": {
                    "f43": 72015,
                    "f59": 4,
                    "f86": 1781510400,
                    "f57": "USDCNY",
                }
            }

    monkeypatch.setattr(exchange_rate, "http_get", lambda *args, **kwargs: Response())

    quote = exchange_rate._eastmoney_provider()

    assert quote.rate == Decimal("7.2015")
    assert quote.is_realtime is True
    assert quote.source == "Eastmoney USDCNY"


def test_alltick_payload_rate_extraction():
    rate, node = exchange_rate._find_rate_in_payload(
        {"data": {"tick_list": [{"code": "USDCNY", "last_price": "7.2051"}]}}
    )

    assert rate == Decimal("7.2051")
    assert node["code"] == "USDCNY"
