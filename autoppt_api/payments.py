"""Credit packages, orders and payment callbacks.

Provider-neutral flow:

1. ``create_order`` freezes the package price into an order and asks the
   provider for payment instructions (QR code / redirect URL).
2. The provider calls ``POST /api/payments/{provider}/callback``.  The
   provider adapter verifies the signature; :func:`handle_callback` records the
   payment once per ``(provider, provider_ref)``, checks the amount, and
   credits the user once per order (ledger business key ``order:<id>``).
   Repeated callbacks are harmless.
3. ``refund_order`` returns unspent credits from that order and asks the
   provider to refund the matching share of the money.
4. ``reconcile`` expires stale pending orders and reports paid orders without
   credits, credits without payments, and payments without orders.

Only a signed mock provider exists until D4 picks the market (WeChat Pay /
Alipay need a mainland business entity; Stripe/Paddle for overseas).  Real
adapters implement :class:`PaymentProvider`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoppt_api import ledger
from autoppt_api.models import CreditTransaction, Order, Payment, User, now
from autoppt_api.pricing import DEFAULT_RULES, active_rules
from autoppt_api.services import ServiceError, audit, aware

PENDING_ORDER_TTL = timedelta(minutes=30)


@dataclass
class CallbackEvent:
    provider_ref: str
    order_id: str
    status: str  # paid | failed
    amount_cents: int
    raw: dict[str, Any]


class PaymentProvider(Protocol):
    name: str

    def create(self, order: Order) -> dict[str, Any]: ...

    def parse_callback(self, headers: dict[str, str], body: bytes) -> CallbackEvent: ...

    def refund(self, order: Order, amount_cents: int) -> str: ...


class MockProvider:
    """Test provider: callbacks are JSON signed with HMAC-SHA256 in ``X-Mock-Signature``."""

    name = "mock"

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("mock provider needs a secret")
        self.secret = secret.encode()

    def sign(self, body: bytes) -> str:
        return hmac.new(self.secret, body, hashlib.sha256).hexdigest()

    def create(self, order: Order) -> dict[str, Any]:
        return {"provider_ref": f"mock_{order.id}", "instructions": "mock payment: POST a signed callback to complete"}

    def parse_callback(self, headers: dict[str, str], body: bytes) -> CallbackEvent:
        signature = headers.get("x-mock-signature", "")
        if not hmac.compare_digest(signature, self.sign(body)):
            raise ServiceError(400, "bad_signature", "callback signature does not verify")
        data = json.loads(body)
        return CallbackEvent(provider_ref=str(data["provider_ref"]), order_id=str(data["order_id"]),
                             status=str(data["status"]), amount_cents=int(data["amount_cents"]), raw=data)

    def refund(self, order: Order, amount_cents: int) -> str:
        return f"mock_refund_{order.id}_{amount_cents}"


def providers_from_settings(settings: Any) -> dict[str, PaymentProvider]:
    providers: dict[str, PaymentProvider] = {}
    if getattr(settings, "payment_mock_secret", ""):
        providers["mock"] = MockProvider(settings.payment_mock_secret)
    return providers


def packages(session: Session) -> list[dict[str, Any]]:
    _, rules = active_rules(session)
    return list(rules.get("packages", DEFAULT_RULES["packages"]))


def create_order(session: Session, user: User, package_id: str, provider: PaymentProvider) -> tuple[Order, dict[str, Any]]:
    package = next((p for p in packages(session) if p["id"] == package_id), None)
    if package is None:
        raise ServiceError(404, "unknown_package", "没有这个积分包")
    order = Order(user_id=user.id, package=package["id"], credits=int(package["credits"]),
                  amount_cents=int(package["price_cents"]), currency=package.get("currency", "CNY"), provider=provider.name)
    session.add(order)
    session.flush()
    instructions = provider.create(order)
    order.provider_ref = instructions["provider_ref"]
    audit(session, "user", user.id, "order.create", order.id, package=package["id"], amount_cents=order.amount_cents)
    return order, instructions


def handle_callback(session: Session, provider: PaymentProvider, event: CallbackEvent) -> dict[str, Any]:
    order = session.get(Order, event.order_id)
    if order is None or order.provider != provider.name or order.provider_ref != event.provider_ref:
        audit(session, "system", provider.name, "payment.orphan", event.order_id, ref=event.provider_ref)
        raise ServiceError(404, "unknown_order", "callback does not match an order")
    status = event.status
    if status == "paid" and event.amount_cents != order.amount_cents:
        status = "amount_mismatch"  # never credit a different amount than the order says
    # One row per (reference, status): a retried notification is a duplicate, but a later "paid"
    # after a "failed" or mismatched notice for the same transaction is still processed.
    seen = session.scalars(select(Payment).where(Payment.provider == provider.name, Payment.provider_ref == event.provider_ref,
                                                 Payment.status == status)).first()
    if seen is not None:
        return {"duplicate": True, "order_id": order.id, "status": order.status}
    if status == "amount_mismatch":
        audit(session, "system", provider.name, "payment.amount_mismatch", order.id,
              expected=order.amount_cents, got=event.amount_cents)
    session.add(Payment(order_id=order.id, provider=provider.name, provider_ref=event.provider_ref, status=status,
                        amount_cents=event.amount_cents, raw=event.raw))
    if status == "paid" and order.status in ("pending", "expired"):
        ledger.credit_user(session, order.user_id, order.credits, kind="topup", business_key=f"order:{order.id}",
                           source="payments", memo=f"package {order.package}")
        order.status, order.paid_at = "paid", now()
        audit(session, "system", provider.name, "order.paid", order.id, credits=order.credits)
    session.flush()
    return {"duplicate": False, "order_id": order.id, "status": order.status}


def refund_order(session: Session, order: Order, provider: PaymentProvider, *, actor: str) -> dict[str, Any]:
    """Refund unspent credits from this order and the matching share of the money."""
    if order.status != "paid":
        raise ServiceError(409, "not_refundable", "只有已支付的订单可以退款")
    account = ledger.user_account(session, order.user_id, lock=True)
    refundable = min(order.credits - order.refunded_credits, max(ledger.available(session, account), 0))
    if refundable <= 0:
        raise ServiceError(409, "nothing_to_refund", "订单积分已用完或被冻结")
    cents = order.amount_cents * refundable // order.credits
    ledger.post(session, kind="refund", business_key=f"order-refund:{order.id}:{order.refunded_credits}",
                memo=f"refund order {order.id}", actor=actor,
                entries=[(account, -refundable), (ledger.system_account(session, "payments"), refundable)])
    reference = provider.refund(order, cents)
    order.refunded_credits += refundable
    order.refunded_cents += cents
    if order.refunded_credits >= order.credits:
        order.status = "refunded"
    audit(session, "admin", actor, "order.refund", order.id, credits=refundable, cents=cents, reference=reference)
    return {"refunded_credits": refundable, "refunded_cents": cents, "reference": reference, "status": order.status}


def reconcile(session: Session) -> dict[str, Any]:
    current = now()
    expired = []
    for order in session.scalars(select(Order).where(Order.status == "pending")):
        if aware(order.created_at) + PENDING_ORDER_TTL < current:
            order.status = "expired"
            expired.append(order.id)
    credited = {txn.business_key.split(":", 1)[1] for txn in session.scalars(
        select(CreditTransaction).where(CreditTransaction.business_key.like("order:%")))}
    paid_orders = {order.id for order in session.scalars(select(Order).where(Order.status.in_(("paid", "refunded"))))}
    paid_payments = {p.order_id for p in session.scalars(select(Payment).where(Payment.status == "paid"))}
    report = {
        "expired_orders": expired,
        "paid_without_credit": sorted(paid_orders - credited),
        "credit_without_payment": sorted(credited - paid_payments),
        "payments_without_paid_order": sorted(paid_payments - paid_orders),
    }
    report["ok"] = not (report["paid_without_credit"] or report["credit_without_payment"] or report["payments_without_paid_order"])
    return report
