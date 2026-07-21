from __future__ import annotations

import json
from typing import Any

from gold_agent.infra import database
from gold_agent.infra.http import compact_text, now_ts


def record_audit(
    user_id: str,
    action: str,
    entity_type: str,
    entity_id: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    safe_details = details or {}
    with database.db_lock:
        database.c.execute(
            """
            INSERT INTO audit_logs
                (user_id, action, entity_type, entity_id, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                compact_text(user_id, 200),
                compact_text(action, 80),
                compact_text(entity_type, 80),
                compact_text(entity_id, 100),
                json.dumps(safe_details, ensure_ascii=False),
                now_ts(),
            ),
        )
        database.conn.commit()
