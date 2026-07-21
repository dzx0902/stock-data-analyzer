import importlib
from decimal import Decimal


def _load_ledger(monkeypatch):
    monkeypatch.setenv("GOLD_AGENT_DB", ":memory:")
    import gold_agent.config as config
    import gold_agent.infra.database as database
    import gold_agent.infra.migrations as migrations
    import gold_agent.ledger.service as ledger

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(migrations).run_migrations()
    return importlib.reload(ledger)


def test_extended_fields_and_real_pnl_are_persisted(monkeypatch):
    ledger = _load_ledger(monkeypatch)
    ok, _ = ledger.add_gold_transaction(
        "extended",
        "buy",
        "10",
        "500",
        fee_cny="5",
        processing_fee_cny="10",
        delivery_fee_cny="5",
        account_type="bank_bar",
        channel="CCB",
        purity="999.9",
        batch_id="batch-1",
        certificate_no="CERT-1",
        image_refs=["img-key-1"],
    )
    assert ok
    ok, _ = ledger.add_gold_transaction(
        "extended",
        "sell",
        "4",
        "550",
        fee_cny="2",
        spread_cny="3",
        cost_method="fifo",
    )
    assert ok

    state = ledger.get_ledger_state("extended")

    assert state["average_cost_cny_per_gram"] == Decimal("502.0000")
    assert state["realized_pnl_cny"] == Decimal("187.00")
    assert state["lots"][0]["remaining_grams"] == Decimal("6.0000")
    buy = state["transactions"][0]
    assert buy["account_type"] == "bank_bar"
    assert buy["certificate_no"] == "CERT-1"
    assert buy["image_refs"] == ["img-key-1"]


def test_invalid_batch_is_atomic_with_specific_lot_validation(monkeypatch):
    ledger = _load_ledger(monkeypatch)
    ok, _ = ledger.add_gold_transaction(
        "atomic", "buy", "1", "500", batch_id="A"
    )
    assert ok

    ok, message = ledger.add_gold_transactions(
        "atomic",
        [
            {
                "side": "buy",
                "quantity_grams": "2",
                "unit_price_cny": "510",
                "batch_id": "B",
            },
            {
                "side": "sell",
                "quantity_grams": "3",
                "unit_price_cny": "600",
                "cost_method": "specific_batch",
                "target_batch_id": "B",
            },
        ],
    )

    assert not ok
    assert "可用数量不足" in message
    state = ledger.get_ledger_state("atomic")
    assert len(state["transactions"]) == 1
    assert state["quantity_grams"] == Decimal("1.0000")


def test_sale_estimate_does_not_write_transaction(monkeypatch):
    ledger = _load_ledger(monkeypatch)
    ok, _ = ledger.add_gold_transaction(
        "estimate", "buy", "5", "500", fee_cny="5", batch_id="A"
    )
    assert ok

    result = ledger.estimate_sale(
        "estimate",
        quantity_grams="2",
        unit_price_cny="550",
        cost_method="fifo",
        fee_cny="2",
        spread_cny="3",
    )

    assert result["net_proceeds_cny"] == Decimal("1095.00")
    assert result["cost_basis_cny"] == Decimal("1002.00")
    assert result["realized_pnl_cny"] == Decimal("93.00")
    assert len(ledger.get_ledger_state("estimate")["transactions"]) == 1


def test_confirmation_preserves_extended_ledger_fields(monkeypatch):
    ledger = _load_ledger(monkeypatch)
    ledger.save_pending_operation(
        "confirm-fields",
        "ledger.add",
        {
            "side": "buy",
            "quantity_grams": "2",
            "unit_price_cny": "500",
            "processing_fee_cny": "8",
            "account_type": "bank_bar",
            "channel": "ICBC",
            "batch_id": "confirm-batch",
            "image_refs": ["image-key"],
        },
    )

    result = ledger.handle_pending_ledger_confirmation(
        "confirm-fields", "确认记账"
    )
    transaction = ledger.get_ledger_state("confirm-fields")["transactions"][0]

    assert "已记账" in result
    assert transaction["processing_fee_cny"] == Decimal("8.00")
    assert transaction["account_type"] == "bank_bar"
    assert transaction["batch_id"] == "confirm-batch"
    assert transaction["image_refs"] == ["image-key"]
