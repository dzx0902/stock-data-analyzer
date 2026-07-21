import importlib
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


def test_ledger_buy_sell_and_undo(monkeypatch):
    ledger = _load_ledger(monkeypatch)
    ledger.ensure_alerts_schema()
    ledger.ensure_operational_schema()

    ok, _ = ledger.add_gold_transaction("u1", "buy", 10, 500, 10)
    assert ok
    ok, _ = ledger.add_gold_transaction("u1", "sell", 4, 550, 2)
    assert ok

    state = ledger.get_ledger_state("u1")
    assert state["quantity_grams"] == 6
    assert state["average_cost_cny_per_gram"] == 501
    assert state["realized_pnl_cny"] == 194

    ledger.undo_last_gold_transaction("u1")
    assert ledger.get_ledger_state("u1")["quantity_grams"] == 10


def test_cannot_sell_more_than_current_holding(monkeypatch):
    ledger = _load_ledger(monkeypatch)
    ledger.ensure_alerts_schema()
    ledger.ensure_operational_schema()
    ledger.add_gold_transaction("u2", "buy", 2, 500)

    ok, _ = ledger.add_gold_transaction("u2", "sell", 3, 510)

    assert not ok


def test_batch_transactions_are_committed_together(monkeypatch):
    ledger = _load_ledger(monkeypatch)
    ledger.ensure_alerts_schema()
    ledger.ensure_operational_schema()

    ok, message = ledger.add_gold_transactions(
        "u3",
        [
            {"side": "buy", "quantity_grams": 2, "unit_price_cny": 500, "fee_cny": 0},
            {"side": "buy", "quantity_grams": 3, "unit_price_cny": 520, "fee_cny": 0},
        ],
    )

    assert ok
    assert "2" in message
    state = ledger.get_ledger_state("u3")
    assert len(state["transactions"]) == 2
    assert state["quantity_grams"] == 5


def test_invalid_batch_writes_nothing(monkeypatch):
    ledger = _load_ledger(monkeypatch)
    ledger.ensure_alerts_schema()
    ledger.ensure_operational_schema()

    ok, _ = ledger.add_gold_transactions(
        "u4",
        [
            {"side": "buy", "quantity_grams": 2, "unit_price_cny": 500},
            {"side": "sell", "quantity_grams": 3, "unit_price_cny": 520},
        ],
    )

    assert not ok
    assert ledger.get_ledger_state("u4")["transactions"] == []
