"""Business logic shared by the HTTP routes: jobs, checkpoints, leases, settlement.

The backend reuses the Worker's contracts so both sides agree:
``validate_job_request`` for orders, ``phase_plan`` / ``can_transition`` for
the state machine, and ``validate_response`` for checkpoint answers.

Job life cycle on the backend: ``draft`` -> ``estimated`` -> ``paid`` ->
``queued`` (credits frozen), then the Worker leases one phase at a time and
reports the state it reached.  ``pending_phase`` is the runnable work; it is
empty while the job waits for a checkpoint answer, is blocked, or finished.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from autoppt_api import ledger
from autoppt_api.models import (
    Artifact,
    AuditLog,
    Checkpoint,
    Job,
    JobEvent,
    JobFile,
    JobPhase,
    LessonCandidate,
    Notification,
    StoredFile,
    UploadSession,
    UsageRecord,
    User,
    Worker,
    new_id,
    now,
)
from autoppt_api.pricing import EstimateError, active_rules, estimate
from autoppt_api.storage import kind_matches_name
from autoppt_service.checkpoint import validate_response
from autoppt_service.job_request import validate_job_request
from autoppt_service.status import CHECKPOINT_STATES, PHASE_STATES, REWORK_EDGES, TERMINAL_STATES, phase_plan

PHASE_FOR_STATE = {state: phase for phase, states in PHASE_STATES.items() for state in states}
PHASE_FOR_STATE["queued"] = "intake"
PHASE_CAPABILITY = {"intake": "intake", "planning": "codex", "masters": "codex", "reconstruct": "office", "package": "package"}
REWORK_AFTER_ANSWER = {"cp1_outline": "planning", "cp2_masters": "masters", "cp3_native_preview": "reconstructing"}
MAX_PHASE_ATTEMPTS = 3
DEFER_FALLBACK = timedelta(minutes=15)


class ServiceError(ValueError):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def iso(value: datetime | None) -> str | None:
    """UTC ISO 8601 with a Z suffix; SQLite returns naive datetimes that are UTC."""
    return aware(value).isoformat().replace("+00:00", "Z") if value is not None else None


def audit(session: Session, actor_type: str, actor_id: str, action: str, target: str = "", **data: Any) -> None:
    session.add(AuditLog(actor_type=actor_type, actor_id=actor_id or "", action=action, target=target, data=data))


def event(session: Session, job: Job, kind: str, message: str = "", **data: Any) -> None:
    session.add(JobEvent(job_id=job.id, kind=kind, message=message[:500], data=data))


def notify(session: Session, user_id: str, subject: str, body: str) -> None:
    session.add(Notification(user_id=user_id, subject=subject[:200], body=body))


def set_state(session: Session, job: Job, target: str, *, reason: str = "") -> None:
    if job.state != target:
        event(session, job, "state", reason, **{"from": job.state, "to": target})
        job.state = target


# ---------------------------------------------------------------- drafts and orders


def _owned_upload(session: Session, user_id: str, sha256: str) -> StoredFile | None:
    owned = session.scalars(
        select(UploadSession).where(UploadSession.user_id == user_id, UploadSession.sha256 == sha256,
                                    UploadSession.status == "complete")
    ).first()
    return session.get(StoredFile, sha256) if owned else None


def check_files(session: Session, user: User, request: dict[str, Any]) -> list[dict[str, str]]:
    """Every request file must be the user's own completed upload whose content matches its name."""
    errors = []
    for index, entry in enumerate(((request.get("inputs") or {}).get("files") or [])):
        path = f"inputs.files[{index}]"
        sha = str(entry.get("sha256") or "").lower()
        stored = _owned_upload(session, user.id, sha) if sha else None
        if stored is None:
            errors.append({"code": "upload_not_found", "path": f"{path}.sha256", "message": f"未找到已上传的文件：{entry.get('name')}"})
            continue
        if stored.scan_status == "infected":
            errors.append({"code": "file_infected", "path": path, "message": f"{entry.get('name')} 未通过安全扫描"})
        if not kind_matches_name(stored.sniffed_kind, str(entry.get("name", ""))):
            errors.append({"code": "file_type_mismatch", "path": f"{path}.name",
                           "message": f"{entry.get('name')} 的实际内容（{stored.sniffed_kind}）与扩展名不符"})
        entry["size_bytes"] = stored.size
    return errors


