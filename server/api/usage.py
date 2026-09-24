"""Read-only model usage statistics for the local Avent runtime."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

from server.db import repos
from server.runtime import pricing

router = APIRouter(prefix="/api/usage", tags=["usage"])


def _range(start_date: str | None, end_date: str | None) -> tuple[str, str]:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    end = datetime.fromisoformat(end_date).date() if end_date else today
    start = datetime.fromisoformat(start_date).date() if start_date else end - timedelta(days=6)
    if start > end or (end - start).days > 90:
        raise ValueError("date range must be between 0 and 90 days")
    return start.isoformat(), end.isoformat()


def _query(start_date: str | None, end_date: str | None, model: str, agent_id: str) -> tuple[str, str, str, str]:
    try:
        start, end = _range(start_date, end_date)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return start, end, model.strip(), agent_id.strip()


@router.get("/overview")
def get_overview(
    start_date: str | None = None, end_date: str | None = None,
    model: str = "", agent_id: str = "",
):
    start, end, model, agent_id = _query(start_date, end_date, model, agent_id)
    data = repos.usage_overview(start, end, model=model, agent_id=agent_id)
    amount, unpriced_model_count = pricing.cost_total(
        pricing.apply_model_costs(repos.usage_by_model(start, end, model=model, agent_id=agent_id))
    )
    data.update({"spend_cny": amount, "unpriced_model_count": unpriced_model_count})
    return {"start_date": start, "end_date": end, "data": data}


@router.get("/trend")
def get_trend(
    start_date: str | None = None, end_date: str | None = None,
    model: str = "", agent_id: str = "",
):
    start, end, model, agent_id = _query(start_date, end_date, model, agent_id)
    return {"start_date": start, "end_date": end, "items": pricing.apply_model_costs(repos.usage_trend(start, end, model=model, agent_id=agent_id))}


@router.get("/by-model")
def get_by_model(
    start_date: str | None = None, end_date: str | None = None,
    model: str = "", agent_id: str = "",
):
    start, end, model, agent_id = _query(start_date, end_date, model, agent_id)
    return {"start_date": start, "end_date": end, "items": pricing.apply_model_costs(repos.usage_by_model(start, end, model=model, agent_id=agent_id))}


@router.get("/by-agent")
def get_by_agent(
    start_date: str | None = None, end_date: str | None = None,
    model: str = "", agent_id: str = "",
):
    start, end, model, agent_id = _query(start_date, end_date, model, agent_id)
    return {
        "start_date": start,
        "end_date": end,
        "items": pricing.apply_agent_costs(
            repos.usage_by_agent(start, end, model=model, agent_id=agent_id),
            repos.usage_by_agent_model(start, end, model=model, agent_id=agent_id),
        ),
    }


@router.get("/pricing-file")
def get_pricing_file():
    return {"path": str(pricing.ensure_pricing_file())}


@router.get("/runs/{run_id}")
def get_run_usage(run_id: str):
    summary = repos.get_run_usage_summary(run_id)
    if summary is None:
        raise HTTPException(404, "usage not found")
    return summary
