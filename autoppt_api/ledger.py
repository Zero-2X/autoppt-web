"""Double-entry credit ledger.

* Every posting is a :class:`CreditTransaction` whose entries sum to zero, keyed
  by a unique ``business_key``: posting the same key twice returns the first
  transaction instead of moving credits again (idempotent callbacks, retries).
* Balances are a cache of the entries and are only changed here.  User
  accounts never go negative.
* A hold freezes credits for one job without moving the balance.  Each
  completed phase settles part of it to revenue; cancellation and system
  failures release what is still frozen.  ``available = balance - holds``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autoppt_api.models import CreditAccount, CreditEntry, CreditHold, CreditLot, CreditTransaction, now

SYSTEM_ACCOUNTS = ("revenue", "grants", "payments", "adjustments")


class LedgerError(ValueError):
    pass


class InsufficientCredits(LedgerError):
    def __init__(self, needed: int, available: int) -> None:
        self.needed, self.available = needed, available
        super().__init__(f"needs {needed} credits, {available} available")


def _locked(session: Session, query):
    # PostgreSQL row locks; SQLite serialises writers anyway.
    if session.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    return session.scalars(query).first()


def user_account(session: Session, user_id: str, *, lock: bool = False) -> CreditAccount:
    query = select(CreditAccount).where(CreditAccount.user_id == user_id)
    account = _locked(session, query) if lock else session.scalars(query).first()
    if account is None:
        account = CreditAccount(user_id=user_id, balance=0)
        session.add(account)
        session.flush()
    return account


def system_account(session: Session, code: str) -> CreditAccount:
    if code not in SYSTEM_ACCOUNTS:
        raise LedgerError(f"unknown system account {code}")
    account = session.scalars(select(CreditAccount).where(CreditAccount.code == code)).first()
    if account is None:
        account = CreditAccount(code=code, balance=0)
        session.add(account)
        session.flush()
    return account


def frozen(session: Session, account: CreditAccount) -> int:
    total = session.scalar(
        select(func.coalesce(func.sum(CreditHold.remaining), 0)).where(
            CreditHold.account_id == account.id, CreditHold.status == "active"
        )
    )
    return int(total or 0)


def available(session: Session, account: CreditAccount) -> int:
    return account.balance - frozen(session, account)


def post(
    session: Session,
    *,
    kind: str,
    business_key: str,
    entries: Iterable[tuple[CreditAccount, int]],
    job_id: str | None = None,
    memo: str = "",
    actor: str = "system",
) -> CreditTransaction:
    existing = session.scalars(select(CreditTransaction).where(CreditTransaction.business_key == business_key)).first()
    if existing is not None:
        return existing
    lines = [(account, int(amount)) for account, amount in entries if int(amount) != 0]
    if not lines or sum(amount for _, amount in lines) != 0:
        raise LedgerError("a posting needs non-zero entries that sum to zero")
    for account, amount in lines:
        if account.user_id is not None and account.balance + amount < 0:
            raise InsufficientCredits(-amount, account.balance)
    transaction = CreditTransaction(kind=kind, business_key=business_key, job_id=job_id, memo=memo[:300], actor=actor)
    session.add(transaction)
    session.flush()
    for account, amount in lines:
        session.add(CreditEntry(transaction_id=transaction.id, account_id=account.id, amount=amount))
        account.balance += amount
        if account.user_id is not None and amount < 0 and kind != "expire":
            _consume_lots(session, account, -amount)
    session.flush()
    return transaction


def _consume_lots(session: Session, account: CreditAccount, amount: int) -> None:
    """Spend expiring credits first, earliest expiry first (in the user's favour)."""
    for lot in session.scalars(
        select(CreditLot).where(CreditLot.account_id == account.id, CreditLot.remaining > 0).order_by(CreditLot.expires_at)
    ):
        if amount <= 0:
            break
        used = min(lot.remaining, amount)
        lot.remaining -= used
        amount -= used


def expire_lots(session: Session, *, at: datetime | None = None) -> list[dict]:
    """Expire what is left of lapsed lots; frozen credits are never expired."""
    at = at or now()
    expired = []
    for lot in session.scalars(select(CreditLot).where(CreditLot.remaining > 0)).all():
        expires = lot.expires_at if lot.expires_at.tzinfo else lot.expires_at.replace(tzinfo=timezone.utc)
        if expires > at:
            continue
        account = session.get(CreditAccount, lot.account_id)
        amount = min(lot.remaining, max(available(session, account), 0))
        lot.remaining = 0
        if amount > 0:
            post(session, kind="expire", business_key=f"expire:{lot.id}", memo="credits expired",
                 entries=[(account, -amount), (system_account(session, "grants"), amount)])
        expired.append({"lot": lot.id, "account": account.id, "expired": amount})
    session.flush()
    return expired


def credit_user(
    session: Session, user_id: str, credits: int, *, kind: str, business_key: str, source: str, memo: str = "",
    actor: str = "system", expires_in_days: int | None = None,
) -> CreditTransaction:
    """Top-up (source ``payments``), grant/redeem (``grants``) or admin adjustment (``adjustments``)."""
    if credits <= 0:
        raise LedgerError("credits must be positive")
    account = user_account(session, user_id, lock=True)
    existing = session.scalars(select(CreditTransaction).where(CreditTransaction.business_key == business_key)).first()
    transaction = post(session, kind=kind, business_key=business_key, memo=memo, actor=actor,
                       entries=[(system_account(session, source), -credits), (account, credits)])
    if expires_in_days and existing is None:
        session.add(CreditLot(account_id=account.id, transaction_id=transaction.id, amount=credits, remaining=credits,
                              expires_at=now() + timedelta(days=expires_in_days)))
        session.flush()
    return transaction


def debit_user(
    session: Session, user_id: str, credits: int, *, kind: str, business_key: str, job_id: str | None = None,
    memo: str = "", actor: str = "system", target: str = "revenue",
) -> CreditTransaction:
    """Charge available (unfrozen) credits directly, e.g. a confirmed redo."""
    account = user_account(session, user_id, lock=True)
    if session.scalars(select(CreditTransaction).where(CreditTransaction.business_key == business_key)).first() is None:
        if available(session, account) < credits:
            raise InsufficientCredits(credits, available(session, account))
    return post(session, kind=kind, business_key=business_key, job_id=job_id, memo=memo, actor=actor,
                entries=[(account, -credits), (system_account(session, target), credits)])


def create_hold(session: Session, user_id: str, job_id: str, amount: int) -> CreditHold:
    existing = session.scalars(select(CreditHold).where(CreditHold.job_id == job_id)).first()
    if existing is not None:
        return existing
    account = user_account(session, user_id, lock=True)
    free = available(session, account)
    if amount > free:
        raise InsufficientCredits(amount, free)
    hold = CreditHold(account_id=account.id, job_id=job_id, amount=amount, remaining=amount)
    session.add(hold)
    session.flush()
    return hold


def job_hold(session: Session, job_id: str) -> CreditHold | None:
    return session.scalars(select(CreditHold).where(CreditHold.job_id == job_id)).first()


def settle(session: Session, job_id: str, amount: int, *, business_key: str, memo: str = "") -> int:
    """Move up to ``amount`` from the job's hold to revenue; returns what was settled.

    Anything above the remaining hold is absorbed by the platform (recorded by the caller).
    """
    hold = job_hold(session, job_id)
    if hold is None or amount <= 0:
        return 0
    already = session.scalars(select(CreditTransaction).where(CreditTransaction.business_key == business_key)).first()
    if already is not None:
        return sum(entry.amount for entry in already.entries if entry.account_id != hold.account_id)
    if hold.status != "active":
        return 0
    take = min(amount, hold.remaining)
    if take <= 0:
        return 0
    account = session.get(CreditAccount, hold.account_id)
    hold.remaining -= take
    post(session, kind="settle", business_key=business_key, job_id=job_id, memo=memo,
         entries=[(account, -take), (system_account(session, "revenue"), take)])
    if hold.remaining == 0:
        hold.status, hold.closed_at = "closed", now()
    return take


def release_hold(session: Session, job_id: str) -> int:
    """Unfreeze whatever is left of the job's hold; returns the released amount."""
    hold = job_hold(session, job_id)
    if hold is None or hold.status != "active":
        return 0
    released, hold.remaining = hold.remaining, 0
    hold.status, hold.closed_at = "closed", now()
    session.flush()
    return released


def job_revenue(session: Session, job_id: str) -> int:
    """Net credits this job brought to revenue (settlements and paid redos minus refunds)."""
    revenue = system_account(session, "revenue")
    return int(session.scalar(
        select(func.coalesce(func.sum(CreditEntry.amount), 0))
        .join(CreditTransaction, CreditTransaction.id == CreditEntry.transaction_id)
        .where(CreditTransaction.job_id == job_id, CreditEntry.account_id == revenue.id)
    ) or 0)


def refund(session: Session, job_id: str, user_id: str, amount: int, *, business_key: str, memo: str = "") -> CreditTransaction:
    """Return already-settled credits (revenue -> user)."""
    return post(session, kind="refund", business_key=business_key, job_id=job_id, memo=memo,
                entries=[(system_account(session, "revenue"), -amount), (user_account(session, user_id), amount)])


def check_ledger(session: Session) -> list[str]:
    """Consistency report: cached balances, zero-sum transactions, holds within balances."""
    issues: list[str] = []
    for account in session.scalars(select(CreditAccount)):
        total = session.scalar(select(func.coalesce(func.sum(CreditEntry.amount), 0)).where(CreditEntry.account_id == account.id))
        if int(total or 0) != account.balance:
            issues.append(f"account {account.id}: balance {account.balance} != entries {total}")
        if account.user_id is not None and (account.balance < 0 or frozen(session, account) > account.balance):
            issues.append(f"account {account.id}: negative or over-frozen balance")
    rows = session.execute(select(CreditEntry.transaction_id, func.sum(CreditEntry.amount)).group_by(CreditEntry.transaction_id))
    issues += [f"transaction {tid} does not sum to zero" for tid, total in rows if int(total) != 0]
    return issues


def _iso(value: datetime) -> str:
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def statement(session: Session, user_id: str, limit: int = 100) -> list[dict]:
    account = user_account(session, user_id)
    rows = session.execute(
        select(CreditTransaction, CreditEntry.amount)
        .join(CreditEntry, CreditEntry.transaction_id == CreditTransaction.id)
        .where(CreditEntry.account_id == account.id)
        .order_by(CreditTransaction.created_at.desc())
        .limit(limit)
    )
    return [
        {"id": txn.id, "kind": txn.kind, "amount": amount, "job_id": txn.job_id, "memo": txn.memo,
         "created_at": _iso(txn.created_at)}
        for txn, amount in rows
    ]
