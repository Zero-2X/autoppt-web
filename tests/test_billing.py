"""P4: payments, refunds, credit expiry, concurrency and ledger consistency."""

from __future__ import annotations

import json
import random
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from autoppt_api import ledger
from autoppt_api.app import create_app
from autoppt_api.config import Settings
from autoppt_api.models import CreditLot, Job, User, now
from autoppt_api.payments import MockProvider
from autoppt_api.services import ServiceError, create_draft, estimate_job, submit_job

SECRET = "test-payment-secret"


@pytest.fixture()
def api(tmp_path):
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'api.db').as_posix()}", storage_dir=tmp_path / "storage",
                        dev_echo_codes=True, admin_emails=("admin@example.com",), payment_mock_secret=SECRET)
    return TestClient(create_app(settings))


def login(client, email):
    code = client.post("/api/auth/code", json={"email": email}).json()["dev_code"]
    return {"Authorization": "Bearer " + client.post("/api/auth/login", json={"email": email, "code": code}).json()["access_token"]}


def callback(client, order, *, amount=None, status="paid", secret=SECRET):
    body = json.dumps({"provider_ref": order["payment"]["provider_ref"], "order_id": order["order_id"], "status": status,
                       "amount_cents": order["amount_cents"] if amount is None else amount}).encode()
    signature = MockProvider(secret).sign(body)
    return client.post("/api/payments/mock/callback", content=body, headers={"X-Mock-Signature": signature})


def balance(client, headers):
    return client.get("/api/credits", headers=headers).json()


def ledger_ok(client):
    with client.app.state.session_factory() as session:
        assert ledger.check_ledger(session) == []


def test_package_purchase_is_credited_once(api):
    user = login(api, "buyer@example.com")
    packages = api.get("/api/packages").json()
    assert packages["providers"] == ["mock"] and packages["packages"][0]["id"] == "p100"
    order = api.post("/api/orders", json={"package": "p100"}, headers=user).json()
    assert order["status"] == "pending" and order["amount_cents"] == 3000
    first = callback(api, order)
    assert first.status_code == 200 and first.json()["status"] == "paid"
    again = callback(api, order)  # provider retries the same notification
    assert again.json()["duplicate"] is True
    assert balance(api, user)["balance"] == 100
    orders = api.get("/api/orders", headers=user).json()
    assert orders[0]["status"] == "paid" and orders[0]["paid_at"].endswith("Z")
    ledger_ok(api)


def test_bad_signature_wrong_amount_and_unknown_order(api):
    user = login(api, "buyer@example.com")
    order = api.post("/api/orders", json={"package": "p300"}, headers=user).json()
    assert callback(api, order, secret="forged").status_code == 400
    mismatch = callback(api, order, amount=1)
    assert mismatch.status_code == 200 and mismatch.json()["status"] == "pending"  # recorded, never credited
    assert balance(api, user)["balance"] == 0
    fake = dict(order, order_id="0" * 32)
    assert callback(api, fake).status_code == 404
    assert callback(api, order, amount=1).json()["duplicate"] is True
    # The genuine notification for the same transaction still credits the order once.
    assert callback(api, order).json()["status"] == "paid"
    assert callback(api, order).json()["duplicate"] is True
    assert balance(api, user)["balance"] == 300
    assert api.post("/api/orders", json={"package": "nope"}, headers=user).status_code == 404
    assert api.post("/api/orders", json={"package": "p100", "provider": "wechat"}, headers=user).status_code == 404
    audit = api.get("/api/admin/audit", headers=login(api, "admin@example.com")).json()
    assert {"payment.amount_mismatch", "payment.orphan"} <= {a["action"] for a in audit}


def test_refund_only_unspent_credits(api):
    admin = login(api, "admin@example.com")
    user = login(api, "buyer@example.com")
    order = api.post("/api/orders", json={"package": "p100"}, headers=user).json()
    callback(api, order)
    with api.app.state.session_factory() as session:
        buyer = session.query(User).filter_by(email="buyer@example.com").one()
        ledger.debit_user(session, buyer.id, 40, kind="settle", business_key="spent-40")
        session.commit()
    refund = api.post(f"/api/admin/orders/{order['order_id']}/refund", headers=admin).json()
    assert refund["refunded_credits"] == 60 and refund["refunded_cents"] == 1800
    assert balance(api, user)["balance"] == 0
    assert api.post(f"/api/admin/orders/{order['order_id']}/refund", headers=admin).status_code == 409
    ledger_ok(api)


def test_reconcile_expires_stale_orders(api):
    admin = login(api, "admin@example.com")
    user = login(api, "buyer@example.com")
    stale = api.post("/api/orders", json={"package": "p100"}, headers=user).json()
    paid = api.post("/api/orders", json={"package": "p100"}, headers=user).json()
    callback(api, paid)
    from autoppt_api.models import Order

    with api.app.state.session_factory() as session:
        session.get(Order, stale["order_id"]).created_at = now() - timedelta(hours=2)
        session.commit()
    report = api.post("/api/admin/reconcile", headers=admin).json()
    assert report["ok"] is True and report["expired_orders"] == [stale["order_id"]]
    # A late payment for an expired order is still honoured.
    assert callback(api, stale).json()["status"] == "paid"
    assert balance(api, user)["balance"] == 200


