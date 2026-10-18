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
import hmac
import json
import logging
import mimetypes
import secrets
import time
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
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
    CreditTransaction,
    IdempotencyKey,
    Job,
    JobEvent,
    JobFile,
    JobPhase,
    LessonCandidate,
    ModelToken,
    Notification,
    PricingRule,
    RedeemCode,
    StoredFile,
    UploadSession,
    User,
    Worker,
    now,
)
from autoppt_api.messaging import deliver_login_code, normalize_phone
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
    email: str | None = None
    phone: str | None = None


class LoginRequest(BaseModel):
    email: str | None = None
    phone: str | None = None
    code: str


class ModelTokenBody(BaseModel):
    lease_token: str
    phase: str = Field(min_length=1, max_length=16)


class TicketRequest(BaseModel):
    ticket: str = Field(min_length=10, max_length=600)


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


LEGAL_DRAFT_VERSION = "2026-10-19"  # web/legal/terms.md and privacy.md


class SubmitBody(BaseModel):
    confirm_total: int
    accepted_terms: str | None = Field(default=None, max_length=40)  # version of the user agreement accepted


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
    slides: int | None = None


class FailReport(BaseModel):
    lease_token: str
    error: str
    retryable: bool = True


class AdjustBody(BaseModel):
    email: str
    credits: int
    memo: str = Field(min_length=1, max_length=300)


class JobRefundBody(BaseModel):
    credits: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=300)


class RedeemCodesBody(BaseModel):
    credits: int = Field(gt=0)
    count: int = Field(default=1, ge=1, le=500)
    max_uses: int = Field(default=1, ge=1)
    expires_days: int | None = Field(default=None, ge=1)
    credit_expires_days: int | None = Field(default=None, ge=1)
    note: str = ""