def create_draft(session: Session, user: User, request: dict[str, Any]) -> Job:
    job = Job(user_id=user.id, request=request, state="draft", service=str(request.get("service", "new_deck")),
              title=str(((request.get("cover") or {}).get("title")) or "")[:200])
    session.add(job)
    session.flush()
    event(session, job, "state", "draft created", to="draft")
    audit(session, "user", user.id, "job.create", job.id)
    return job


def update_draft(session: Session, job: Job, request: dict[str, Any]) -> Job:
    if job.state not in ("draft", "estimated"):
        raise ServiceError(409, "not_editable", "只有草稿或已估价的任务可以修改")
    job.request, job.estimate, job.normalized, job.plan = request, None, None, None
    job.title = str(((request.get("cover") or {}).get("title")) or "")[:200]
    set_state(session, job, "draft", reason="draft edited")
    return job


def estimate_job(session: Session, user: User, job: Job) -> dict[str, Any]:
    if job.state not in ("draft", "estimated"):
        raise ServiceError(409, "not_estimable", "任务已提交")
    request = dict(job.request)
    file_errors = check_files(session, user, request)
    result = validate_job_request(request)
    errors = file_errors + result.errors
    if errors:
        raise ServiceError(422, "invalid_request", "需求未通过校验", {"errors": errors, "warnings": result.warnings})
    version, rules = active_rules(session)
    try:
        quote = estimate(result.normalized, result.derived, rules, version=version)
    except EstimateError as exc:
        raise ServiceError(422, "cannot_estimate", str(exc)) from exc
    job.normalized = result.normalized
    job.plan = phase_plan(result.normalized)
    job.estimate = {**quote, "warnings": result.warnings, "derived": result.derived}
    job.retention_days = int(result.normalized["privacy"]["retention_days"])
    set_state(session, job, "estimated", reason="estimated")
    return job.estimate


def submit_job(session: Session, user: User, job: Job, *, confirm_total: int) -> Job:
    if job.state in ("paid", "queued") or job.submitted_at is not None:
        return job  # idempotent resubmit
    if job.state != "estimated" or not job.estimate:
        raise ServiceError(409, "not_estimated", "请先估价")
    if confirm_total != job.estimate["total"]:
        raise ServiceError(409, "price_changed", "确认金额与估价不一致，请重新估价", {"total": job.estimate["total"]})
    try:
        ledger.create_hold(session, user.id, job.id, job.estimate["hold"])
    except ledger.InsufficientCredits as exc:
        raise ServiceError(402, "insufficient_credits", "积分不足", {"needed": exc.needed, "available": exc.available}) from exc
    for entry in job.normalized["inputs"]["files"]:
        session.add(JobFile(job_id=job.id, file_id=entry["file_id"], role=entry["role"], name=entry["name"],
                            sha256=entry["sha256"].lower(), size=entry.get("size_bytes") or 0))
    set_state(session, job, "paid", reason=f"{job.estimate['hold']} credits frozen")
    set_state(session, job, "queued", reason="queued for intake")
    job.pending_phase = "intake"
    job.submitted_at = now()
    audit(session, "user", user.id, "job.submit", job.id, hold=job.estimate["hold"])
    return job


def cancel_job(session: Session, job: Job, *, actor: str) -> Job:
    if job.state in TERMINAL_STATES:
        return job
    released = ledger.release_hold(session, job.id)
    job.pending_phase, job.lease_worker_id, job.lease_token, job.lease_expires_at = None, None, None, None
    set_state(session, job, "cancelled", reason=f"cancelled; {released} frozen credits released")
    job.finished_at = now()
    audit(session, "user", actor, "job.cancel", job.id, released=released)
    return job


# ---------------------------------------------------------------- checkpoints


def open_checkpoint_for(session: Session, job: Job) -> Checkpoint | None:
    return session.scalars(
        select(Checkpoint).where(Checkpoint.job_id == job.id, Checkpoint.status == "open").order_by(Checkpoint.created_at.desc())
    ).first()


