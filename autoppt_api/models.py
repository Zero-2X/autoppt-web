"""ORM models.  Credits are whole integers; balances change only through ledger entries."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from autoppt_api.db import Base


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> Mapped[str]:
    return mapped_column(String(32), primary_key=True, default=new_id)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=now, nullable=False)


# ---------------------------------------------------------------- accounts and auth


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = _id()
    # One of email / phone / wechat_unionid identifies the account (phone and WeChat logins, 2026-10-18).
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    wechat_unionid: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    display_name: Mapped[str] = mapped_column(String(100), default="")
    locale: Mapped[str] = mapped_column(String(10), default="zh-CN")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | disabled | deleted
    created_at: Mapped[datetime] = _created()


class AuthCode(Base):
    __tablename__ = "auth_codes"
    id: Mapped[str] = _id()
    email: Mapped[str] = mapped_column(String(254), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    requester_ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = _created()


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[str] = _id()  # the token's jti
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    replaced_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- credits (double entry)


class CreditAccount(Base):
    __tablename__ = "credit_accounts"
    id: Mapped[str] = _id()
    # user accounts have user_id; system accounts have a unique code (revenue, grants, payments, ...)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), unique=True, nullable=True)
    code: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    balance: Mapped[int] = mapped_column(BigInteger, default=0)  # cache of the entries' sum
    created_at: Mapped[datetime] = _created()


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"
    id: Mapped[str] = _id()
    kind: Mapped[str] = mapped_column(String(24))  # topup | grant | redeem | settle | refund | adjust | expire
    business_key: Mapped[str] = mapped_column(String(160), unique=True)  # idempotency: one posting per key
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    memo: Mapped[str] = mapped_column(String(300), default="")
    actor: Mapped[str] = mapped_column(String(80), default="system")
    created_at: Mapped[datetime] = _created()
    entries: Mapped[list[CreditEntry]] = relationship(back_populates="transaction")


class CreditEntry(Base):
    __tablename__ = "credit_entries"
    id: Mapped[str] = _id()
    transaction_id: Mapped[str] = mapped_column(ForeignKey("credit_transactions.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("credit_accounts.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)  # signed; entries of one transaction sum to zero
    transaction: Mapped[CreditTransaction] = relationship(back_populates="entries")


class CreditHold(Base):
    __tablename__ = "credit_holds"
    id: Mapped[str] = _id()
    account_id: Mapped[str] = mapped_column(ForeignKey("credit_accounts.id"), index=True)
    job_id: Mapped[str] = mapped_column(String(32), unique=True)
    amount: Mapped[int] = mapped_column(BigInteger)  # originally frozen
    remaining: Mapped[int] = mapped_column(BigInteger)  # still frozen
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | closed
    created_at: Mapped[datetime] = _created()
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CreditLot(Base):
    """Credits that expire (grants, promotional redeem codes); spent before non-expiring credits."""

    __tablename__ = "credit_lots"
    id: Mapped[str] = _id()
    account_id: Mapped[str] = mapped_column(ForeignKey("credit_accounts.id"), index=True)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("credit_transactions.id"))
    amount: Mapped[int] = mapped_column(BigInteger)
    remaining: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = _created()


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = _id()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    package: Mapped[str] = mapped_column(String(32))
    credits: Mapped[int] = mapped_column(BigInteger)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | paid | refunded | expired
    provider: Mapped[str] = mapped_column(String(24), default="")
    provider_ref: Mapped[str] = mapped_column(String(128), default="")
    refunded_credits: Mapped[int] = mapped_column(BigInteger, default=0)
    refunded_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = _created()
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = _id()
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    provider: Mapped[str] = mapped_column(String(24))
    provider_ref: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (UniqueConstraint("provider", "provider_ref", "status", name="uq_payment_provider_ref_status"),)


class RedeemCode(Base):
    __tablename__ = "redeem_codes"
    id: Mapped[str] = _id()
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    credits: Mapped[int] = mapped_column(BigInteger)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_by: Mapped[str] = mapped_column(String(32), default="")
    credit_expires_days: Mapped[int | None] = mapped_column(Integer, nullable=True)  # credits from this code expire
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- files


class StoredFile(Base):
    """Content-addressed blob; identical uploads share one blob."""

    __tablename__ = "stored_files"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(200))
    sniffed_kind: Mapped[str] = mapped_column(String(16))  # from content, not the extension
    scan_status: Mapped[str] = mapped_column(String(16), default="not_scanned")  # clean | infected | not_scanned
    created_at: Mapped[datetime] = _created()


class UploadSession(Base):
    __tablename__ = "upload_sessions"
    id: Mapped[str] = _id()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    expected_size: Mapped[int] = mapped_column(BigInteger)
    expected_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    received: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | complete | failed
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- jobs


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = _id()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    state: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    service: Mapped[str] = mapped_column(String(16), default="new_deck")
    title: Mapped[str] = mapped_column(String(200), default="")
    request: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # draft as submitted by the client
    normalized: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # validated request
    plan: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)  # autoppt_service.status.phase_plan
    estimate: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    pending_phase: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)  # runnable work
    lease_worker_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_reason: Mapped[str] = mapped_column(Text, default="")
    snapshot_artifact_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    template_version: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    retention_days: Mapped[int] = mapped_column(Integer, default=7)


class JobFile(Base):
    __tablename__ = "job_files"
    id: Mapped[str] = _id()
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    file_id: Mapped[str] = mapped_column(String(64))  # the request's file_id (f_01)
    role: Mapped[str] = mapped_column(String(24))
    name: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(ForeignKey("stored_files.sha256"), index=True)
    size: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (UniqueConstraint("job_id", "file_id", name="uq_job_file_id"),)


class JobPhase(Base):
    __tablename__ = "job_phases"
    id: Mapped[str] = _id()
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    phase: Mapped[str] = mapped_column(String(16))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | done | failed | expired
    worker_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_credits: Mapped[int] = mapped_column(BigInteger, default=0)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # state | progress | checkpoint | warning | delivered | ...
    message: Mapped[str] = mapped_column(String(500), default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()


class Checkpoint(Base):
    __tablename__ = "checkpoints"
    id: Mapped[str] = _id()
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(24))
    round: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)  # service/checkpoint.json
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | answered | consumed
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (UniqueConstraint("job_id", "checkpoint_id", name="uq_job_checkpoint"),)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = _id()
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # preview | deliverable | snapshot | report
    path: Mapped[str] = mapped_column(String(300))  # job-relative path
    sha256: Mapped[str] = mapped_column(ForeignKey("stored_files.sha256"), index=True)
    size: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (UniqueConstraint("job_id", "kind", "path", "sha256", name="uq_artifact"),)


# ---------------------------------------------------------------- workers, providers, usage


class Worker(Base):
    __tablename__ = "workers"
    id: Mapped[str] = _id()
    name: Mapped[str] = mapped_column(String(64), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | draining | disabled
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)  # e.g. ["planning", "masters", "office"]
    template_version: Mapped[str] = mapped_column(String(64), default="")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class ProviderKey(Base):
    """Registry of model accounts (Codex profiles) by label only; no credentials are stored here."""

    __tablename__ = "provider_keys"
    id: Mapped[str] = _id()
    provider: Mapped[str] = mapped_column(String(24), default="codex")
    label: Mapped[str] = mapped_column(String(64), unique=True)
    worker_id: Mapped[str | None] = mapped_column(ForeignKey("workers.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | resting | disabled
    resting_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _created()


class ModelToken(Base):
    """A per-job credential for the model gateway (API-key mode, D2): bounded in time and input tokens.

    Only the hash is stored; the provider keys themselves never leave the gateway's environment."""

    __tablename__ = "model_tokens"
    id: Mapped[str] = _id()
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    worker_id: Mapped[str] = mapped_column(ForeignKey("workers.id"))
    phase: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    budget_input_tokens: Mapped[int] = mapped_column(BigInteger)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()


class UsageRecord(Base):
    __tablename__ = "usage_records"
    id: Mapped[str] = _id()
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    phase: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))  # agent_turn | office | phase
    profile: Mapped[str] = mapped_column(String(64), default="")
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    seconds: Mapped[float] = mapped_column(Float, default=0.0)  # SQLAlchemy 2.1 maps bare float to Double
    business_key: Mapped[str] = mapped_column(String(160), unique=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False, index=True)


class PricingRule(Base):
    __tablename__ = "pricing_rules"
    id: Mapped[str] = _id()
    version: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    rules: Mapped[dict[str, Any]] = mapped_column(JSON)
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = _created()


class LessonCandidate(Base):
    __tablename__ = "lesson_candidates"
    id: Mapped[str] = _id()
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | approved | rejected
    reviewed_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_type: Mapped[str] = mapped_column(String(16))  # user | admin | worker | system
    actor_id: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(100), default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(String(160), primary_key=True)  # "<scope>:<client key>"
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = _created()


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = _id()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16), default="email")
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | sent | failed
    created_at: Mapped[datetime] = _created()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index("ix_jobs_queue", Job.pending_phase, Job.lease_expires_at)
