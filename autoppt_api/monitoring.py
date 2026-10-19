"""Operational health and alerts: queue, stuck jobs, failures, workers, account limits, cost.

``health()`` is served at ``/api/admin/health`` and run by
``python -m autoppt_api.maintenance alerts``, which notifies administrators
when any alert is raised.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from autoppt_api.models import Job, JobPhase, Notification, UsageRecord, User, Worker, now
from autoppt_api.services import aware, iso

THRESHOLDS = {
    "queue_backlog": 20,           # runnable phases waiting
    "queue_wait_minutes": 60,      # oldest runnable phase
    "failure_rate": 0.2,           # failed / finished in 24h
    "worker_offline_minutes": 10,  # no heartbeat
    "phase_running_hours": 12,     # one phase running this long is stuck
    "estimate_overrun": 1.5,       # model + Office cost over the estimate's value (needs cost rates; 0 with the account pool)
    # Agent input tokens of one job.  Measured: the 9-page J-plan-real-001 used 44 M (93% cached) end to end.
    "job_input_tokens": 60_000_000,
}


def health(session: Session, *, lease_timeout_s: int = 900) -> dict[str, Any]:
    current = now()
    alerts: list[dict[str, Any]] = []
    runnable = session.scalars(select(Job).where(Job.pending_phase.is_not(None), Job.lease_worker_id.is_(None))).all()
    waiting = [job for job in runnable if not job.not_before or aware(job.not_before) <= current]
    deferred = [job for job in runnable if job.not_before and aware(job.not_before) > current]
    oldest = min((aware(job.updated_at) for job in waiting), default=None)
    wait_minutes = round((current - oldest).total_seconds() / 60, 1) if oldest else 0
    if len(waiting) > THRESHOLDS["queue_backlog"]:
        alerts.append({"kind": "queue_backlog", "detail": f"{len(waiting)} phases waiting"})
    if wait_minutes > THRESHOLDS["queue_wait_minutes"]:
        alerts.append({"kind": "queue_wait", "detail": f"oldest phase waiting {wait_minutes} min"})

    running = session.scalars(select(JobPhase).where(JobPhase.status == "running")).all()
    stuck = [p for p in running if aware(p.started_at) < current - timedelta(hours=THRESHOLDS["phase_running_hours"])]
    expired = session.scalars(select(Job).where(Job.lease_expires_at.is_not(None), Job.lease_expires_at < current)).all()
    if stuck:
        alerts.append({"kind": "stuck_phase", "detail": ", ".join(f"{p.job_id[:8]}:{p.phase}" for p in stuck)})
    if expired:
        alerts.append({"kind": "lease_expired", "detail": f"{len(expired)} jobs lost their worker (will be re-leased)"})

    since = current - timedelta(hours=24)
    # Counted in SQL: loading a day of finished jobs (with their JSON) held SQLite's write lock for seconds.
    finished_count, failed_count = session.execute(
        select(func.count(Job.id), func.coalesce(func.sum(case((Job.state == "failed", 1), else_=0)), 0))
        .where(Job.finished_at.is_not(None), Job.finished_at >= since)).one()
    finished_count, failed_count = int(finished_count), int(failed_count)
    rate = round(failed_count / finished_count, 3) if finished_count else 0.0
    if finished_count and rate > THRESHOLDS["failure_rate"]:
        alerts.append({"kind": "failure_rate", "detail": f"{failed_count}/{finished_count} jobs failed in 24h"})
    blocked = session.scalar(select(func.count(Job.id)).where(Job.state == "blocked")) or 0

    workers = []
    for worker in session.scalars(select(Worker).where(Worker.status != "disabled")):
        seen = aware(worker.last_heartbeat_at)
        offline = seen is None or seen < current - timedelta(minutes=THRESHOLDS["worker_offline_minutes"])
        workers.append({"name": worker.name, "capabilities": worker.capabilities, "template_version": worker.template_version,
                        "last_heartbeat_at": iso(worker.last_heartbeat_at), "offline": offline})
    online = [w for w in workers if not w["offline"]]
    if workers and not online:
        alerts.append({"kind": "no_worker_online", "detail": "no worker has sent a heartbeat recently"})
    if waiting and not online:
        alerts.append({"kind": "work_without_workers", "detail": f"{len(waiting)} phases waiting and no worker online"})
    if deferred:
        alerts.append({"kind": "account_limits", "detail": f"{len(deferred)} phases deferred by Codex usage limits"})
    costly = job_cost_alerts(session, since)
    if costly:
        alerts.append({"kind": "job_cost", "detail": "; ".join(costly)})
    versions = sorted({w["template_version"] for w in online if w["template_version"]})
    return {
        "generated_at": iso(current),
        "queue": {"waiting": len(waiting), "deferred": len(deferred), "oldest_wait_minutes": wait_minutes,
                  "by_phase": {phase: sum(1 for j in waiting if j.pending_phase == phase) for phase in sorted({j.pending_phase for j in waiting})}},
        "running_phases": len(running),
        "blocked_jobs": int(blocked),
        "finished_24h": finished_count,
        "failure_rate_24h": rate,
        "workers": workers,
        "template_versions_online": versions,
        "alerts": alerts,
        "thresholds": THRESHOLDS,
    }


def job_cost_alerts(session: Session, since) -> list[str]:
    """Jobs running or finished since ``since`` whose tokens or cost run past the thresholds.

    One grouped query over usage records that returns only jobs able to trip an alert: measured 2026-10-19 on
    soak databases, computing every job's cost took 8 s (9,000 jobs) and then 2 s (11,000 jobs, all rows fetched)
    under SQLite's write lock, stalling every request."""
    from autoppt_api.pricing import DEFAULT_RULES, active_rules

    _, rules = active_rules(session)
    rates = rules.get("cost_rates", DEFAULT_RULES["cost_rates"])
    value = float(rules.get("credit_value_cents", DEFAULT_RULES["credit_value_cents"]))
    recent = select(UsageRecord.job_id).where(UsageRecord.created_at >= since).distinct()  # jobs that spent recently
    agent = UsageRecord.kind == "agent_turn"
    input_sum = func.coalesce(func.sum(case((agent, UsageRecord.input_tokens), else_=0)), 0)
    cached_sum = func.coalesce(func.sum(case((agent, UsageRecord.cached_input_tokens), else_=0)), 0)
    output_sum = func.coalesce(func.sum(case((agent, UsageRecord.output_tokens), else_=0)), 0)
    office_sum = func.coalesce(func.sum(case((UsageRecord.kind == "office", UsageRecord.seconds), else_=0)), 0)
    spent = ((input_sum - cached_sum) * rates.get("input_per_million_cents", 0) + cached_sum * rates.get("cached_input_per_million_cents", 0)
             + output_sum * rates.get("output_per_million_cents", 0)) / 1_000_000.0 + office_sum / 60.0 * rates.get("office_minute_cents", 0)
    # No estimate is below the base fee, so a job cannot overrun before it spends 1.5x that: only those rows come back.
    floor = float(rules.get("base", DEFAULT_RULES["base"])) * value * THRESHOLDS["estimate_overrun"]
    rows = session.execute(
        select(UsageRecord.job_id, input_sum, spent).where(UsageRecord.job_id.in_(recent)).group_by(UsageRecord.job_id)
        .having((input_sum > THRESHOLDS["job_input_tokens"]) | (spent > floor))).all()
    estimates = dict(session.execute(select(Job.id, Job.estimate).where(Job.id.in_([row[0] for row in rows]))).all()) if rows else {}
    found = []
    for job_id, input_tokens, cost in rows:
        if input_tokens > THRESHOLDS["job_input_tokens"]:
            found.append(f"{job_id[:8]} used {input_tokens / 1e6:.1f} M input tokens")
        budget = float((estimates.get(job_id) or {}).get("total") or 0) * value
        if budget and cost > budget * THRESHOLDS["estimate_overrun"]:
            found.append(f"{job_id[:8]} cost {cost / 100:.2f} CNY against an estimate worth {budget / 100:.2f} CNY")
    return found


def notify_admins(session: Session, report: dict[str, Any]) -> int:
    if not report["alerts"]:
        return 0
    body = "\n".join(f"- {alert['kind']}: {alert['detail']}" for alert in report["alerts"])
    admins = session.scalars(select(User).where(User.is_admin.is_(True), User.status == "active")).all()
    for admin in admins:
        session.add(Notification(user_id=admin.id, subject=f"AutoPPT 告警：{len(report['alerts'])} 项", body=body))
    return len(admins)