def test_expiring_credits_are_spent_first_and_frozen_credits_never_expire(api):
    with api.app.state.session_factory() as session:
        user = User(email="lots@example.com")
        session.add(user)
        session.flush()
        ledger.credit_user(session, user.id, 100, kind="grant", business_key="bonus", source="grants", expires_in_days=7)
        ledger.credit_user(session, user.id, 50, kind="topup", business_key="paid", source="payments")
        ledger.debit_user(session, user.id, 30, kind="settle", business_key="spend")  # comes out of the expiring lot
        lot = session.query(CreditLot).one()
        assert lot.remaining == 70
        job = Job(user_id=user.id, state="queued")
        session.add(job)
        session.flush()
        ledger.create_hold(session, user.id, job.id, 100)  # 100 of the 120 are frozen
        expired = ledger.expire_lots(session, at=now() + timedelta(days=8))
        assert expired[0]["expired"] == 20  # only the unfrozen part of the lot
        account = ledger.user_account(session, user.id)
        assert account.balance == 100 and ledger.available(session, account) == 0
        assert ledger.expire_lots(session, at=now() + timedelta(days=9)) == []  # nothing left to expire
        assert ledger.check_ledger(session) == []
        session.commit()


def test_redeem_codes_with_expiring_credits(api):
    admin = login(api, "admin@example.com")
    user = login(api, "student@example.com")
    code = api.post("/api/admin/redeem-codes", json={"credits": 80, "credit_expires_days": 3}, headers=admin).json()["codes"][0]
    api.post("/api/credits/redeem", json={"code": code}, headers=user)
    with api.app.state.session_factory() as session:
        assert session.query(CreditLot).one().remaining == 80
        ledger.expire_lots(session, at=now() + timedelta(days=4))
        session.commit()
    statement = balance(api, user)
    assert statement["balance"] == 0 and [row["kind"] for row in statement["statement"]][:1] == ["expire"]


def test_concurrent_orders_cannot_overspend(api):
    """Two jobs race for the same 300 credits; only one may freeze them."""
    factory = api.app.state.session_factory
    from tests_support import job_request_for

    with factory() as session:
        user = User(email="race@example.com")
        session.add(user)
        session.flush()
        ledger.credit_user(session, user.id, 300, kind="grant", business_key="race-grant", source="grants")
        jobs = []
        for _ in range(2):
            job = create_draft(session, user, job_request_for(session, user))
            estimate_job(session, user, job)
            jobs.append((job.id, job.estimate["total"]))
        session.commit()
        user_id = user.id
    assert all(total > 150 for _, total in jobs)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def submit(job_id: str, total: int) -> None:
        barrier.wait()
        with factory() as session:
            try:
                submit_job(session, session.get(User, user_id), session.get(Job, job_id), confirm_total=total)
                session.commit()
                outcomes.append("ok")
            except ServiceError as exc:
                session.rollback()
                outcomes.append(exc.code)

    threads = [threading.Thread(target=submit, args=job) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["insufficient_credits", "ok"]
    with factory() as session:
        account = ledger.user_account(session, user_id)
        assert ledger.frozen(session, account) <= account.balance
        assert ledger.check_ledger(session) == []


def test_random_operations_keep_the_ledger_consistent(api):
    rng = random.Random(7)
    factory = api.app.state.session_factory
    with factory() as session:
        users = []
        for index in range(4):
            user = User(email=f"r{index}@example.com")
            session.add(user)
            session.flush()
            users.append(user.id)
        jobs: dict[str, str] = {}
        for step in range(300):
            user_id = rng.choice(users)
            action = rng.choice(["grant", "lot", "hold", "settle", "release", "debit", "refund", "expire"])
            try:
                if action == "grant":
                    ledger.credit_user(session, user_id, rng.randint(1, 50), kind="grant", business_key=f"g{step}", source="grants")
                elif action == "lot":
                    ledger.credit_user(session, user_id, rng.randint(1, 50), kind="grant", business_key=f"l{step}", source="grants",
                                       expires_in_days=rng.randint(1, 5))
                elif action == "hold":
                    job = Job(user_id=user_id, state="queued")
                    session.add(job)
                    session.flush()
                    ledger.create_hold(session, user_id, job.id, rng.randint(1, 40))
                    jobs[job.id] = user_id
                elif action == "settle" and jobs:
                    job_id = rng.choice(list(jobs))
                    ledger.settle(session, job_id, rng.randint(1, 30), business_key=f"s{step}")
                elif action == "release" and jobs:
                    ledger.release_hold(session, rng.choice(list(jobs)))
                elif action == "debit":
                    ledger.debit_user(session, user_id, rng.randint(1, 20), kind="settle", business_key=f"d{step}")
                elif action == "refund" and jobs:
                    job_id = rng.choice(list(jobs))
                    ledger.refund(session, job_id, jobs[job_id], rng.randint(1, 5), business_key=f"rf{step}")
                elif action == "expire":
                    ledger.expire_lots(session, at=now() + timedelta(days=rng.randint(0, 6)))
            except ledger.InsufficientCredits:
                pass
            if step % 50 == 0:
                assert ledger.check_ledger(session) == []
        assert ledger.check_ledger(session) == []
        session.commit()


def test_margin_report(api):
    admin = login(api, "admin@example.com")
    report = api.get("/api/admin/reports/margin", headers=admin).json()
    assert report["jobs"] == [] and report["credit_value_cents"] == 28
    assert "office_minute_cents" in report["cost_rates"]


def test_pricing_proposal_is_a_complete_rule_set(api):
    import json
    from pathlib import Path

    proposal = json.loads((Path(__file__).resolve().parents[1] / "docs" / "pricing-proposal-20261019.json").read_text(encoding="utf-8"))
    admin = login(api, "admin@example.com")
    reply = api.put("/api/admin/pricing", json=proposal, headers=admin)
    assert reply.status_code == 200, reply.text
    assert api.get("/api/pricing").json()["rules"]["per_slide"]["native_editable"] == 30
