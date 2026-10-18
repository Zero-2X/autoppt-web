"""Per-job gross margin: revenue from settled credits minus model, Office and storage cost.

Credit value and cost rates come from the active pricing rules
(``credit_value_cents``, ``cost_rates``).  With the Codex account pool the
per-token rates are 0 (the subscription is a fixed cost); they become real
when D2 moves to API keys.  All amounts are CNY cents.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autoppt_api.ledger import job_revenue
from autoppt_api.models import Artifact, Job, UsageRecord, now
from autoppt_api.pricing import DEFAULT_RULES, active_rules


def job_cost(session: Session, job: Job, rates: dict[str, Any]) -> dict[str, Any]:
    """Tokens, Office minutes, storage and their cost in CNY cents for one job (rates from the pricing rules)."""
    usage = session.execute(
        select(func.coalesce(func.sum(UsageRecord.input_tokens), 0), func.coalesce(func.sum(UsageRecord.cached_input_tokens), 0),
               func.coalesce(func.sum(UsageRecord.output_tokens), 0))
        .where(UsageRecord.job_id == job.id, UsageRecord.kind == "agent_turn")
    ).one()
    office_seconds = float(session.scalar(select(func.coalesce(func.sum(UsageRecord.seconds), 0))
                                          .where(UsageRecord.job_id == job.id, UsageRecord.kind == "office")) or 0)
    stored = int(session.scalar(select(func.coalesce(func.sum(Artifact.size), 0)).where(Artifact.job_id == job.id)) or 0)
    input_tokens, cached, output = (int(x) for x in usage)
    model = ((input_tokens - cached) * rates.get("input_per_million_cents", 0) + cached * rates.get("cached_input_per_million_cents", 0)
             + output * rates.get("output_per_million_cents", 0)) / 1_000_000
    office = office_seconds / 60 * rates.get("office_minute_cents", 0)
    storage = stored / 1024**3 * rates.get("storage_gb_month_cents", 0) * job.retention_days / 30
    return {
        "tokens": {"input": input_tokens, "cached_input": cached, "output": output},
        "office_minutes": round(office_seconds / 60, 2), "stored_mb": round(stored / 1024**2, 2),
        "model_cents": round(model, 2), "office_cents": round(office, 2), "storage_cents": round(storage, 2),
    }


def margin_report(session: Session, *, days: int = 30) -> dict[str, Any]:
    _, rules = active_rules(session)
    value = float(rules.get("credit_value_cents", DEFAULT_RULES["credit_value_cents"]))
    rates = rules.get("cost_rates", DEFAULT_RULES["cost_rates"])
    since = now() - timedelta(days=days)
    rows = []
    totals = {"revenue_cents": 0.0, "model_cents": 0.0, "office_cents": 0.0, "storage_cents": 0.0, "margin_cents": 0.0}
    for job in session.scalars(select(Job).where(Job.finished_at.is_not(None), Job.finished_at >= since)):
        charged = job_revenue(session, job.id)
        revenue = charged * value
        row = {"job_id": job.id, "state": job.state, "credits_charged": charged, "revenue_cents": round(revenue, 2),
               **job_cost(session, job, rates)}
        row["margin_cents"] = round(revenue - row["model_cents"] - row["office_cents"] - row["storage_cents"], 2)
        for key in totals:
            totals[key] += row[key]
        rows.append(row)
    return {"days": days, "credit_value_cents": value, "cost_rates": rates, "jobs": rows,
            "totals": {k: round(v, 2) for k, v in totals.items()},
            "note": "Codex account-pool token cost is a fixed subscription and shows as 0 per token"}
