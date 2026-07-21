from __future__ import annotations

from datetime import datetime
from typing import Any

from gold_agent.infra import database
from gold_agent.infra.http import now_ts


FACTOR_RULES = {
    "dxy": ("美元指数", "inverse"),
    "us_yield": ("美债收益率", "inverse"),
    "real_yield": ("实际利率", "inverse"),
    "cpi": ("美国 CPI", "mixed"),
    "nonfarm": ("非农就业", "mixed"),
    "fed_expectation": ("美联储利率预期", "inverse"),
    "usd_cny": ("人民币汇率", "positive_cny"),
    "oil": ("原油价格", "positive"),
    "gold_etf_holdings": ("黄金 ETF 持仓", "positive"),
    "central_bank_buying": ("央行购金", "positive"),
    "geopolitical_risk": ("地缘政治风险", "positive"),
}


def interpret_factor(key: str, change: float | None) -> dict[str, Any]:
    name, relation = FACTOR_RULES.get(key, (key, "mixed"))
    if change is None or change == 0:
        direction = "neutral"
    elif relation == "inverse":
        direction = "bearish" if change > 0 else "bullish"
    elif relation in {"positive", "positive_cny"}:
        direction = "bullish" if change > 0 else "bearish"
    else:
        direction = "uncertain"
    return {
        "factor": key,
        "name": name,
        "direction": direction,
        "strength": "high" if change is not None and abs(change) >= 1 else "medium",
        "change": change,
    }


def build_factor_panel(factors: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for key, payload in factors.items():
        row = interpret_factor(key, payload.get("change"))
        row.update(
            {
                "value": payload.get("value"),
                "source": payload.get("source", ""),
                "timestamp": payload.get("timestamp", ""),
            }
        )
        rows.append(row)
    bullish = sum(row["direction"] == "bullish" for row in rows)
    bearish = sum(row["direction"] == "bearish" for row in rows)
    return {
        "factors": rows,
        "summary": "bullish" if bullish > bearish else "bearish" if bearish > bullish else "mixed",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def add_macro_event(
    event_type: str,
    title: str,
    event_time: int,
    importance: str = "medium",
    source: str = "",
    details: dict[str, Any] | None = None,
) -> int:
    if importance not in {"low", "medium", "high"}:
        raise ValueError("importance 必须是 low、medium 或 high")
    import json

    with database.db_lock:
        database.c.execute(
            """
            INSERT INTO macro_events
                (event_type, title, event_time, importance, source, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_type, title, event_time, importance, source, json.dumps(details or {}, ensure_ascii=False), now_ts()),
        )
        event_id = int(database.c.lastrowid)
        database.conn.commit()
    return event_id


def list_macro_events(start_time: int, end_time: int) -> list[dict[str, Any]]:
    with database.db_lock:
        database.c.execute(
            """
            SELECT id, event_type, title, event_time, importance, source, details_json
            FROM macro_events
            WHERE event_time BETWEEN ? AND ?
            ORDER BY event_time, id
            """,
            (start_time, end_time),
        )
        rows = database.c.fetchall()
    return [
        {
            "id": row[0],
            "event_type": row[1],
            "title": row[2],
            "event_time": row[3],
            "importance": row[4],
            "source": row[5],
            "details_json": row[6],
        }
        for row in rows
    ]
