from decimal import Decimal

from gold_agent.backtest.engine import run_backtest


def _rows():
    return [{"date": f"d{i}", "close": str(100 + i)} for i in range(40)]


def test_buy_and_hold_backtest_metrics():
    result = run_backtest(_rows(), "buy_and_hold", "10000", "0")
    assert result["final_value_cny"] == Decimal("13900.00")
    assert result["total_return"] == Decimal("0.39")
    assert result["trade_count"] == 1
