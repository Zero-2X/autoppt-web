"""Phone (SMS) and WeChat logins, and login codes that actually reach new users (2026-10-18)."""

from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from autoppt_api.app import create_app
from autoppt_api.config import Settings
from autoppt_api.messaging import normalize_phone, send_sms


def make_client(tmp_path, base_url: str = "http://testserver", **overrides) -> TestClient:
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'api.db').as_posix()}", storage_dir=tmp_path / "storage",
                        admin_emails=("admin@example.com",), **{"dev_echo_codes": True, **overrides})
    return TestClient(create_app(settings), base_url=base_url)


def test_phone_numbers_are_normalised_to_mainland_mobiles():
    assert normalize_phone("138 0013 8000") == normalize_phone("+86-13800138000") == "+8613800138000"
    assert normalize_phone("0086 13800138000") == "+8613800138000"
    for bad in ("12800138000", "1380013800", "+1 4155550100", "abc"):
        with pytest.raises(ValueError):
            normalize_phone(bad)


def test_phone_login_creates_one_account_per_number(tmp_path):
    client = make_client(tmp_path)
    reply = client.post("/api/auth/code", json={"phone": "138 0013 8000"}).json()
    assert reply["channel"] == "sms"
    tokens = client.post("/api/auth/login", json={"phone": "+8613800138000", "code": reply["dev_code"]}).json()
    me = client.get("/api/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}).json()
    assert me["phone"] == "+8613800138000" and me["email"] is None
    code = client.post("/api/auth/code", json={"phone": "13800138000"}).json()["dev_code"]
    again = client.post("/api/auth/login", json={"phone": "13800138000", "code": code}).json()
    assert client.get("/api/me", headers={"Authorization": f"Bearer {again['access_token']}"}).json()["id"] == me["id"]
    # An email code does not open the phone account, and requests must name exactly one channel.
    assert client.post("/api/auth/login", json={"email": "a@example.com", "code": code}).status_code == 401
    assert client.post("/api/auth/code", json={"phone": "12345"}).status_code == 422
    assert client.post("/api/auth/code", json={"phone": "13800138000", "email": "a@example.com"}).status_code == 422
    assert client.post("/api/auth/code", json={}).status_code == 422


def test_codes_are_delivered_to_new_users_without_dev_echo(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("autoppt_api.app.deliver_login_code",
                        lambda settings, *, email, phone, code: sent.append((email, phone, code)))
    client = make_client(tmp_path, dev_echo_codes=False)
    reply = client.post("/api/auth/code", json={"email": "New.Student@Example.com"}).json()
    assert "dev_code" not in reply and reply["channel"] == "email"
    client.post("/api/auth/code", json={"phone": "13900139000"})
    assert [(e, p) for e, p, _ in sent] == [("new.student@example.com", None), (None, "+8613900139000")]
    tokens = client.post("/api/auth/login", json={"email": "new.student@example.com", "code": sent[0][2]})
    assert tokens.status_code == 200, tokens.text
    with client.app.state.session_factory() as session:  # the code is not kept in clear in notifications
        from autoppt_api.models import Notification

        assert not [n for n in session.query(Notification) if sent[0][2] in n.body]


def test_sms_webhook_is_signed(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"], seen["signature"] = request.content, request.headers["X-AutoPPT-Signature"]
        return httpx.Response(200, json={"ok": True})

    settings = Settings(database_url="sqlite://", storage_dir=tmp_path, sms_provider="webhook",
                        sms_webhook_url="https://relay.example/sms", sms_webhook_secret="s3cret")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    status = send_sms(settings, "+8613800138000", template="login_code", params={"code": "123456", "minutes": "10"},
                      text="【AutoPPT】验证码 123456", client=client)
    assert status == "sent"
    assert json.loads(seen["body"])["params"]["code"] == "123456"
    assert seen["signature"] == "sha256=" + hmac.new(b"s3cret", seen["body"], hashlib.sha256).hexdigest()
    failing = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    assert send_sms(settings, "+8613800138000", template="t", params={}, text="x", client=failing) == "failed"
    with pytest.raises(ValueError):
        Settings(database_url="sqlite://", storage_dir=tmp_path, sms_provider="webhook")


def test_phone_only_users_get_notifications_by_sms(tmp_path, monkeypatch):
    from autoppt_api.maintenance import send_notifications
    from autoppt_api.models import Notification, User

    texts = []
    monkeypatch.setattr("autoppt_api.messaging.send_sms",
                        lambda settings, phone, **kw: texts.append((phone, kw["text"])) or "sent")
    client = make_client(tmp_path)
    with client.app.state.session_factory() as session:
        user = User(phone="+8613800138000")
        session.add(user)
        session.flush()
        session.add(Notification(user_id=user.id, subject="任务已交付", body="详情"))
        session.commit()
        assert send_notifications(session, "", settings=client.app.state.settings) == 1
        session.commit()
        assert session.query(Notification).one().status == "sent"
    assert texts == [("+8613800138000", "【AutoPPT】任务已交付。详情请登录 AutoPPT 查看。")]


def test_admin_adjusts_credits_by_phone(tmp_path):
    client = make_client(tmp_path)
    code = client.post("/api/auth/code", json={"email": "admin@example.com"}).json()["dev_code"]
    admin = {"Authorization": "Bearer " + client.post("/api/auth/login", json={"email": "admin@example.com", "code": code}).json()["access_token"]}
    code = client.post("/api/auth/code", json={"phone": "13800138000"}).json()["dev_code"]
    client.post("/api/auth/login", json={"phone": "13800138000", "code": code})
    reply = client.post("/api/admin/credits/adjust", json={"email": "138 0013 8000", "credits": 50, "memo": "内测"},
                        headers={**admin, "Idempotency-Key": "p1"})
    assert reply.status_code == 200 and reply.json()["balance"] == 50
    users = client.get("/api/admin/users?email=1380013", headers=admin).json()
    assert [u["phone"] for u in users] == ["+8613800138000"]


def test_wechat_login_round_trip(tmp_path):
    client = make_client(tmp_path, "https://testserver", wechat_app_id="wx123", wechat_app_secret="secret",
                         public_base_url="https://autoppt.example")
    assert client.get("/api/auth/options").json()["wechat"] is True
    calls = []

    def wechat(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        if request.url.params["code"] == "bad":
            return httpx.Response(200, json={"errcode": 40029, "errmsg": "invalid code"})
        return httpx.Response(200, json={"access_token": "x", "openid": "o-1", "unionid": "u-1"})

    client.app.state.wechat_http = httpx.Client(transport=httpx.MockTransport(wechat))
    start = client.get("/api/auth/wechat/start")
    url = urlparse(start.json()["url"])
    query = parse_qs(url.query)
    assert url.netloc == "open.weixin.qq.com" and query["appid"] == ["wx123"] and query["scope"] == ["snsapi_login"]
    assert query["redirect_uri"] == ["https://autoppt.example/api/auth/wechat/callback"]
    state = query["state"][0]

    stranger = TestClient(client.app, base_url="https://testserver")  # same state, but not the browser that started the login
    assert stranger.get("/api/auth/wechat/callback", params={"code": "c", "state": state},
                        follow_redirects=False).status_code == 403
    assert client.get("/api/auth/wechat/callback", params={"code": "bad", "state": state},
                      follow_redirects=False).status_code == 502
    done = client.get("/api/auth/wechat/callback", params={"code": "good", "state": state}, follow_redirects=False)
    assert done.status_code == 303
    assert calls[-1] == {"appid": "wx123", "secret": "secret", "code": "good", "grant_type": "authorization_code"}
    location = done.headers["location"]
    assert location.startswith("/#/login?wechat_ticket=") and "access_token" not in location
    ticket = location.split("=", 1)[1]
    tokens = client.post("/api/auth/wechat/exchange", json={"ticket": ticket})
    assert tokens.status_code == 200
    me = client.get("/api/me", headers={"Authorization": f"Bearer {tokens.json()['access_token']}"}).json()
    assert me["wechat"] is True and me["email"] is None
    assert client.post("/api/auth/wechat/exchange", json={"ticket": ticket}).status_code == 401  # single use
    assert client.post("/api/auth/wechat/exchange", json={"ticket": "x" * 20}).status_code == 401


def test_wechat_is_off_without_credentials(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/api/auth/options").json()["wechat"] is False
    assert client.get("/api/auth/wechat/start").status_code == 404