def answer_checkpoint(session: Session, user: User, job: Job, checkpoint_id: str, response: dict[str, Any], *, responder: str) -> Checkpoint:
    checkpoint = open_checkpoint_for(session, job)
    if checkpoint is None or checkpoint.checkpoint_id != checkpoint_id:
        raise ServiceError(409, "no_open_checkpoint", "没有待审核的检查点")
    rules = job.estimate or {}
    free_rounds = int(rules.get("free_rounds", 2))
    payload = {"schema_version": "autoppt-checkpoint-response-v1", **response, "checkpoint_id": checkpoint_id,
               "responder": responder, "answered_at": now().isoformat().replace("+00:00", "Z")}
    issues = validate_response(checkpoint.payload, payload, free_rounds=free_rounds)
    charge = 0
    if payload["decision"] == "redo_slides":
        charge += int(rules.get("redo_slide", 0)) * len(payload.get("redo_slide_ids") or [])
    if payload["decision"] != "approve" and checkpoint.round >= free_rounds:
        charge += int(rules.get("extra_round", 0))
    if charge and not payload.get("charge_confirmed"):
        issues.append(f"this answer costs {charge} credits; set charge_confirmed after showing the amount")
    if issues:
        raise ServiceError(422, "invalid_response", "回复未通过校验", {"issues": issues, "charge": charge})
    if charge:
        try:
            ledger.debit_user(session, job.user_id, charge, kind="settle", business_key=f"rework:{job.id}:{checkpoint_id}",
                              job_id=job.id, memo=f"{checkpoint.type} {payload['decision']}", actor=user.id)
        except ledger.InsufficientCredits as exc:
            raise ServiceError(402, "insufficient_credits", "积分不足", {"needed": exc.needed, "available": exc.available}) from exc
    checkpoint.response, checkpoint.status, checkpoint.answered_at = payload, "answered", now()
    if payload["decision"] == "approve" or job.state == "cp_v_text":
        index = job.plan.index(job.state)
        target = job.plan[index + 1]
    else:
        target = REWORK_AFTER_ANSWER[job.state]
    set_state(session, job, target, reason=f"{checkpoint_id}: {payload['decision']}")
    job.pending_phase = _pending_for(job, target)
    audit(session, "user" if responder == "user" else "admin", user.id, "checkpoint.answer", job.id,
          checkpoint=checkpoint_id, decision=payload["decision"], charge=charge)
    return checkpoint


def _pending_for(job: Job, state: str) -> str | None:
    if state == "image_deck_ready":
        index = job.plan.index(state)
        state = job.plan[index + 1] if index + 1 < len(job.plan) else state
    return PHASE_FOR_STATE.get(state)


# ---------------------------------------------------------------- worker side


def lease_job(session: Session, worker: Worker, *, timeout_s: int) -> Job | None:
    """Claim the oldest runnable phase this worker can run (optimistic, safe with several workers)."""
    current = now()
    worker.last_heartbeat_at = current
    capabilities = set(worker.capabilities or [])
    candidates = session.scalars(
        select(Job)
        .where(Job.pending_phase.is_not(None), Job.state.not_in(TERMINAL_STATES))
        .order_by(Job.submitted_at)
        .limit(50)
    ).all()
    for job in candidates:
        if "all" not in capabilities and PHASE_CAPABILITY[job.pending_phase] not in capabilities:
            continue
        if job.not_before and aware(job.not_before) > current:
            continue
        expires = aware(job.lease_expires_at)
        if expires and expires > current:
            continue
        token = new_id()
        claimed = session.execute(
            update(Job)
            .where(Job.id == job.id,
                   Job.lease_token.is_(None) if job.lease_token is None else Job.lease_token == job.lease_token)
            .values(lease_worker_id=worker.id, lease_token=token, lease_expires_at=current + timedelta(seconds=timeout_s))
        ).rowcount
        if claimed != 1:
            continue
        session.refresh(job)
        if expires is not None:
            for stale in session.scalars(select(JobPhase).where(JobPhase.job_id == job.id, JobPhase.status == "running")):
                stale.status, stale.finished_at = "expired", current
        attempt = 1 + len(session.scalars(select(JobPhase).where(JobPhase.job_id == job.id, JobPhase.phase == job.pending_phase)).all())
        session.add(JobPhase(job_id=job.id, phase=job.pending_phase, attempt=attempt, worker_id=worker.id))
        event(session, job, "lease", f"{job.pending_phase} leased by {worker.name}", phase=job.pending_phase)
        session.flush()
        return job
    return None


