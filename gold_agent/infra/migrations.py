from __future__ import annotations

import json
from decimal import Decimal
from typing import Callable

from gold_agent.infra.database import c, conn, db_lock
from gold_agent.infra.http import now_ts


Migration = Callable[[], None]


def _table_columns(table: str) -> set[str]:
    c.execute(f"PRAGMA table_info({table})")
    return {str(row[1]) for row in c.fetchall()}


def _decimal_text(value: object, default: str = "0") -> str:
    try:
        return format(Decimal(str(value)), "f")
    except Exception:
        return default


def _migration_001_baseline() -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at INTEGER NOT NULL
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT DEFAULT '',
            details_json TEXT DEFAULT '{}',
            created_at INTEGER NOT NULL
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_user_time "
        "ON audit_logs(user_id, created_at, id)"
    )


def _migration_002_decimal_ledger() -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS ledger_transactions_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            legacy_id INTEGER UNIQUE,
            user_id TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
            quantity_grams TEXT NOT NULL,
            unit_price_cny TEXT NOT NULL,
            fee_cny TEXT NOT NULL DEFAULT '0',
            note TEXT DEFAULT '',
            trade_time INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_v2_user_time "
        "ON ledger_transactions_v2(user_id, trade_time, id)"
    )
    c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='gold_transactions'"
    )
    if c.fetchone():
        c.execute(
            """
            SELECT id, user_id, side, quantity_grams, unit_price_cny,
                   fee_cny, note, trade_time, created_at
            FROM gold_transactions
            ORDER BY id
            """
        )
        rows = c.fetchall()
        c.executemany(
            """
            INSERT OR IGNORE INTO ledger_transactions_v2
                (legacy_id, user_id, side, quantity_grams, unit_price_cny,
                 fee_cny, note, trade_time, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row[0],
                    row[1],
                    row[2],
                    _decimal_text(row[3]),
                    _decimal_text(row[4]),
                    _decimal_text(row[5]),
                    row[6] or "",
                    row[7],
                    row[8],
                )
                for row in rows
            ],
        )


def _migration_003_operations_and_message_state() -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            operation_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            source_message_id TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            confirmed_at INTEGER DEFAULT 0
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_operations_user_status "
        "ON pending_operations(user_id, status, created_at)"
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'processing',
            attempts INTEGER NOT NULL DEFAULT 1,
            last_error TEXT DEFAULT '',
            processed_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    columns = _table_columns("processed_messages")
    additions = {
        "status": "ALTER TABLE processed_messages ADD COLUMN status TEXT NOT NULL DEFAULT 'succeeded'",
        "attempts": "ALTER TABLE processed_messages ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1",
        "last_error": "ALTER TABLE processed_messages ADD COLUMN last_error TEXT DEFAULT ''",
        "updated_at": "ALTER TABLE processed_messages ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0",
    }
    for column, ddl in additions.items():
        if column not in columns:
            c.execute(ddl)
    c.execute(
        """
        UPDATE processed_messages
        SET status = CASE WHEN status IS NULL OR status = '' THEN 'succeeded' ELSE status END,
            updated_at = CASE WHEN updated_at IS NULL OR updated_at = 0 THEN processed_at ELSE updated_at END
        """
    )


def _migration_004_ledger_cost_basis() -> None:
    columns = _table_columns("ledger_transactions_v2")
    additions = {
        "account_type": "ALTER TABLE ledger_transactions_v2 ADD COLUMN account_type TEXT DEFAULT ''",
        "channel": "ALTER TABLE ledger_transactions_v2 ADD COLUMN channel TEXT DEFAULT ''",
        "spread_cny": "ALTER TABLE ledger_transactions_v2 ADD COLUMN spread_cny TEXT NOT NULL DEFAULT '0'",
        "processing_fee_cny": "ALTER TABLE ledger_transactions_v2 ADD COLUMN processing_fee_cny TEXT NOT NULL DEFAULT '0'",
        "tax_cny": "ALTER TABLE ledger_transactions_v2 ADD COLUMN tax_cny TEXT NOT NULL DEFAULT '0'",
        "delivery_fee_cny": "ALTER TABLE ledger_transactions_v2 ADD COLUMN delivery_fee_cny TEXT NOT NULL DEFAULT '0'",
        "storage_fee_cny": "ALTER TABLE ledger_transactions_v2 ADD COLUMN storage_fee_cny TEXT NOT NULL DEFAULT '0'",
        "purity": "ALTER TABLE ledger_transactions_v2 ADD COLUMN purity TEXT DEFAULT ''",
        "batch_id": "ALTER TABLE ledger_transactions_v2 ADD COLUMN batch_id TEXT DEFAULT ''",
        "certificate_no": "ALTER TABLE ledger_transactions_v2 ADD COLUMN certificate_no TEXT DEFAULT ''",
        "image_refs_json": "ALTER TABLE ledger_transactions_v2 ADD COLUMN image_refs_json TEXT DEFAULT '[]'",
        "cost_method": "ALTER TABLE ledger_transactions_v2 ADD COLUMN cost_method TEXT NOT NULL DEFAULT 'moving_average'",
        "target_batch_id": "ALTER TABLE ledger_transactions_v2 ADD COLUMN target_batch_id TEXT DEFAULT ''",
        "cost_basis_cny": "ALTER TABLE ledger_transactions_v2 ADD COLUMN cost_basis_cny TEXT NOT NULL DEFAULT '0'",
        "realized_pnl_cny": "ALTER TABLE ledger_transactions_v2 ADD COLUMN realized_pnl_cny TEXT NOT NULL DEFAULT '0'",
    }
    for column, ddl in additions.items():
        if column not in columns:
            c.execute(ddl)
    c.execute(
        """
        UPDATE ledger_transactions_v2
        SET batch_id = 'legacy-' || id
        WHERE side = 'buy' AND (batch_id IS NULL OR batch_id = '')
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_v2_user_batch "
        "ON ledger_transactions_v2(user_id, batch_id)"
    )


