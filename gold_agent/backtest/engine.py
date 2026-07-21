from __future__ import annotations

from decimal import Decimal
from decimal import InvalidOperation
from math import sqrt
from typing import Any

from gold_agent.infra.decimal_utils import ZERO, quantize_money, to_decimal


def _max_drawdown(values: list[Decimal]) -> Decimal:
    peak = values[0]
    worst = ZERO
    for value in values:
        peak = max(peak, value)
        if peak > ZERO:
            worst = min(worst, (value - peak) / peak)
    return worst


def run_backtest(
    rows: list[dict[str, Any]],
    strategy: str,
    initial_cash_cny: Any = "10000",
    fee_rate: Any = "0.001",
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if len(rows) < 2:
        raise ValueError("回测至少需要两条价格数据")
    parameters = parameters or {}
    cash = to_decimal(initial_cash_cny)
    fee = to_decimal(fee_rate)
    if cash is None or cash <= ZERO or fee is None or fee < ZERO:
        raise ValueError("初始资金或费率无效")
    initial = cash
    quantity = ZERO
    trades = 0
    values: list[Decimal] = []
    trade_returns: list[Decimal] = []
    entry_value: Decimal | None = None
    closes = [to_decimal(row.get("close")) for row in rows]
    if any(price is None or price <= ZERO for price in closes):
        raise ValueError("价格数据无效")

    for index, price in enumerate(closes):
        buy = sell = False
        if strategy == "dca":
            interval = int(parameters.get("interval", 20))
            buy = index % max(1, interval) == 0
        elif strategy == "moving_average":
            window = int(parameters.get("window", 20))
            if index >= window:
                average = sum(closes[index - window : index], ZERO) / window
                buy = price > average and quantity == ZERO
                sell = price < average and quantity > ZERO
        elif strategy == "rsi":
            window = int(parameters.get("window", 14))
            if index >= window:
                changes = [closes[j] - closes[j - 1] for j in range(index - window + 1, index + 1)]
                gains = sum((max(change, ZERO) for change in changes), ZERO)
                losses = sum((max(-change, ZERO) for change in changes), ZERO)
                rsi = Decimal("100") if losses == ZERO else Decimal("100") - Decimal("100") / (Decimal("1") + gains / losses)
                buy = rsi < Decimal(str(parameters.get("buy_rsi", 30))) and quantity == ZERO
                sell = rsi > Decimal(str(parameters.get("sell_rsi", 70))) and quantity > ZERO
        elif strategy == "grid":
            grid = to_decimal(parameters.get("grid_pct"), Decimal("0.03")) or Decimal("0.03")
            if index:
                change = (price - closes[index - 1]) / closes[index - 1]
                buy = change <= -grid
                sell = change >= grid and quantity > ZERO
        elif strategy == "buy_and_hold":
            buy = index == 0
        else:
            raise ValueError("不支持的回测策略")

        if buy and cash > ZERO:
            amount = cash if strategy in {"buy_and_hold", "moving_average", "rsi"} else min(cash, initial / Decimal("12"))
            acquired = amount * (Decimal("1") - fee) / price
            cash -= amount
            quantity += acquired
            trades += 1
            entry_value = amount
        if sell and quantity > ZERO:
            proceeds = quantity * price * (Decimal("1") - fee)
            cash += proceeds
            if entry_value:
                trade_returns.append(proceeds / entry_value - Decimal("1"))
            quantity = ZERO
            trades += 1
            entry_value = None
        values.append(cash + quantity * price)

    final_value = values[-1]
    total_return = final_value / initial - Decimal("1")
    daily_returns = [
        values[index] / values[index - 1] - Decimal("1")
        for index in range(1, len(values))
        if values[index - 1] > ZERO
    ]
    mean_return = sum(daily_returns, ZERO) / len(daily_returns) if daily_returns else ZERO
    variance = (
        sum(((value - mean_return) ** 2 for value in daily_returns), ZERO)
        / max(1, len(daily_returns) - 1)
    )
    volatility = Decimal(str(sqrt(float(variance)))) * Decimal(str(sqrt(252)))
    annualized = (
        (final_value / initial) ** (Decimal("252") / Decimal(str(len(rows)))) - Decimal("1")
    )
    sharpe = (
        mean_return / Decimal(str(sqrt(float(variance)))) * Decimal(str(sqrt(252)))
        if variance > ZERO
        else ZERO
    )
    benchmark_return = closes[-1] / closes[0] - Decimal("1")
    return {
        "strategy": strategy,
        "initial_cash_cny": quantize_money(initial),
        "final_value_cny": quantize_money(final_value),
        "total_return": total_return,
        "annualized_return": annualized,
        "max_drawdown": _max_drawdown(values),
        "annualized_volatility": volatility,
        "sharpe_ratio": sharpe,
        "win_rate": (
            Decimal(sum(value > ZERO for value in trade_returns))
            / Decimal(len(trade_returns))
            if trade_returns
            else None
        ),
        "trade_count": trades,
        "fee_rate": fee,
        "buy_and_hold_return": benchmark_return,
        "excess_return": total_return - benchmark_return,
    }