def require_lease(job: Job, worker: Worker, lease_token: str) -> None:
    if job.lease_worker_id != worker.id or job.lease_token != lease_token:
        raise ServiceError(409, "lease_lost", "lease is not held by this worker")
    if aware(job.lease_expires_at) < now():
        raise ServiceError(409, "lease_expired", "lease expired")


def heartbeat(session: Session, job: Job, worker: Worker, lease_token: str, *, timeout_s: int, progress: dict[str, Any] | None) -> Job:
    require_lease(job, worker, lease_token)
    job.lease_expires_at = now() + timedelta(seconds=timeout_s)
    worker.last_heartbeat_at = now()
    if progress:
        event(session, job, "progress", str(progress.get("message", ""))[:500],
              **{k: progress[k] for k in ("phase", "state", "progress", "current_slide", "warnings") if k in progress})
    return job


def _running_phase(session: Session, job: Job) -> JobPhase | None:
    return session.scalars(
        select(JobPhase).where(JobPhase.job_id == job.id, JobPhase.status == "running").order_by(JobPhase.started_at.desc())
    ).first()


def _reachable(job: Job, target: str) -> bool:
    if target in ("blocked", "failed") or target == job.state:
        return True
    if (job.state, target) in REWORK_EDGES:
        return True
    plan = job.plan or []
    return job.state in plan and target in plan and plan.index(target) > plan.index(job.state)


def complete_phase(session: Session, job: Job, worker: Worker, report: dict[str, Any]) -> Job:
    """Apply a phase report: new state, checkpoint, settlement, notifications; release the lease."""
    require_lease(job, worker, report["lease_token"])
    phase_row = _running_phase(session, job)
    phase = report["phase"]
    if phase != job.pending_phase:
        raise ServiceError(409, "wrong_phase", f"job expects {job.pending_phase}, got {phase}")
    target = report["job_state"]
    if not _reachable(job, target):
        raise ServiceError(422, "illegal_state", f"{job.state} -> {target} is not allowed")
    if report.get("template_version"):
        job.template_version = report["template_version"]
    if report.get("snapshot_artifact_id"):
        job.snapshot_artifact_id = report["snapshot_artifact_id"]
    for record in report.get("usage", []):
        _store_usage(session, job, record)
    for lesson in report.get("lesson_candidates", []):
        session.add(LessonCandidate(job_id=job.id, payload=lesson))
    job.lease_worker_id, job.lease_token, job.lease_expires_at = None, None, None

    if report.get("deferred"):
        job.not_before = now() + DEFER_FALLBACK
        event(session, job, "deferred", f"{phase} deferred: {report.get('reason', '')}", retry_after=report.get("retry_after"))
        if phase_row:
            phase_row.status, phase_row.finished_at, phase_row.result = "deferred", now(), report
        return job

    if phase_row:
        phase_row.status, phase_row.finished_at = "done", now()
        phase_row.result = {k: v for k, v in report.items() if k not in ("usage", "lease_token")}
    if target != "blocked":
        share = phase_share(job, phase, report.get("slides"))
        settled = ledger.settle(session, job.id, share, business_key=f"settle:{job.id}:{phase}", memo=f"{phase} done")
        if phase_row:
            phase_row.settled_credits = settled
    set_state(session, job, target, reason=report.get("reason", f"{phase} done"))
    job.not_before = None
    if target == "blocked":
        job.blocked_reason = str(report.get("blocked_reason", ""))[:2000]
        job.pending_phase = None
        notify(session, job.user_id, "任务需要你处理", f"任务 {job.title or job.id} 暂停：{job.blocked_reason}")
    elif target in CHECKPOINT_STATES:
        payload = report.get("checkpoint")
        if not payload:
            raise ServiceError(422, "checkpoint_missing", "a checkpoint state needs the checkpoint payload")
        existing = session.scalars(select(Checkpoint).where(Checkpoint.job_id == job.id,
                                                            Checkpoint.checkpoint_id == payload["checkpoint_id"])).first()
        if existing is None:
            session.add(Checkpoint(job_id=job.id, checkpoint_id=payload["checkpoint_id"], type=payload["type"],
                                   round=int(payload.get("round", 1)), payload=payload))
        job.pending_phase = None
        event(session, job, "checkpoint", f"{CHECKPOINT_STATES[target]} checkpoint ready", checkpoint_id=payload["checkpoint_id"])
        notify(session, job.user_id, "有待你审核的内容", f"任务 {job.title or job.id} 等待{CHECKPOINT_STATES[target]}审核")
    elif target == "delivered":
        released = ledger.release_hold(session, job.id)
        job.pending_phase, job.finished_at = None, now()
        event(session, job, "delivered", "delivered", released=released)
        notify(session, job.user_id, "任务已交付", f"任务 {job.title or job.id} 已交付")
    else:
        job.pending_phase = _pending_for(job, target)
    return job


