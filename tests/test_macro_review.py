from gold_agent.macro.service import build_factor_panel, interpret_factor
from gold_agent.review.service import analyze_trading_behavior


def test_macro_factor_interpretation():
    assert interpret_factor("dxy", 1.2)["direction"] == "bearish"
    panel = build_factor_panel(
        {
            "dxy": {"change": -1, "value": 100, "source": "fixture"},
            "central_bank_buying": {"change": 2, "value": 10, "source": "fixture"},
        }
    )
    assert panel["summary"] == "bullish"


def test_behavior_analysis_flags_fee_cost():
    findings = analyze_trading_behavior(
        [{"side": "buy", "unit_price_cny": "500", "trade_time": 1, "total_costs_cny": "12"}]
    )
    assert any("交易费用" in item for item in findings)