def _migration_005_investment_profiles() -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS investment_profiles (
            user_id TEXT PRIMARY KEY,
            investment_goal TEXT NOT NULL DEFAULT '',
            risk_level TEXT NOT NULL DEFAULT 'medium',
            target_gold_allocation TEXT DEFAULT '',
            max_single_buy_amount_cny TEXT DEFAULT '',
            max_drawdown_tolerance TEXT DEFAULT '',
            preferred_channels_json TEXT NOT NULL DEFAULT '[]',
            report_style TEXT NOT NULL DEFAULT 'concise',
            alert_preference_json TEXT NOT NULL DEFAULT '{}',
            default_cost_method TEXT NOT NULL DEFAULT 'moving_average',
            allow_suggestion INTEGER NOT NULL DEFAULT 1,
            total_assets_cny TEXT DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )


def _migration_006_remaining_features() -> None:
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS strategy_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            rule_type TEXT NOT NULL,
            parameters_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            triggered_at INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_alerts_user_enabled
            ON strategy_alerts(user_id, enabled);

        CREATE TABLE IF NOT EXISTS macro_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            event_time INTEGER NOT NULL,
            importance TEXT NOT NULL DEFAULT 'medium',
            source TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_macro_events_time
            ON macro_events(event_time, importance);

        CREATE TABLE IF NOT EXISTS physical_gold_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            quantity_grams TEXT NOT NULL,
            purity TEXT DEFAULT '',
            brand TEXT DEFAULT '',
            channel TEXT DEFAULT '',
            purchase_date TEXT DEFAULT '',
            purchase_price_cny TEXT DEFAULT '',
            certificate_no TEXT DEFAULT '',
            invoice_no TEXT DEFAULT '',
            storage_location TEXT DEFAULT '',
            image_refs_json TEXT NOT NULL DEFAULT '[]',
            note TEXT DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_physical_gold_user
            ON physical_gold_items(user_id, id);

        CREATE TABLE IF NOT EXISTS generated_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            review_type TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        """
    )


MIGRATIONS: list[tuple[int, str, Migration]] = [
    (1, "baseline_audit", _migration_001_baseline),
    (2, "decimal_ledger", _migration_002_decimal_ledger),
    (3, "operations_and_message_state", _migration_003_operations_and_message_state),
    (4, "ledger_cost_basis", _migration_004_ledger_cost_basis),
    (5, "investment_profiles", _migration_005_investment_profiles),
    (6, "remaining_features", _migration_006_remaining_features),
]


def run_migrations() -> None:
    with db_lock:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at INTEGER NOT NULL
            )
            """
        )
        applied = {int(row[0]) for row in c.execute("SELECT version FROM schema_migrations")}
        for version, name, migration in MIGRATIONS:
            if version in applied:
                continue
            try:
                migration()
                c.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, now_ts()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise


def migration_status() -> list[dict[str, object]]:
    with db_lock:
        c.execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
        return [
            {"version": row[0], "name": row[1], "applied_at": row[2]}
            for row in c.fetchall()
        ]