class OrderBody(BaseModel):
    package: str
    provider: str = "mock"


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
    from autoppt_api.gateway import KeyPool

    key_pool = KeyPool.from_file(settings.provider_keys_file)  # provider keys stay in this process's memory
    storage = LocalStorage(settings.storage_dir, key=settings.storage_key or None)
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

    # Measured 2026-10-18 (scripts/stress_test.py): a sync dependency's teardown runs as a separate job in
    # the same thread pool as the endpoints.  Requests waiting for the database write lock filled every
    # thread, so the request holding the lock could not get a thread to commit, and all of them waited
    # out the 30 s SQLite timeout ("database is locked").  Commits get their own threads.
    commit_limiter = anyio.CapacityLimiter(8)

    async def finish(call: Any) -> None:
        with anyio.CancelScope(shield=True):  # a client disconnect must not leave the transaction open
            await anyio.to_thread.run_sync(call, limiter=commit_limiter)

    async def db() -> AsyncIterator[Session]:
        session = factory()
        try:
            yield session
            await finish(session.commit)
        except BaseException:
            await finish(session.rollback)
            raise
        finally:
            await finish(session.close)

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

    def own_job(job_id: str, user: User, session: Session, request: Request | None = None) -> Job:
        job = session.get(Job, job_id)
        if job is None or (job.user_id != user.id and not user.is_admin) or job.state == "deleted":
            raise HTTPException(404, "job not found")
        if job.user_id != user.id:
            # An administrator looking at a customer's job must say why; every access is audited.
            from urllib.parse import unquote

            reason = unquote(request.headers.get("X-Access-Reason", "")).strip() if request is not None else ""
            if not reason:
                raise HTTPException(403, "X-Access-Reason header required to open another user's job")
            audit(session, "admin", user.id, "admin.access_job", job.id, reason=reason[:300],
                  path=request.url.path if request is not None else "")
            session.commit()  # the access record survives even if the action itself is refused
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
        payload = export_options()
        payload["terms_version"] = settings.terms_version or LEGAL_DRAFT_VERSION
        samples = Path(__file__).resolve().parents[1] / "web" / "styles"  # scripts/build_style_samples.py
        kinds = json.loads((samples / "samples.json").read_text(encoding="utf-8")) if (samples / "samples.json").is_file() else {}
        for style in payload["styles"]:
            if (samples / f"{style['id']}.jpg").is_file():
                style["sample_image"] = f"/styles/{style['id']}.jpg"
                # "imagegen": a real master in this style; "schematic": drawn from the palette until image_gen returns
                style["sample_kind"] = kinds.get(style["id"], {}).get("kind", "imagegen")
        return payload

    @app.get("/api/pricing")
    def pricing(session: Session = Depends(db)) -> dict[str, Any]:
        from autoppt_api.pricing import active_rules

        version, rules = active_rules(session)
        return {"version": version, "rules": rules,
                "note": "测试期价格，P1 实测后调整" if rules.get("provisional") else ""}

    # ------------------------------------------------------------ auth

    def account_key(body: CodeRequest | LoginRequest) -> tuple[str, str | None, str | None]:
        """``(code identity, email, phone)`` from a request that gives exactly one of email and phone."""
        if bool(body.email) == bool(body.phone):
            raise HTTPException(422, "give an email or a phone number")
        try:
            if body.phone:
                phone = normalize_phone(body.phone)
                return f"phone:{phone}", None, phone
            email = normalize_email(body.email or "")
            return email, email, None
        except ValueError as exc:
            raise HTTPException(422, "invalid phone number" if body.phone else "invalid email") from exc

    def signup(session: Session, **identity: Any) -> User:
        user = User(**identity, is_admin=bool(identity.get("email")) and identity["email"] in settings.admin_emails)
        session.add(user)
        session.flush()
        ledger.user_account(session, user.id)
        if settings.signup_bonus_credits > 0:
            ledger.credit_user(session, user.id, settings.signup_bonus_credits, kind="grant",
                               business_key=f"signup:{user.id}", source="grants", memo="signup bonus",
                               expires_in_days=settings.signup_bonus_expires_days)
        audit(session, "user", user.id, "user.signup", user.id, via=next(iter(identity)))
        return user

    def tokens_for(session: Session, user: User) -> dict[str, Any]:
        if user.status != "active":
            raise HTTPException(403, "account disabled")
        return issue_tokens(session, user.id, secret=settings.secret_key, access_ttl=settings.access_token_ttl_s,
                            refresh_ttl=settings.refresh_token_ttl_s)

    @app.post("/api/auth/code")
    def request_code(body: CodeRequest, request: Request, background: BackgroundTasks,
                     session: Session = Depends(db)) -> dict[str, Any]:
        key, email, phone = account_key(body)
        ip = request.client.host if request.client else ""
        if recent_codes(session, email=key) >= settings.login_codes_per_hour or recent_codes(session, ip=ip) >= settings.login_codes_per_hour * 4:
            raise HTTPException(429, "too many codes requested; try again later")
        code = create_login_code(session, key, ttl_s=settings.login_code_ttl_s, ip=ip)
        # Sent after the response, once this request's transaction has ended; the code is never stored in clear.
        background.add_task(deliver_login_code, settings, email=email, phone=phone, code=code)
        result: dict[str, Any] = {"sent": True, "expires_in": settings.login_code_ttl_s, "channel": "sms" if phone else "email"}
        if settings.dev_echo_codes:
            result["dev_code"] = code
        return result

    @app.post("/api/auth/login")
    def login(body: LoginRequest, session: Session = Depends(db)) -> dict[str, Any]:
        key, email, phone = account_key(body)
        if not consume_login_code(session, key, body.code, max_attempts=settings.login_code_max_attempts):
            session.commit()  # keep the attempt count even though the request fails
            raise HTTPException(401, "invalid or expired code")
        column = User.phone if phone else User.email
        user = session.scalars(select(User).where(column == (phone or email))).first()
        if user is None:
            user = signup(session, **({"phone": phone} if phone else {"email": email}))
        return tokens_for(session, user)

    @app.get("/api/auth/options")
    def auth_options() -> dict[str, Any]:
        return {"email": True, "phone": True, "wechat": bool(settings.wechat_app_id and settings.wechat_app_secret)}

    @app.get("/api/auth/wechat/start")
    def wechat_start() -> Response:
        from autoppt_api.wechat import authorize_url

        if not (settings.wechat_app_id and settings.wechat_app_secret):
            raise HTTPException(404, "WeChat login is not configured")
        nonce = secrets.token_urlsafe(16)
        state = sign_url_token(settings.secret_key, purpose="wechat-state", ref=nonce, ttl_s=600)
        callback = f"{settings.public_base_url.rstrip('/')}/api/auth/wechat/callback"
        response = JSONResponse({"url": authorize_url(settings.wechat_app_id, callback, state)})
        # The callback must come back to the browser that started the login (no login CSRF).
        response.set_cookie("autoppt_wx", nonce, max_age=600, httponly=True, samesite="lax",
                            secure=settings.public_base_url.startswith("https"))
        return response

    @app.get("/api/auth/wechat/callback")
    def wechat_callback(request: Request, code: str = Query(...), state: str = Query(...),
                        session: Session = Depends(db)) -> Response:
        from autoppt_api.wechat import WeChatError, exchange_code

        try:
            nonce = verify_url_token(settings.secret_key, state, purpose="wechat-state")
        except TokenError as exc:
            raise HTTPException(403, f"bad state: {exc}") from exc
        if not hmac.compare_digest(request.cookies.get("autoppt_wx", ""), nonce):
            raise HTTPException(403, "this login was started in another browser")
        try:
            ids = exchange_code(settings.wechat_app_id, settings.wechat_app_secret, code,
                                client=getattr(app.state, "wechat_http", None))
        except WeChatError as exc:
            raise HTTPException(502, str(exc)) from exc
        key = ids["unionid"] or f"openid:{ids['openid']}"
        user = session.scalars(select(User).where(User.wechat_unionid == key)).first() or signup(session, wechat_unionid=key)
        # A single-use, two-minute ticket instead of tokens in the URL (browser history, referrers).
        ticket_secret = create_login_code(session, f"wechat-ticket:{user.id}", ttl_s=120)
        ticket = sign_url_token(settings.secret_key, purpose="wechat-ticket", ref=f"{user.id}.{ticket_secret}", ttl_s=120)
        response = RedirectResponse(f"/#/login?wechat_ticket={ticket}", status_code=303)
        response.delete_cookie("autoppt_wx")
        return response

    @app.post("/api/auth/wechat/exchange")
    def wechat_exchange(body: TicketRequest, session: Session = Depends(db)) -> dict[str, Any]:
        try:
            user_id, ticket_secret = verify_url_token(settings.secret_key, body.ticket, purpose="wechat-ticket").split(".", 1)
        except (TokenError, ValueError) as exc:
            raise HTTPException(401, "invalid or expired ticket") from exc
        if not consume_login_code(session, f"wechat-ticket:{user_id}", ticket_secret, max_attempts=1):
            session.commit()
            raise HTTPException(401, "invalid or expired ticket")
        return tokens_for(session, session.get(User, user_id))

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
        return {"id": user.id, "email": user.email, "phone": user.phone, "wechat": bool(user.wechat_unionid),
                "is_admin": user.is_admin, "locale": user.locale,
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
        if session.scalars(select(CreditTransaction).where(CreditTransaction.business_key == key)).first() is None:
            if code.uses >= code.max_uses:
                raise HTTPException(409, "code fully used")
            code.uses += 1
        ledger.credit_user(session, user.id, code.credits, kind="redeem", business_key=key, source="grants", memo=code.note,
                           expires_in_days=code.credit_expires_days)
        account = ledger.user_account(session, user.id)
        return {"credited": code.credits, "balance": account.balance}

    # ------------------------------------------------------------ packages, orders, payments

    from autoppt_api import payments as payments_module
    from autoppt_api.models import Order

    providers = payments_module.providers_from_settings(settings)
    app.state.payment_providers = providers

    def provider_for(name: str):
        if name not in providers:
            raise HTTPException(404, f"payment provider {name} is not enabled")
        return providers[name]

    @app.get("/api/packages")
    def list_packages(session: Session = Depends(db)) -> dict[str, Any]:
        return {"packages": payments_module.packages(session), "providers": sorted(providers)}

    @app.post("/api/orders")
    def new_order(body: OrderBody, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        order, instructions = payments_module.create_order(session, user, body.package, provider_for(body.provider))
        return {"order_id": order.id, "credits": order.credits, "amount_cents": order.amount_cents, "currency": order.currency,
                "status": order.status, "payment": instructions}

    @app.get("/api/orders")
    def my_orders(user: User = Depends(current_user), session: Session = Depends(db)) -> list[dict[str, Any]]:
        return [{"id": o.id, "package": o.package, "credits": o.credits, "amount_cents": o.amount_cents, "currency": o.currency,
                 "status": o.status, "refunded_credits": o.refunded_credits, "refunded_cents": o.refunded_cents,
                 "created_at": iso(o.created_at), "paid_at": iso(o.paid_at)}
                for o in session.scalars(select(Order).where(Order.user_id == user.id).order_by(Order.created_at.desc()))]

    @app.post("/api/payments/{provider_name}/callback")
    async def payment_callback(provider_name: str, request: Request) -> dict[str, Any]:
        provider = provider_for(provider_name)
        body = await request.body()
        event = provider.parse_callback({k.lower(): v for k, v in request.headers.items()}, body)

        def handle() -> dict[str, Any]:  # in a thread, like put_chunk: never wait for the DB lock on the event loop
            with factory() as session:
                try:
                    result = payments_module.handle_callback(session, provider, event)
                except ServiceError:
                    session.commit()  # keep the audit record of a rejected callback
                    raise
                session.commit()
                return result

        return await anyio.to_thread.run_sync(handle)

    @app.post("/api/admin/orders/{order_id}/refund")
    def admin_refund(order_id: str, admin: User = Depends(admin_user), session: Session = Depends(db)) -> dict[str, Any]:
        order = session.get(Order, order_id)
        if order is None:
            raise HTTPException(404, "order not found")
        return payments_module.refund_order(session, order, provider_for(order.provider), actor=admin.id)

    @app.post("/api/admin/reconcile")
    def admin_reconcile(_admin: User = Depends(admin_user), session: Session = Depends(db)) -> dict[str, Any]:
        return payments_module.reconcile(session)

    @app.get("/api/admin/reports/margin")
    def admin_margin(days: int = Query(30, ge=1, le=366), _admin: User = Depends(admin_user),
                     session: Session = Depends(db)) -> dict[str, Any]:
        from autoppt_api.reports import margin_report

        return margin_report(session, days=days)

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
        # Database and file work runs in threads: blocking calls here stalled the event loop while SQLite
        # waited for the write lock, which froze every request including the one holding the lock.
        def expected_size() -> int:
            with factory() as session:
                upload = session.get(UploadSession, upload_id)
                if upload is None or upload.status != "open":
                    raise HTTPException(409, "upload is not open")
                return upload.expected_size

        def record(received: int) -> None:
            with factory() as session:
                session.get(UploadSession, upload_id).received = received
                session.commit()

        limit = await anyio.to_thread.run_sync(expected_size)
        data = await request.body()
        if len(data) > CHUNK_SIZE:
            raise HTTPException(413, "chunk too large")
        import io

        try:
            received = await anyio.to_thread.run_sync(lambda: storage.append(upload_id, offset, io.BytesIO(data), limit))
        except StorageError as exc:
            raise HTTPException(409, str(exc)) from exc
        await anyio.to_thread.run_sync(record, received)
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
        scanner = getattr(app.state, "scanner", None)
        scan = scanner(partial) if scanner else "not_scanned"  # scan the plaintext, before encryption at rest
        sha, size, path = storage.finish(upload_id)
        if upload.expected_sha256 and upload.expected_sha256 != sha:
            upload.status = "failed"
            raise HTTPException(422, "sha256 does not match the uploaded bytes")
        stored = session.get(StoredFile, sha)
        if stored is None:
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
        if storage.encrypted:
            import mimetypes
            from urllib.parse import quote

            media = mimetypes.guess_type(name)[0] or "application/octet-stream"
            return Response(content=storage.read_bytes(stored.storage_key), media_type=media,
                            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name or sha)}"})
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
    def get_job(request: Request, job_id: str, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        return job_view(session, own_job(job_id, user, session, request), detail=True)

    @app.put("/api/jobs/{job_id}")
    def edit_job(request: Request, job_id: str, body: DraftBody, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        return job_view(session, update_draft(session, own_job(job_id, user, session, request), body.request))

    @app.post("/api/jobs/{job_id}/estimate")
    def estimate(request: Request, job_id: str, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        return estimate_job(session, user, own_job(job_id, user, session, request))

    @app.post("/api/jobs/{job_id}/submit")
    def submit(request: Request, job_id: str, body: SubmitBody, user: User = Depends(current_user), session: Session = Depends(db),
               idempotency_key: str | None = Header(default=None)):
        job = own_job(job_id, user, session, request)
        if settings.terms_version and body.accepted_terms != settings.terms_version:
            raise HTTPException(422, f"accept the current user agreement (version {settings.terms_version}) to order")

        def run() -> dict[str, Any]:
            if body.accepted_terms:
                audit(session, "user", user.id, "job.terms_accepted", job.id, version=body.accepted_terms)
            return job_view(session, submit_job(session, user, job, confirm_total=body.confirm_total))

        return idempotent(session, f"submit:{user.id}", idempotency_key, body.model_dump(), run)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(request: Request, job_id: str, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        return job_view(session, cancel_job(session, own_job(job_id, user, session, request), actor=user.id))

    @app.delete("/api/jobs/{job_id}")
    def delete_job(request: Request, job_id: str, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        from autoppt_api.lifecycle import delete_job_data

        job = own_job(job_id, user, session, request)
        if job.state not in ("draft", "estimated", "delivered", "failed", "cancelled"):
            raise HTTPException(409, "cancel the job before deleting it")
        removed = delete_job_data(session, storage, job, actor_type="user", actor_id=user.id)
        return {"deleted": True, "removed_blobs": removed}

    @app.get("/api/jobs/{job_id}/events")
    def events(request: Request, job_id: str, after: int = 0, user: User = Depends(current_user), session: Session = Depends(db)) -> list[dict[str, Any]]:
        own_job(job_id, user, session, request)
        rows = session.scalars(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.id > after).order_by(JobEvent.id).limit(500))
        return [{"id": e.id, "kind": e.kind, "message": e.message, "data": e.data, "at": iso(e.created_at)} for e in rows]

    @app.get("/api/jobs/{job_id}/events/stream")
    def stream(request: Request, job_id: str, after: int = 0, timeout: float = Query(25.0, ge=0, le=300), user: User = Depends(current_user),
               session: Session = Depends(db)) -> StreamingResponse:
        own_job(job_id, user, session, request)
        session.commit()  # release the request's lock before the long-lived stream polls with its own sessions

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
    def get_checkpoint(request: Request, job_id: str, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        job = own_job(job_id, user, session, request)
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
    def respond(request: Request, job_id: str, checkpoint_id: str, body: ResponseBody, user: User = Depends(current_user),
                session: Session = Depends(db)) -> dict[str, Any]:
        job = own_job(job_id, user, session, request)
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
                              authorization: str = Header(default="")) -> dict[str, Any]:
        # The body (snapshots run to many MB) is received before any database work, and the database work
        # runs in a thread: holding SQLite's write lock during the transfer, or waiting for it on the event
        # loop, stalled every other request (scripts/stress_test.py, 2026-10-18).
        from autoppt_api.services import require_lease

        data = await request.body()
        if len(data) > settings.max_upload_bytes * 5:
            raise HTTPException(413, "artifact too large")

        def register() -> dict[str, Any]:
            with factory() as session:
                worker = current_worker(authorization, session)
                job = leased(job_id, worker, session)
                require_lease(job, worker, lease_token)
                sha, stored_path = storage.put_bytes(data)
                if session.get(StoredFile, sha) is None:
                    session.add(StoredFile(sha256=sha, size=len(data), storage_key=storage.key_for(sha),
                                           sniffed_kind=sniff_kind(data[:8192], stored_path), scan_status="worker_output"))
                    session.flush()
                artifact = register_artifact(session, job, kind=kind, path=path, sha256=sha, size=len(data))
                session.commit()
                return {"artifact_id": artifact.id, "sha256": sha}

        return await anyio.to_thread.run_sync(register)

    @app.post("/api/worker/jobs/{job_id}/model-token")
    def worker_model_token(job_id: str, body: ModelTokenBody, worker: Worker = Depends(current_worker),
                           session: Session = Depends(db)) -> dict[str, Any]:
        """A model token for this lease (API-key mode): the job's remaining input-token budget, a few hours long."""
        from autoppt_api.services import require_lease

        if not key_pool:
            raise HTTPException(404, "model gateway is not configured")
        job = leased(job_id, worker, session)
        require_lease(job, worker, body.lease_token)
        spent = int(session.scalar(select(func.coalesce(func.sum(ModelToken.input_tokens), 0))
                                   .where(ModelToken.job_id == job_id)) or 0)
        remaining = settings.gateway_job_budget_input_tokens - spent
        if remaining <= 0:
            raise HTTPException(429, f"model budget for this job is used up ({spent} input tokens)")
        for old in session.scalars(select(ModelToken).where(ModelToken.job_id == job_id, ModelToken.revoked.is_(False))):
            old.revoked = True  # one live token per job
        token = secrets.token_urlsafe(32)
        expires = now() + timedelta(seconds=settings.gateway_token_ttl_s)
        session.add(ModelToken(job_id=job_id, worker_id=worker.id, phase=body.phase, token_hash=hash_secret(token),
                               budget_input_tokens=remaining, expires_at=expires))
        return {"token": token, "base_url": f"{settings.public_base_url.rstrip('/')}/gateway/v1",
                "budget_input_tokens": remaining, "expires_at": iso(expires)}

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

    def find_account(session: Session, identifier: str) -> User | None:
        """An account by email or (mainland) phone number, as typed by an administrator."""
        try:
            column, value = (User.email, normalize_email(identifier)) if "@" in identifier else (User.phone, normalize_phone(identifier))
        except ValueError:
            return None
        return session.scalars(select(User).where(column == value)).first()

    @app.post("/api/admin/credits/adjust")
    def admin_adjust(body: AdjustBody, admin: User = Depends(admin_user), session: Session = Depends(db),
                     idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        user = find_account(session, body.email)
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
                                   created_by=admin.id, credit_expires_days=body.credit_expires_days,
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
            needle = email.strip().lower()
            query = query.where(User.email.contains(needle) | User.phone.contains(needle.replace(" ", "")))
        result = []
        for user in session.scalars(query):
            account = ledger.user_account(session, user.id)
            result.append({"id": user.id, "email": user.email, "phone": user.phone, "wechat": bool(user.wechat_unionid),
                           "is_admin": user.is_admin, "status": user.status,
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

    @app.post("/api/admin/jobs/{job_id}/refund")
    def admin_job_refund(job_id: str, body: JobRefundBody, admin: User = Depends(admin_user), session: Session = Depends(db),
                         idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        """Return settled credits for one job (a poor delivery, a goodwill gesture); never more than it earned."""
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        key = f"job-refund:{job_id}:{idempotency_key or secrets.token_hex(8)}"
        if session.scalars(select(CreditTransaction).where(CreditTransaction.business_key == key)).first() is None:
            refundable = ledger.job_revenue(session, job_id)
            if body.credits > refundable:
                raise HTTPException(409, f"only {refundable} settled credits can be refunded for this job")
            ledger.refund(session, job_id, job.user_id, body.credits, business_key=key, memo=body.reason)
            session.add(Notification(user_id=job.user_id, subject="积分已退回",
                                     body=f"任务 {job.title or job_id[:8]} 退回 {body.credits} 积分。原因：{body.reason}"))
            audit(session, "admin", admin.id, "job.refund", job_id, credits=body.credits, reason=body.reason)
        account = ledger.user_account(session, job.user_id)
        return {"job_id": job_id, "refundable": ledger.job_revenue(session, job_id), "balance": account.balance}

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

    @app.get("/api/admin/health")
    def admin_health(_admin: User = Depends(admin_user), session: Session = Depends(db)) -> dict[str, Any]:
        from autoppt_api.monitoring import health

        return health(session, lease_timeout_s=settings.lease_timeout_s)

    @app.get("/api/admin/ledger/check")
    def admin_ledger(_admin: User = Depends(admin_user), session: Session = Depends(db)) -> dict[str, Any]:
        issues = ledger.check_ledger(session)
        return {"ok": not issues, "issues": issues}

    @app.get("/api/admin/gateway")
    def admin_gateway(_admin: User = Depends(admin_user), session: Session = Depends(db)) -> dict[str, Any]:
        tokens = session.scalars(select(ModelToken).order_by(ModelToken.created_at.desc()).limit(100)).all()
        return {"enabled": bool(key_pool), "upstream": settings.gateway_upstream_url, "keys": key_pool.stats(),
                "job_budget_input_tokens": settings.gateway_job_budget_input_tokens,
                "tokens": [{"job_id": t.job_id, "phase": t.phase, "requests": t.requests, "input_tokens": t.input_tokens,
                            "cached_input_tokens": t.cached_input_tokens, "output_tokens": t.output_tokens,
                            "budget_input_tokens": t.budget_input_tokens, "revoked": t.revoked,
                            "expires_at": iso(t.expires_at), "created_at": iso(t.created_at)} for t in tokens]}

    @app.api_route("/gateway/v1/{path:path}", methods=["GET", "POST"])
    async def gateway(path: str, request: Request, authorization: str = Header(default="")) -> Response:
        import httpx

        from autoppt_api.gateway import ALLOWED_PATHS, RETRY_STATUSES, UsageMeter, retry_after_seconds

        if not key_pool:
            raise HTTPException(404, "model gateway is not configured")
        if path.split("/", 1)[0] not in ALLOWED_PATHS:
            raise HTTPException(404, "not forwarded")
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing model token")
        token_hash = hash_secret(authorization[7:])

        def check() -> str:  # in a thread: never wait for the database on the event loop
            with factory() as session:
                token = session.scalars(select(ModelToken).where(ModelToken.token_hash == token_hash)).first()
                if token is None or token.revoked or aware(token.expires_at) < now():
                    raise HTTPException(401, "invalid or expired model token")
                job = session.get(Job, token.job_id)
                if job is None or job.state in ("cancelled", "failed", "deleted", "delivered"):
                    raise HTTPException(403, "the job is not running")
                if token.input_tokens >= token.budget_input_tokens:
                    raise HTTPException(429, "the model budget for this job is used up")
                return token.id

        def record(token_id: str, usage: dict[str, int]) -> None:
            with factory() as session:
                token = session.get(ModelToken, token_id)
                token.requests += 1
                token.input_tokens += usage.get("input_tokens", 0)
                token.cached_input_tokens += usage.get("cached_input_tokens", 0)
                token.output_tokens += usage.get("output_tokens", 0)
                session.commit()

        token_id = await anyio.to_thread.run_sync(check)
        body = await request.body()
        if getattr(app.state, "gateway_client", None) is None:
            app.state.gateway_client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=15),
                                                         transport=getattr(app.state, "gateway_transport", None))
        client = app.state.gateway_client
        upstream, key, tried = None, None, set()
        headers = {"Content-Type": request.headers.get("content-type", "application/json"),
                   "Accept": request.headers.get("accept", "*/*")}
        while upstream is None:
            key = key_pool.acquire(tried)
            if key is None:
                raise HTTPException(503, "no provider key is available right now; try again later")
            tried.add(key.label)
            outgoing = client.build_request(request.method, f"{settings.gateway_upstream_url.rstrip('/')}/{path}",
                                            params=request.query_params, content=body,
                                            headers={**headers, "Authorization": f"Bearer {key.key}"})
            try:
                upstream = await client.send(outgoing, stream=True)
            except httpx.HTTPError:
                key_pool.release(key, failed_status=502)
                continue
            if upstream.status_code in RETRY_STATUSES:  # nothing streamed yet: rest this key, try the next
                retry = retry_after_seconds(upstream.headers)
                await upstream.aclose()
                key_pool.release(key, failed_status=upstream.status_code, retry_after=retry)
                upstream = None
        meter = UsageMeter()
        answer, used_key = upstream, key

        async def relay():
            try:
                async for chunk in answer.aiter_bytes():
                    meter.feed(chunk)
                    yield chunk
            finally:
                with anyio.CancelScope(shield=True):
                    await answer.aclose()
                    key_pool.release(used_key, failed_status=answer.status_code if answer.status_code >= 400 else None)
                    await anyio.to_thread.run_sync(record, token_id, meter.finish())

        return StreamingResponse(relay(), status_code=answer.status_code,
                                 headers={k: v for k in ("content-type", "x-request-id") if (v := answer.headers.get(k))})

    @app.get("/api/admin/audit")
    def admin_audit(limit: int = Query(100, le=1000), _admin: User = Depends(admin_user), session: Session = Depends(db)) -> list[dict[str, Any]]:
        return [{"id": a.id, "actor_type": a.actor_type, "actor_id": a.actor_id, "action": a.action, "target": a.target,
                 "data": a.data, "at": iso(a.created_at)}
                for a in session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))]

    web_root = Path(__file__).resolve().parents[1] / "web"
    if web_root.is_dir():
        # Windows takes MIME types from the registry, where .js can be text/plain; browsers then refuse to run
        # the front end's ES modules (the login page stayed empty on the CI runner, 2026-10-18).
        mimetypes.add_type("text/javascript", ".js")
        mimetypes.add_type("text/css", ".css")
        # The single-page front end; API routes above take precedence.
        app.mount("/", StaticFiles(directory=web_root, html=True), name="web")

    return app
