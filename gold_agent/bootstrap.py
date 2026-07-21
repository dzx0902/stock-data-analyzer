from gold_agent.infra.migrations import run_migrations
from gold_agent.ledger.service import ensure_alerts_schema, ensure_operational_schema
from gold_agent.memory.service import ensure_memory_schema


def initialize() -> None:
    run_migrations()
    ensure_alerts_schema()
    ensure_memory_schema()
    ensure_operational_schema()
