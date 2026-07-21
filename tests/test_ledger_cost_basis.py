from decimal import Decimal

import pytest

from gold_agent.ledger.cost_basis import LedgerCalculationError, calculate_ledger


def test_moving_average_includes_all_buy_and_sell_costs():
    state = calculate_ledger(
        [
            {
                "side": "buy",
                "quantity_grams": "10",
                "unit_price_cny": "500",
                "fee_cny": "10",
                "batch_id": "A",
            },
            {
                "side": "sell",
                "quantity_grams": "4",
                "unit_price_cny": "550",
                "fee_cny": "2",
                "spread_cny": "3",
                "cost_method": "moving_average",
            },
        ]
    )

    assert state["quantity_grams"] == Decimal("6.0000")
    assert state["average_cost_cny_per_gram"] == Decimal("501.0000")
    assert state["realized_pnl_cny"] == Decimal("191.00")
    assert state["lots"][0]["remaining_grams"] == Decimal("6.0000")


def test_fifo_uses_oldest_lots_and_tracks_remaining_quantity():
    state = calculate_ledger(
        [
            {
                "side": "buy",
                "quantity_grams": "2",
                "unit_price_cny": "500",
                "batch_id": "A",
            },
            {
                "side": "buy",
                "quantity_grams": "3",
                "unit_price_cny": "600",
                "batch_id": "B",
            },
            {
                "side": "sell",
                "quantity_grams": "3",
                "unit_price_cny": "700",
                "fee_cny": "10",
                "cost_method": "fifo",
            },
        ]
    )

    assert state["realized_pnl_cny"] == Decimal("490.00")
    assert state["lots"] == [
        {
            "batch_id": "B",
            "remaining_grams": Decimal("2.0000"),
            "unit_cost_cny": Decimal("600.0000"),
            "unrealized_pnl_cny": None,
            "source_transaction_id": None,
            "account_type": "",
            "channel": "",
            "purity": "",
        }
    ]


def test_specific_batch_sale_uses_selected_batch():
    state = calculate_ledger(
        [
            {
                "side": "buy",
                "quantity_grams": "2",
                "unit_price_cny": "500",
                "batch_id": "A",
            },
            {
                "side": "buy",
                "quantity_grams": "3",
                "unit_price_cny": "600",
                "batch_id": "B",
            },
            {
                "side": "sell",
                "quantity_grams": "2",
                "unit_price_cny": "700",
                "cost_method": "specific_batch",
                "target_batch_id": "B",
            },
        ]
    )

    assert state["realized_pnl_cny"] == Decimal("200.00")
    remaining = {lot["batch_id"]: lot["remaining_grams"] for lot in state["lots"]}
    assert remaining == {"A": Decimal("2.0000"), "B": Decimal("1.0000")}


def test_specific_batch_rejects_insufficient_quantity():
    with pytest.raises(LedgerCalculationError, match="可用数量不足"):
        calculate_ledger(
            [
                {
                    "side": "buy",
                    "quantity_grams": "1",
                    "unit_price_cny": "500",
                    "batch_id": "A",
                },
                {
                    "side": "buy",
                    "quantity_grams": "2",
                    "unit_price_cny": "550",
                    "batch_id": "B",
                },
                {
                    "side": "sell",
                    "quantity_grams": "2",
                    "unit_price_cny": "600",
                    "cost_method": "specific_batch",
                    "target_batch_id": "A",
                },
            ]
        )
