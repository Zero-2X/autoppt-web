"""HTTP API (FastAPI).  ``uvicorn autoppt_api.app:create_app --factory``.

Routes:
  /api/options                       form choices (export_options)
  /api/auth/*                        email code login, refresh, logout
  /api/me, /api/credits*             account, balance, statement, redeem
  /api/uploads*                      chunked uploads with signed URLs, content sniffing, sha256 de-duplication
  /api/jobs*                         draft -> estimate -> submit -> status/events(SSE) -> checkpoint -> deliverables
  /api/files/{token}                 signed downloads
  /api/worker/*                      lease, heartbeat, artifacts, phase report, failure (worker token)
  /api/admin/*                       credits, redeem codes, workers, pricing, jobs, lessons, ledger check, audit
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autoppt_api import ledger
from autoppt_api.config import Settings
from autoppt_api.db import Base, make_engine, make_session_factory
from autoppt_api.models import (
    Artifact,
    AuditLog,
    Checkpoint,
    IdempotencyKey,
    Job,
    JobEvent,
    JobFile,
    JobPhase,
    LessonCandidate,
    Notification,
    PricingRule,
    RedeemCode,
    StoredFile,
    UploadSession,
    User,
    Worker,
    now,
)
from autoppt_api.security import (
    TokenError,
    consume_login_code,
    create_login_code,
    hash_secret,
    issue_tokens,
    normalize_email,
    recent_codes,
    rotate_refresh,
    sign_url_token,
    verify_jwt,
    verify_url_token,
)
from autoppt_api.services import (
    ServiceError,
    answer_checkpoint,
    audit,
    aware,
    iso,
    cancel_job,
    complete_phase,
    create_draft,
    estimate_job,
    fail_phase,
    heartbeat,
    lease_job,
    open_checkpoint_for,
    register_artifact,
    resume_blocked,
    submit_job,
    update_draft,
)
from autoppt_api.storage import LocalStorage, StorageError, sniff_kind
from autoppt_service.export_options import export_options

CHUNK_SIZE = 8 * 1024 * 1024
access_log = logging.getLogger("autoppt_api.access")
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


# ---------------------------------------------------------------- request bodies


class CodeRequest(BaseModel):
    email: str


class LoginRequest(BaseModel):
    email: str
    code: str


class RefreshRequest(BaseModel):
    refresh_token: str


class RedeemRequest(BaseModel):
    code: str


class UploadInit(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=1)
    sha256: str | None = Field(default=None, pattern="^[0-9a-fA-F]{64}$")


class DraftBody(BaseModel):
    request: dict[str, Any]


class SubmitBody(BaseModel):
    confirm_total: int


class ResponseBody(BaseModel):
    decision: str
    user_reply: str = ""
    annotations: list[dict[str, Any]] = []
    redo_slide_ids: list[str] = []
    answers: dict[str, Any] = {}
    charge_confirmed: bool = False
    reviewed_previews: list[str] = []


class LeaseBody(BaseModel):
    template_version: str = ""


class HeartbeatBody(BaseModel):
    lease_token: str
    progress: dict[str, Any] | None = None


class PhaseReport(BaseModel):
    lease_token: str
    phase: str
    job_state: str
    reason: str = ""
    blocked_reason: str = ""
    deferred: bool = False
    retry_after: str | None = None
    checkpoint: dict[str, Any] | None = None
    snapshot_artifact_id: str | None = None
    template_version: str = ""
    usage: list[dict[str, Any]] = []
    lesson_candidates: list[dict[str, Any]] = []


class FailReport(BaseModel):
    lease_token: str
    error: str
    retryable: bool = True


class AdjustBody(BaseModel):
    email: str
    credits: int
    memo: str = Field(min_length=1, max_length=300)


class RedeemCodesBody(BaseModel):
    credits: int = Field(gt=0)
    count: int = Field(default=1, ge=1, le=500)
    max_uses: int = Field(default=1, ge=1)
    expires_days: int | None = Field(default=None, ge=1)
    note: str = ""


class WorkerBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    capabilities: list[str] = ["intake", "codex", "package"]


class PricingBody(BaseModel):
    rules: dict[str, Any]
    note: str = ""


class LessonReview(BaseModel):
    status: str = Field(pattern="^(approved|rejected)$")


# ---------------------------------------------------------------- app factory


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)  # Alembic migrations take over in deployed environments
    factory = make_session_factory(engine)
    storage = LocalStorage(settings.storage_dir)
    app = FastAPI(title="AutoPPT API", version="0.1.0")
    app.state.settings, app.state.engine, app.state.session_factory, app.state.storage = settings, engine, factory, storage
    if settings.scan_command:
        from autoppt_api.maintenance import scanner_from_env

        app.state.scanner = scanner_from_env(settings.scan_command)

    @app.middleware("http")
    async def _access_log(request: Request, call_next):
        # Structured access log: one JSON line per request, no bodies, no tokens.
        request_id = request.headers.get("X-Request-ID") or secrets.token_hex(8)
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        if not request.url.path.startswith("/api/"):
            response.headers["Content-Security-Policy"] = CSP
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
        access_log.info(json.dumps({
            "request_id": request_id, "method": request.method, "path": request.url.path,
            "status": response.status_code, "ms": round((time.monotonic() - started) * 1000, 1),
        }))
        return response

    @app.exception_handler(ServiceError)
    async def _service_error(_request: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": exc.message, "details": exc.details})

    def db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def current_user(authorization: str = Header(default=""), session: Session = Depends(db)) -> User:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        try:
            payload = verify_jwt(authorization[7:], settings.secret_key, expected_type="access")
        except TokenError as exc:
            raise HTTPException(401, str(exc)) from exc
        user = session.get(User, payload["sub"])
        if user is None or user.status != "active":
            raise HTTPException(401, "account not active")
        return user

    def admin_user(user: User = Depends(current_user)) -> User:
        if not user.is_admin:
            raise HTTPException(403, "admin only")
        return user

    def current_worker(authorization: str = Header(default=""), session: Session = Depends(db)) -> Worker:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing worker token")
        worker = session.scalars(select(Worker).where(Worker.token_hash == hash_secret(authorization[7:]))).first()
        if worker is None or worker.status == "disabled":
            raise HTTPException(401, "unknown worker")
        return worker

    def own_job(job_id: str, user: User, session: Session) -> Job:
        job = session.get(Job, job_id)
        if job is None or (job.user_id != user.id and not user.is_admin) or job.state == "deleted":
            raise HTTPException(404, "job not found")
        return job

    def idempotent(session: Session, scope: str, key: str | None, body: Any, compute):
        """Replay the stored response for a repeated Idempotency-Key with the same body."""
        if not key:
            return compute()
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
        full = f"{scope}:{key}"[:160]
        stored = session.get(IdempotencyKey, full)
        if stored is not None:
            if stored.request_hash != digest:
                raise HTTPException(422, "Idempotency-Key reused with a different body")
            return JSONResponse(status_code=stored.status_code, content=stored.response)
        result = compute()
        session.add(IdempotencyKey(key=full, request_hash=digest, status_code=200, response=json.loads(json.dumps(result, default=str))))
        return result

    def download_url(sha256: str, name: str) -> str:
        token = sign_url_token(settings.secret_key, purpose="download", ref=f"{sha256}|{name}", ttl_s=settings.download_url_ttl_s)
        return f"/api/files/{token}"

    def job_view(session: Session, job: Job, *, detail: bool = False) -> dict[str, Any]:
        hold = ledger.job_hold(session, job.id)
        view = {
            "id": job.id, "state": job.state, "title": job.title, "service": job.service,
            "pending_phase": job.pending_phase, "estimate": job.estimate, "blocked_reason": job.blocked_reason,
            "hold": {"amount": hold.amount, "remaining": hold.remaining, "status": hold.status} if hold else None,
            "created_at": iso(job.created_at), "submitted_at": iso(job.submitted_at) if job.submitted_at else None,
            "finished_at": iso(job.finished_at) if job.finished_at else None,
        }
        if detail:
            view["request"] = job.request
            view["phases"] = [
                {"phase": p.phase, "attempt": p.attempt, "status": p.status, "settled_credits": p.settled_credits,
                 "started_at": iso(p.started_at), "finished_at": iso(p.finished_at) if p.finished_at else None}
                for p in session.scalars(select(JobPhase).where(JobPhase.job_id == job.id).order_by(JobPhase.started_at))
            ]
            checkpoint = open_checkpoint_for(session, job)
            view["open_checkpoint"] = checkpoint.checkpoint_id if checkpoint else None
            view["deliverables"] = [
                {"path": a.path, "size": a.size, "sha256": a.sha256, "url": download_url(a.sha256, a.path.rsplit("/", 1)[-1])}
                for a in session.scalars(select(Artifact).where(Artifact.job_id == job.id, Artifact.kind == "deliverable"))
            ] if job.state == "delivered" else []
        return view

    # ------------------------------------------------------------ public

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/options")
    def options() -> dict[str, Any]:
        return export_options()

    @app.get("/api/pricing")
    def pricing(session: Session = Depends(db)) -> dict[str, Any]:
        from autoppt_api.pricing import active_rules

        version, rules = active_rules(session)
        return {"version": version, "rules": rules,
                "note": "测试期价格，P1 实测后调整" if rules.get("provisional") else ""}

    # ------------------------------------------------------------ auth

    @app.post("/api/auth/code")
    def request_code(body: CodeRequest, request: Request, session: Session = Depends(db)) -> dict[str, Any]:
        try:
            email = normalize_email(body.email)
        except ValueError as exc:
            raise HTTPException(422, "invalid email") from exc
        ip = request.client.host if request.client else ""
        if recent_codes(session, email=email) >= settings.login_codes_per_hour or recent_codes(session, ip=ip) >= settings.login_codes_per_hour * 4:
            raise HTTPException(429, "too many codes requested; try again later")
        code = create_login_code(session, email, ttl_s=settings.login_code_ttl_s, ip=ip)
        user = session.scalars(select(User).where(User.email == email)).first()
        if user is not None:
            session.add(Notification(user_id=user.id, subject="AutoPPT 登录验证码", body=f"验证码 {code}，10 分钟内有效。"))
        result: dict[str, Any] = {"sent": True, "expires_in": settings.login_code_ttl_s}
        if settings.dev_echo_codes:
            result["dev_code"] = code
        return result

    @app.post("/api/auth/login")
    def login(body: LoginRequest, session: Session = Depends(db)) -> dict[str, Any]:
        try:
            email = normalize_email(body.email)
        except ValueError as exc:
            raise HTTPException(422, "invalid email") from exc
        if not consume_login_code(session, email, body.code, max_attempts=settings.login_code_max_attempts):
            session.commit()  # keep the attempt count even though the request fails
            raise HTTPException(401, "invalid or expired code")
        user = session.scalars(select(User).where(User.email == email)).first()
        if user is None:
            user = User(email=email, is_admin=email in settings.admin_emails)
            session.add(user)
            session.flush()
            ledger.user_account(session, user.id)
            if settings.signup_bonus_credits > 0:
                ledger.credit_user(session, user.id, settings.signup_bonus_credits, kind="grant",
                                   business_key=f"signup:{user.id}", source="grants", memo="signup bonus")
            audit(session, "user", user.id, "user.signup", user.id)
        if user.status != "active":
            raise HTTPException(403, "account disabled")
        return issue_tokens(session, user.id, secret=settings.secret_key, access_ttl=settings.access_token_ttl_s,
                            refresh_ttl=settings.refresh_token_ttl_s)

    @app.post("/api/auth/refresh")
    def refresh(body: RefreshRequest, session: Session = Depends(db)) -> dict[str, Any]:
        try:
            return rotate_refresh(session, body.refresh_token, secret=settings.secret_key,
                                  access_ttl=settings.access_token_ttl_s, refresh_ttl=settings.refresh_token_ttl_s)
        except TokenError as exc:
            session.commit()  # keep the family revocation
            raise HTTPException(401, str(exc)) from exc

    @app.post("/api/auth/logout")
    def logout(body: RefreshRequest, session: Session = Depends(db)) -> dict[str, bool]:
        from autoppt_api.models import RefreshToken

        try:
            payload = verify_jwt(body.refresh_token, settings.secret_key, expected_type="refresh")
        except TokenError:
            return {"ok": True}
        record = session.get(RefreshToken, payload["jti"])
        if record:
            record.revoked = True
        return {"ok": True}

    # ------------------------------------------------------------ account and credits

    @app.get("/api/me")
    def me(user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        account = ledger.user_account(session, user.id)
        return {"id": user.id, "email": user.email, "is_admin": user.is_admin, "locale": user.locale,
                "credits": {"balance": account.balance, "available": ledger.available(session, account)}}

    @app.get("/api/credits")
    def credits(user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        account = ledger.user_account(session, user.id)
        return {"balance": account.balance, "available": ledger.available(session, account),
                "frozen": ledger.frozen(session, account), "statement": ledger.statement(session, user.id)}

    @app.post("/api/credits/redeem")
    def redeem(body: RedeemRequest, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        code = session.scalars(select(RedeemCode).where(RedeemCode.code_hash == hash_secret(body.code.strip().upper()))).first()
        if code is None or (code.expires_at and aware(code.expires_at) < now()):
            raise HTTPException(404, "invalid or expired code")
        key = f"redeem:{code.id}:{user.id}"
        from autoppt_api.models import CreditTransaction

        if session.scalars(select(CreditTransaction).where(CreditTransaction.business_key == key)).first() is None:
            if code.uses >= code.max_uses:
                raise HTTPException(409, "code fully used")
            code.uses += 1
        ledger.credit_user(session, user.id, code.credits, kind="redeem", business_key=key, source="grants", memo=code.note)
        account = ledger.user_account(session, user.id)
        return {"credited": code.credits, "balance": account.balance}

    # ------------------------------------------------------------ uploads and downloads

    @app.post("/api/uploads")
    def init_upload(body: UploadInit, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        if body.size > settings.max_upload_bytes:
            raise HTTPException(413, f"file larger than {settings.max_upload_bytes // (1024 * 1024)} MB")
        upload = UploadSession(user_id=user.id, filename=body.filename, expected_size=body.size,
                               expected_sha256=body.sha256.lower() if body.sha256 else None)
        session.add(upload)
        session.flush()
        # No "instant upload" from a hash alone: knowing a file's sha256 must not grant access to it.
        # The bytes are always uploaded; storage still keeps one copy per content hash.
        token = sign_url_token(settings.secret_key, purpose="upload", ref=upload.id, ttl_s=settings.upload_url_ttl_s)
        return {"upload_id": upload.id, "complete": False, "upload_url": f"/api/upload/{token}", "chunk_size": CHUNK_SIZE}

    @app.put("/api/upload/{token}")
    async def put_chunk(token: str, request: Request, offset: int = Query(0, ge=0)) -> dict[str, Any]:
        try:
            upload_id = verify_url_token(settings.secret_key, token, purpose="upload")
        except TokenError as exc:
            raise HTTPException(403, str(exc)) from exc
        with factory() as session:
            upload = session.get(UploadSession, upload_id)
            if upload is None or upload.status != "open":
                raise HTTPException(409, "upload is not open")
            limit = upload.expected_size
        data = await request.body()
        if len(data) > CHUNK_SIZE:
            raise HTTPException(413, "chunk too large")
        import io

        try:
            received = storage.append(upload_id, offset, io.BytesIO(data), limit)
        except StorageError as exc:
            raise HTTPException(409, str(exc)) from exc
        with factory() as session:
            upload = session.get(UploadSession, upload_id)
            upload.received = received
            session.commit()
        return {"received": received, "expected": limit}

    @app.post("/api/uploads/{upload_id}/complete")
    def complete_upload(upload_id: str, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        upload = session.get(UploadSession, upload_id)
        if upload is None or upload.user_id != user.id:
            raise HTTPException(404, "upload not found")
        if upload.status == "complete":
            stored = session.get(StoredFile, upload.sha256)
            return {"sha256": upload.sha256, "size": upload.received, "kind": stored.sniffed_kind, "scan": stored.scan_status}
        if upload.received != upload.expected_size:
            raise HTTPException(409, f"received {upload.received} of {upload.expected_size} bytes")
        partial = storage.partial_path(upload_id)
        with partial.open("rb") as handle:
            head = handle.read(8192)
        kind = sniff_kind(head, partial)
        sha, size, path = storage.finish(upload_id)
        if upload.expected_sha256 and upload.expected_sha256 != sha:
            upload.status = "failed"
            raise HTTPException(422, "sha256 does not match the uploaded bytes")
        stored = session.get(StoredFile, sha)
        if stored is None:
            scanner = getattr(app.state, "scanner", None)
            scan = scanner(path) if scanner else "not_scanned"
            stored = StoredFile(sha256=sha, size=size, storage_key=storage.key_for(sha), sniffed_kind=kind, scan_status=scan)
            session.add(stored)
        upload.status, upload.sha256 = "complete", sha
        return {"sha256": sha, "size": size, "kind": stored.sniffed_kind, "scan": stored.scan_status}

    @app.get("/api/files/{token}")
    def download(token: str, session: Session = Depends(db)):
        try:
            ref = verify_url_token(settings.secret_key, token, purpose="download")
        except TokenError as exc:
            raise HTTPException(403, str(exc)) from exc
        sha, _, name = ref.partition("|")
        stored = session.get(StoredFile, sha)
        if stored is None:
            raise HTTPException(404, "file not found")
        return FileResponse(storage.path(stored.storage_key), filename=name or sha)

    # ------------------------------------------------------------ jobs

    @app.post("/api/jobs")
    def new_job(body: DraftBody, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        return job_view(session, create_draft(session, user, body.request))

    @app.get("/api/jobs")
    def list_jobs(user: User = Depends(current_user), session: Session = Depends(db)) -> list[dict[str, Any]]:
        jobs = session.scalars(select(Job).where(Job.user_id == user.id, Job.state != "deleted").order_by(Job.created_at.desc()))
        return [job_view(session, job) for job in jobs]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        return job_view(session, own_job(job_id, user, session), detail=True)

    @app.put("/api/jobs/{job_id}")
    def edit_job(job_id: str, body: DraftBody, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        return job_view(session, update_draft(session, own_job(job_id, user, session), body.request))

    @app.post("/api/jobs/{job_id}/estimate")
    def estimate(job_id: str, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        return estimate_job(session, user, own_job(job_id, user, session))

    @app.post("/api/jobs/{job_id}/submit")
    def submit(job_id: str, body: SubmitBody, user: User = Depends(current_user), session: Session = Depends(db),
               idempotency_key: str | None = Header(default=None)):
        job = own_job(job_id, user, session)
        return idempotent(session, f"submit:{user.id}", idempotency_key, body.model_dump(),
                          lambda: job_view(session, submit_job(session, user, job, confirm_total=body.confirm_total)))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        return job_view(session, cancel_job(session, own_job(job_id, user, session), actor=user.id))

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        from autoppt_api.lifecycle import delete_job_data

        job = own_job(job_id, user, session)
        if job.state not in ("draft", "estimated", "delivered", "failed", "cancelled"):
            raise HTTPException(409, "cancel the job before deleting it")
        removed = delete_job_data(session, storage, job, actor_type="user", actor_id=user.id)
        return {"deleted": True, "removed_blobs": removed}

    @app.get("/api/jobs/{job_id}/events")
    def events(job_id: str, after: int = 0, user: User = Depends(current_user), session: Session = Depends(db)) -> list[dict[str, Any]]:
        own_job(job_id, user, session)
        rows = session.scalars(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.id > after).order_by(JobEvent.id).limit(500))
        return [{"id": e.id, "kind": e.kind, "message": e.message, "data": e.data, "at": iso(e.created_at)} for e in rows]

    @app.get("/api/jobs/{job_id}/events/stream")
    def stream(job_id: str, after: int = 0, timeout: float = Query(25.0, ge=0, le=300), user: User = Depends(current_user),
               session: Session = Depends(db)) -> StreamingResponse:
        own_job(job_id, user, session)

        def generate() -> Iterator[str]:
            last, deadline = after, time.monotonic() + timeout
            while True:
                with factory() as poll:
                    rows = poll.scalars(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.id > last).order_by(JobEvent.id)).all()
                for row in rows:
                    last = row.id
                    payload = {"id": row.id, "kind": row.kind, "message": row.message, "data": row.data}
                    yield f"id: {row.id}\nevent: {row.kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if time.monotonic() >= deadline:
                    break
                time.sleep(1.0)
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/api/jobs/{job_id}/checkpoint")
    def get_checkpoint(job_id: str, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        job = own_job(job_id, user, session)
        checkpoint = open_checkpoint_for(session, job)
        if checkpoint is None:
            raise HTTPException(404, "no open checkpoint")
        artifacts = {a.path: a for a in session.scalars(select(Artifact).where(Artifact.job_id == job.id, Artifact.kind == "preview"))}
        previews = []
        for preview in checkpoint.payload.get("previews", []):
            artifact = artifacts.get(preview["path"])
            previews.append({**preview, "url": download_url(artifact.sha256, preview["path"].rsplit("/", 1)[-1]) if artifact else None})
        rules = job.estimate or {}
        return {"checkpoint_id": checkpoint.checkpoint_id, "type": checkpoint.type, "round": checkpoint.round,
                "summary": checkpoint.payload.get("summary", ""), "questions": checkpoint.payload.get("questions", []),
                "previews": previews, "editability_scope": checkpoint.payload.get("editability_scope"),
                "pricing": {"redo_slide": rules.get("redo_slide"), "extra_round": rules.get("extra_round"),
                            "free_rounds": rules.get("free_rounds")}}

    @app.post("/api/jobs/{job_id}/checkpoint/{checkpoint_id}/response")
    def respond(job_id: str, checkpoint_id: str, body: ResponseBody, user: User = Depends(current_user),
                session: Session = Depends(db)) -> dict[str, Any]:
        job = own_job(job_id, user, session)
        responder = "user" if job.user_id == user.id else "admin"
        answer_checkpoint(session, user, job, checkpoint_id, body.model_dump(), responder=responder)
        return job_view(session, job, detail=True)

    # ------------------------------------------------------------ worker API

    @app.post("/api/worker/lease")
    def lease(body: LeaseBody, worker: Worker = Depends(current_worker), session: Session = Depends(db)):
        if body.template_version:
            worker.template_version = body.template_version
        if worker.status != "active":
            return Response(status_code=204)
        job = lease_job(session, worker, timeout_s=settings.lease_timeout_s)
        if job is None:
            return Response(status_code=204)
        files = [
            {"file_id": f.file_id, "role": f.role, "name": f.name, "sha256": f.sha256, "size": f.size, "url": download_url(f.sha256, f.name)}
            for f in session.scalars(select(JobFile).where(JobFile.job_id == job.id))
        ]
        answered = session.scalars(select(Checkpoint).where(Checkpoint.job_id == job.id, Checkpoint.status == "answered")).first()
        snapshot = session.get(Artifact, job.snapshot_artifact_id) if job.snapshot_artifact_id else None
        return {
            "job_id": job.id, "phase": job.pending_phase, "state": job.state, "lease_token": job.lease_token,
            "lease_expires_at": iso(job.lease_expires_at), "request": job.normalized, "files": files,
            "checkpoint_response": answered.response if answered else None,
            "snapshot": {"url": download_url(snapshot.sha256, "snapshot.zip"), "sha256": snapshot.sha256} if snapshot else None,
        }

    def leased(job_id: str, worker: Worker, session: Session) -> Job:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job

    @app.post("/api/worker/jobs/{job_id}/heartbeat")
    def worker_heartbeat(job_id: str, body: HeartbeatBody, worker: Worker = Depends(current_worker),
                         session: Session = Depends(db)) -> dict[str, Any]:
        job = heartbeat(session, leased(job_id, worker, session), worker, body.lease_token,
                        timeout_s=settings.lease_timeout_s, progress=body.progress)
        return {"lease_expires_at": iso(job.lease_expires_at), "state": job.state, "cancelled": job.state == "cancelled"}

    @app.put("/api/worker/jobs/{job_id}/artifacts")
    async def worker_artifact(job_id: str, request: Request, kind: str = Query(pattern="^(preview|deliverable|snapshot|report)$"),
                              path: str = Query(min_length=1, max_length=300), lease_token: str = Query(...),
                              worker: Worker = Depends(current_worker)) -> dict[str, Any]:
        data = await request.body()
        with factory() as session:
            job = leased(job_id, worker, session)
            from autoppt_api.services import require_lease

            require_lease(job, worker, lease_token)
            if len(data) > settings.max_upload_bytes * 5:
                raise HTTPException(413, "artifact too large")
            sha, stored_path = storage.put_bytes(data)
            if session.get(StoredFile, sha) is None:
                session.add(StoredFile(sha256=sha, size=len(data), storage_key=storage.key_for(sha),
                                       sniffed_kind=sniff_kind(data[:8192], stored_path), scan_status="worker_output"))
                session.flush()
            artifact = register_artifact(session, job, kind=kind, path=path, sha256=sha, size=len(data))
            session.commit()
            return {"artifact_id": artifact.id, "sha256": sha}

    @app.post("/api/worker/jobs/{job_id}/complete")
    def worker_complete(job_id: str, body: PhaseReport, worker: Worker = Depends(current_worker), session: Session = Depends(db),
                        idempotency_key: str | None = Header(default=None)):
        job = leased(job_id, worker, session)

        def run() -> dict[str, Any]:
            complete_phase(session, job, worker, body.model_dump())
            consumed = session.scalars(select(Checkpoint).where(Checkpoint.job_id == job.id, Checkpoint.status == "answered"))
            for checkpoint in consumed:
                checkpoint.status = "consumed"
            return {"state": job.state, "pending_phase": job.pending_phase}

        return idempotent(session, f"worker:{worker.id}:{job_id}", idempotency_key, body.model_dump(), run)

    @app.post("/api/worker/jobs/{job_id}/fail")
    def worker_fail(job_id: str, body: FailReport, worker: Worker = Depends(current_worker), session: Session = Depends(db),
                    idempotency_key: str | None = Header(default=None)):
        job = leased(job_id, worker, session)
        return idempotent(session, f"worker-fail:{worker.id}:{job_id}", idempotency_key, body.model_dump(),
                          lambda: {"state": fail_phase(session, job, worker, lease_token=body.lease_token, error=body.error,
                                                       retryable=body.retryable).state})

    # ------------------------------------------------------------ admin

    @app.post("/api/admin/credits/adjust")
    def admin_adjust(body: AdjustBody, admin: User = Depends(admin_user), session: Session = Depends(db),
                     idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        user = session.scalars(select(User).where(User.email == normalize_email(body.email))).first()
        if user is None:
            raise HTTPException(404, "user not found")
        key = f"adjust:{idempotency_key or secrets.token_hex(8)}"
        if body.credits > 0:
            ledger.credit_user(session, user.id, body.credits, kind="adjust", business_key=key, source="adjustments",
                               memo=body.memo, actor=admin.id)
        elif body.credits < 0:
            ledger.debit_user(session, user.id, -body.credits, kind="adjust", business_key=key, memo=body.memo,
                              actor=admin.id, target="adjustments")
        audit(session, "admin", admin.id, "credits.adjust", user.id, credits=body.credits, memo=body.memo)
        account = ledger.user_account(session, user.id)
        return {"balance": account.balance, "available": ledger.available(session, account)}

    @app.post("/api/admin/redeem-codes")
    def admin_codes(body: RedeemCodesBody, admin: User = Depends(admin_user), session: Session = Depends(db)) -> dict[str, Any]:
        codes = []
        for _ in range(body.count):
            code = "-".join(secrets.token_hex(2).upper() for _ in range(4))
            session.add(RedeemCode(code_hash=hash_secret(code), credits=body.credits, max_uses=body.max_uses, note=body.note,
                                   created_by=admin.id,
                                   expires_at=now() + timedelta(days=body.expires_days) if body.expires_days else None))
            codes.append(code)
        audit(session, "admin", admin.id, "redeem.create", "", count=body.count, credits=body.credits)
        return {"codes": codes, "note": "shown once; only hashes are stored"}

    @app.post("/api/admin/workers")
    def admin_worker(body: WorkerBody, admin: User = Depends(admin_user), session: Session = Depends(db)) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        worker = Worker(name=body.name, token_hash=hash_secret(token), capabilities=body.capabilities)
        session.add(worker)
        session.flush()
        audit(session, "admin", admin.id, "worker.create", worker.id, name=body.name)
        return {"worker_id": worker.id, "token": token, "note": "shown once; only the hash is stored"}

    @app.get("/api/admin/users")
    def admin_users(email: str = "", _admin: User = Depends(admin_user), session: Session = Depends(db)) -> list[dict[str, Any]]:
        query = select(User).order_by(User.created_at.desc()).limit(100)
        if email:
            query = query.where(User.email.contains(email.strip().lower()))
        result = []
        for user in session.scalars(query):
            account = ledger.user_account(session, user.id)
            result.append({"id": user.id, "email": user.email, "is_admin": user.is_admin, "status": user.status,
                           "balance": account.balance, "available": ledger.available(session, account),
                           "created_at": iso(user.created_at)})
        return result

    @app.get("/api/admin/workers")
    def admin_workers(_admin: User = Depends(admin_user), session: Session = Depends(db)) -> list[dict[str, Any]]:
        return [{"id": w.id, "name": w.name, "status": w.status, "capabilities": w.capabilities, "template_version": w.template_version,
                 "last_heartbeat_at": iso(w.last_heartbeat_at) if w.last_heartbeat_at else None}
                for w in session.scalars(select(Worker))]

    @app.put("/api/admin/pricing")
    def admin_pricing(body: PricingBody, admin: User = Depends(admin_user), session: Session = Depends(db)) -> dict[str, Any]:
        from autoppt_api.pricing import DEFAULT_RULES

        missing = sorted(set(DEFAULT_RULES) - set(body.rules))
        if missing:
            raise HTTPException(422, f"pricing rules missing keys: {', '.join(missing)}")
        latest = session.scalar(select(func.coalesce(func.max(PricingRule.version), 0)))
        for rule in session.scalars(select(PricingRule).where(PricingRule.active.is_(True))):
            rule.active = False
        session.add(PricingRule(version=int(latest) + 1, active=True, rules=body.rules, note=body.note))
        audit(session, "admin", admin.id, "pricing.update", str(int(latest) + 1))
        return {"version": int(latest) + 1}

    @app.get("/api/admin/jobs")
    def admin_jobs(state: str | None = None, _admin: User = Depends(admin_user), session: Session = Depends(db)) -> list[dict[str, Any]]:
        query = select(Job).order_by(Job.created_at.desc()).limit(200)
        if state:
            query = query.where(Job.state == state)
        return [job_view(session, job) | {"user_id": job.user_id, "lease_worker_id": job.lease_worker_id} for job in session.scalars(query)]

    @app.post("/api/admin/jobs/{job_id}/resume")
    def admin_resume(job_id: str, to_state: str = Query(...), admin: User = Depends(admin_user),
                     session: Session = Depends(db)) -> dict[str, Any]:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job_view(session, resume_blocked(session, job, actor=admin.id, previous_state=to_state))

    @app.get("/api/admin/lessons")
    def admin_lessons(status: str = "pending", _admin: User = Depends(admin_user), session: Session = Depends(db)) -> list[dict[str, Any]]:
        return [{"id": l.id, "job_id": l.job_id, "payload": l.payload, "status": l.status}
                for l in session.scalars(select(LessonCandidate).where(LessonCandidate.status == status))]

    @app.post("/api/admin/lessons/{lesson_id}")
    def admin_review_lesson(lesson_id: str, body: LessonReview, admin: User = Depends(admin_user),
                            session: Session = Depends(db)) -> dict[str, Any]:
        lesson = session.get(LessonCandidate, lesson_id)
        if lesson is None:
            raise HTTPException(404, "lesson not found")
        lesson.status, lesson.reviewed_by, lesson.reviewed_at = body.status, admin.id, now()
        audit(session, "admin", admin.id, "lesson.review", lesson_id, status=body.status)
        return {"id": lesson.id, "status": lesson.status}

    @app.get("/api/admin/ledger/check")
    def admin_ledger(_admin: User = Depends(admin_user), session: Session = Depends(db)) -> dict[str, Any]:
        issues = ledger.check_ledger(session)
        return {"ok": not issues, "issues": issues}

    @app.get("/api/admin/audit")
    def admin_audit(limit: int = Query(100, le=1000), _admin: User = Depends(admin_user), session: Session = Depends(db)) -> list[dict[str, Any]]:
        return [{"id": a.id, "actor_type": a.actor_type, "actor_id": a.actor_id, "action": a.action, "target": a.target,
                 "data": a.data, "at": iso(a.created_at)}
                for a in session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))]

    web_root = Path(__file__).resolve().parents[1] / "web"
    if web_root.is_dir():
        # The single-page front end; API routes above take precedence.
        app.mount("/", StaticFiles(directory=web_root, html=True), name="web")

    return app
