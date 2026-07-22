# Finance PostgreSQL Migration

The Finance Agent keeps SQLite as its current production-compatible store because ledger, audit, strategy, and Feishu workflows share one established schema. Do not switch `GOLD_AGENT_DB` to PostgreSQL directly.

Migration order:

1. Export and checksum `user_profiles`, `investment_profiles`, and `audit_logs`.
2. Migrate `ledger_transactions_v2` and validate per-user quantity, weighted cost, and realized PnL against SQLite.
3. Migrate alerts, report subscriptions, and macro events.
4. Migrate memory and generated reports.
5. Enable PostgreSQL reads in shadow mode, then switch writes after two report cycles match.

Every migration must retain the SQLite backup and support an import rollback before changing the Feishu process.
