import importlib
import json
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace


def _load_services(monkeypatch):
    monkeypatch.setenv("GOLD_AGENT_DB", ":memory:")
    import gold_agent.config as config
    import gold_agent.infra.database as database
    import gold_agent.infra.migrations as migrations
    import gold_agent.ledger.service as ledger

    importlib.reload(config)
    importlib.reload(database)
    migrations = importlib.reload(migrations)
    migrations.run_migrations()
    return importlib.reload(ledger), migrations, database


def test_migrations_are_versioned_and_idempotent(monkeypatch):
    _, migrations, database = _load_services(monkeypatch)

    migrations.run_migrations()
    status = migrations.migration_status()

    assert [item["version"] for item in status] == [1, 2, 3, 4, 5, 6]
    assert database.c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_decimal_ledger_and_confirmation_workflow(monkeypatch):
    ledger, _, database = _load_services(monkeypatch)
    ledger.ensure_alerts_schema()
    ledger.ensure_operational_schema()

    ledger.save_pending_operation(
        "u-confirm",
        "ledger.add",
        {
            "side": "buy",
            "quantity_grams": "0.1",
            "unit_price_cny": "500.1234",
            "fee_cny": "0.01",
            "note": "decimal test",
        },
    )

    assert ledger.get_ledger_state("u-confirm")["transactions"] == []
    result = ledger.handle_pending_ledger_confirmation("u-confirm", "确认记账")
    state = ledger.get_ledger_state("u-confirm")

    assert "已记账" in result
    assert state["quantity_grams"] == Decimal("0.1000")
    assert state["average_cost_cny_per_gram"] == Decimal("500.2234")
    stored = database.c.execute(
        "SELECT quantity_grams, unit_price_cny, fee_cny FROM ledger_transactions_v2"
    ).fetchone()
    assert stored == ("0.1000", "500.1234", "0.01")
    assert database.c.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] >= 2


def test_llm_ledger_tool_creates_draft_only(monkeypatch):
    ledger, _, _ = _load_services(monkeypatch)
    import gold_agent.agent.tools as agent_tools

    agent_tools = importlib.reload(agent_tools)
    threading.current_thread().current_user_id = "u-tool"
    response = json.loads(
        agent_tools.call_tool(
            "manage_gold_ledger",
            {
                "action": "add",
                "side": "buy",
                "quantity_grams": 1,
                "unit_price_cny": 500,
            },
        )
    )

    assert "草稿" in response["message"]
    assert ledger.get_ledger_state("u-tool")["transactions"] == []
    assert ledger.get_pending_ledger_entry("u-tool") is not None


def test_failed_message_can_be_retried(monkeypatch):
    ledger, _, _ = _load_services(monkeypatch)

    assert ledger.mark_message_processed("m1")
    assert not ledger.mark_message_processed("m1")
    ledger.fail_message_processing("m1", "temporary failure")
    assert ledger.mark_message_processed("m1")
    ledger.complete_message_processing("m1")
    assert not ledger.mark_message_processed("m1")


def test_word_report_runs_quality_gate(monkeypatch):
    _, _, _ = _load_services(monkeypatch)
    import gold_agent.reports.service as reports

    runtime = Path(__file__).parent / ".runtime"
    runtime.mkdir(exist_ok=True)
    monkeypatch.chdir(runtime)
    monkeypatch.setattr(
        reports,
        "auto_improve_report_content",
        lambda title, content: ("quality checked content", {"status": "pass"}),
    )
    threading.current_thread().current_user_id = "u-report"

    message = reports.create_word_report("Quality Test", "original")
    path = Path(threading.current_thread().uploaded_file_path)

    assert "Word" in message
    assert threading.current_thread().last_report_quality["status"] == "pass"
    assert path.exists()


def test_sqlite_backup_uses_online_backup_api(monkeypatch):
    import gold_agent.security.backup as backup

    runtime = Path(__file__).parent / ".runtime" / "backups"
    calls = {}

    class IntegrityResult:
        @staticmethod
        def fetchone():
            return ("ok",)

    class Destination:
        def execute(self, statement):
            calls["integrity_statement"] = statement
            return IntegrityResult()

        def close(self):
            calls["destination_closed"] = True

    class Source:
        def backup(self, destination):
            calls["backup_destination"] = destination

    destination = Destination()
    monkeypatch.setattr(backup, "conn", Source())
    monkeypatch.setattr(backup.sqlite3, "connect", lambda _: destination)
    monkeypatch.setattr(
        backup,
        "settings",
        SimpleNamespace(resolved_db_path=lambda: "source.db", backup_dir=runtime),
    )

    target = backup.create_backup(runtime)

    assert calls["backup_destination"] is destination
    assert calls["integrity_statement"] == "PRAGMA integrity_check"
    assert calls["destination_closed"] is True
    assert target.parent == runtime