def phase_share(job: Job, phase: str, slides: int | None) -> int:
    """Credits to settle for a finished phase.

    Per-slide phases are charged for the slides actually planned, never more than the
    estimate (which froze the upper end of the slide range); the rest of the hold is
    released at delivery.
    """
    estimate = job.estimate or {}
    share = int((estimate.get("phases") or {}).get(phase, 0))
    hold_slides = int(estimate.get("slides_for_hold") or 0)
    if phase in ("masters", "reconstruct") and slides and hold_slides:
        share = share * min(int(slides), hold_slides) // hold_slides
    return share


def fail_phase(session: Session, job: Job, worker: Worker, *, lease_token: str, error: str, retryable: bool) -> Job:
    require_lease(job, worker, lease_token)
    phase_row = _running_phase(session, job)
    if phase_row:
        phase_row.status, phase_row.finished_at, phase_row.result = "failed", now(), {"error": error[:2000]}
    attempts = len(session.scalars(select(JobPhase).where(JobPhase.job_id == job.id, JobPhase.phase == job.pending_phase,
                                                         JobPhase.status == "failed")).all())
    job.lease_worker_id, job.lease_token, job.lease_expires_at = None, None, None
    if retryable and attempts < MAX_PHASE_ATTEMPTS:
        job.not_before = now() + timedelta(minutes=2 ** attempts)
        event(session, job, "retry", f"{job.pending_phase} failed, retry {attempts + 1}: {error[:300]}")
        return job
    # A system failure: the user keeps what was not delivered.
    released = ledger.release_hold(session, job.id)
    set_state(session, job, "failed", reason=error[:300])
    job.pending_phase, job.finished_at = None, now()
    notify(session, job.user_id, "任务失败，未完成部分已退还", f"任务 {job.title or job.id} 失败；冻结的 {released} 积分已退还")
    audit(session, "worker", worker.id, "job.fail", job.id, error=error[:300], released=released)
    return job


def resume_blocked(session: Session, job: Job, *, actor: str, previous_state: str) -> Job:
    if job.state != "blocked":
        raise ServiceError(409, "not_blocked", "任务不在暂停状态")
    set_state(session, job, previous_state, reason="resumed")
    job.blocked_reason, job.pending_phase = "", _pending_for(job, previous_state)
    audit(session, "admin", actor, "job.resume", job.id, to=previous_state)
    return job


def _store_usage(session: Session, job: Job, record: dict[str, Any]) -> None:
    usage = record.get("usage") or {}
    key = f"usage:{job.id}:{record.get('phase')}:{record.get('timestamp')}:{record.get('kind')}:{record.get('thread_id', '')}"
    if session.scalars(select(UsageRecord).where(UsageRecord.business_key == key[:160])).first():
        return
    session.add(UsageRecord(
        job_id=job.id, phase=str(record.get("phase", "")), kind=str(record.get("kind") or "agent_turn"),
        profile=str(record.get("profile", "")), input_tokens=int(usage.get("input_tokens") or 0),
        cached_input_tokens=int(usage.get("cached_input_tokens") or 0), output_tokens=int(usage.get("output_tokens") or 0),
        seconds=float(record.get("duration_s") or record.get("seconds") or 0), business_key=key[:160], data=record,
    ))


def register_artifact(session: Session, job: Job, *, kind: str, path: str, sha256: str, size: int) -> Artifact:
    existing = session.scalars(select(Artifact).where(Artifact.job_id == job.id, Artifact.kind == kind, Artifact.path == path,
                                                      Artifact.sha256 == sha256)).first()
    if existing:
        return existing
    artifact = Artifact(job_id=job.id, kind=kind, path=path, sha256=sha256, size=size)
    session.add(artifact)
    session.flush()
    return artifact
