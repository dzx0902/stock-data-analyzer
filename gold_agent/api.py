from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel
import os
import requests

from gold_agent.agent.core import run_agent
from gold_agent.bootstrap import initialize
from gold_agent.ledger.service import build_scheduled_report, get_ledger_state


class AnalyzeRequest(BaseModel):
    query: str
    user_id: str = "default"


class ReportRequest(BaseModel):
    user_id: str = "default"
    slot: str = "morning"


app = FastAPI(title="Finance Agent", version="1.0.0")


def sync_report(report: str) -> None:
    base_url = os.getenv("KNOWLEDGE_SYNC_URL", "").strip().rstrip("/")
    if not base_url:
        return
    try:
        requests.post(
            f"{base_url}/v1/notes",
            json={"category": "finance", "filename": "finance-report-latest.md", "content": report, "tags": ["finance", "gold"]},
            timeout=20,
        ).raise_for_status()
    except requests.RequestException:
        return


@app.on_event("startup")
def startup() -> None:
    initialize()


@app.get("/health")
def health() -> dict[str, object]:
    return {"ok": True, "service": "finance-agent"}


@app.post("/api/finance/analyze")
def analyze(request: AnalyzeRequest) -> dict[str, str]:
    return {"ok": "true", "answer": run_agent(request.query, request.user_id)}


@app.get("/api/finance/portfolio")
def portfolio(user_id: str = "default") -> dict[str, object]:
    return {"ok": True, "portfolio": get_ledger_state(user_id)}


@app.post("/api/finance/report/today")
def today_report(request: ReportRequest) -> dict[str, str]:
    report = build_scheduled_report(request.user_id, request.slot)
    sync_report(report)
    return {"ok": "true", "report": report}
