"""Operational health and alerts: queue, stuck jobs, failures, workers, account limits, cost.

``health()`` is served at ``/api/admin/health`` and run by
``python -m autoppt_api.maintenance alerts``, which notifies administrators
when any alert is raised.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autoppt_api.models import Job, JobPhase, Notification, User, Worker, now
from autoppt_api.services import aware, iso

THRESHOLDS = {
    "queue_backlog": 20,           # runnable phases waiting
    "queue_wait_minutes": 60,      # oldest runnable phase
    "failure_rate": 0.2,           # failed / finished in 24h
    "worker_offline_minutes": 10,  # no heartbeat
    "phase_running_hours": 12,     # one phase running this long is stuck
    "estimate_overrun": 1.5,       # usage cost over the estimate (once cost rates exist)
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
    finished = session.scalars(select(Job).where(Job.finished_at.is_not(None), Job.finished_at >= since)).all()
    failed = [job for job in finished if job.state == "failed"]
    rate = round(len(failed) / len(finished), 3) if finished else 0.0
    if finished and rate > THRESHOLDS["failure_rate"]:
        alerts.append({"kind": "failure_rate", "detail": f"{len(failed)}/{len(finished)} jobs failed in 24h"})
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
    versions = sorted({w["template_version"] for w in online if w["template_version"]})
    return {
        "generated_at": iso(current),
        "queue": {"waiting": len(waiting), "deferred": len(deferred), "oldest_wait_minutes": wait_minutes,
                  "by_phase": {phase: sum(1 for j in waiting if j.pending_phase == phase) for phase in sorted({j.pending_phase for j in waiting})}},
        "running_phases": len(running),
        "blocked_jobs": int(blocked),
        "finished_24h": len(finished),
        "failure_rate_24h": rate,
        "workers": workers,
        "template_versions_online": versions,
        "alerts": alerts,
        "thresholds": THRESHOLDS,
    }


def notify_admins(session: Session, report: dict[str, Any]) -> int:
    if not report["alerts"]:
        return 0
    body = "\n".join(f"- {alert['kind']}: {alert['detail']}" for alert in report["alerts"])
    admins = session.scalars(select(User).where(User.is_admin.is_(True), User.status == "active")).all()
    for admin in admins:
        session.add(Notification(user_id=admin.id, subject=f"AutoPPT 告警：{len(report['alerts'])} 项", body=body))
    return len(admins)
