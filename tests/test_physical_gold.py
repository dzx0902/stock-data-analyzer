import importlib
from decimal import Decimal


def _load(monkeypatch):
    monkeypatch.setenv("GOLD_AGENT_DB", ":memory:")
    import gold_agent.config as config
    import gold_agent.infra.database as database
    import gold_agent.infra.migrations as migrations
    import gold_agent.physical_gold.service as service

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(migrations).run_migrations()
    return importlib.reload(service)


def test_physical_inventory_and_valuation(monkeypatch):
    service = _load(monkeypatch)
    item_id = service.add_physical_item(
        "physical",
        "建行金条",
        "50",
        purity="999.9",
        brand="建行",
        certificate_no="CERT",
        image_refs=["img-1"],
    )
    result = service.value_physical_items("physical", "600", "580")
    assert result["items"][0]["id"] == item_id
    assert result["items"][0]["image_refs"] == ["img-1"]
    assert result["total_market_value_cny"] == Decimal("29997.00")


def test_physical_delete_requires_confirmation(monkeypatch):
    service = _load(monkeypatch)
    import gold_agent.ledger.service as ledger

    ledger = importlib.reload(ledger)
    item_id = service.add_physical_item("physical-delete", "金条", "10")
    ledger.save_pending_operation(
        "physical-delete", "physical.delete", {"item_id": item_id}
    )

    assert len(service.list_physical_items("physical-delete")) == 1
    result = ledger.handle_pending_ledger_confirmation(
        "physical-delete", "确认操作"
    )

    assert "已删除" in result
    assert service.list_physical_items("physical-delete") == []
