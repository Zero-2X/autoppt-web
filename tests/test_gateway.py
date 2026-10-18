"""Model gateway for API-key mode (D2): pooled provider keys, per-job tokens, budgets, metering."""

from __future__ import annotations

import json
import sys
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from autoppt_api.app import create_app
from autoppt_api.config import Settings
from autoppt_api.gateway import KeyPool, ProviderSecret, UsageMeter
from test_api import docx_bytes, login, request_body, upload

COMPLETED = {"type": "response.completed", "response": {"id": "resp_1", "usage": {
    "input_tokens": 1200, "input_tokens_details": {"cached_tokens": 1000}, "output_tokens": 80}}}


def sse(*events: dict) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def test_key_pool_rotates_caps_and_rests():
    pool = KeyPool([ProviderSecret("a", "ka", max_concurrency=1), ProviderSecret("b", "kb", max_concurrency=1)])
    first, second = pool.acquire(), pool.acquire()
    assert (first.label, second.label) == ("a", "b") and pool.acquire() is None  # both at their cap
    pool.release(first, failed_status=429, retry_after=30)
    pool.release(second)
    assert pool.acquire().label == "b" and pool.acquire() is None  # a rests, b is busy again
    stats = {row["label"]: row for row in pool.stats()}
    assert stats["a"]["errors"] == 1 and stats["a"]["resting_s"] > 20 and "ka" not in json.dumps(pool.stats())


def test_usage_meter_reads_streams_and_json():
    meter = UsageMeter()
    body = sse({"type": "response.created"}, {"type": "response.output_text.delta", "delta": "hi"}, COMPLETED)
    for start in range(0, len(body), 7):  # arbitrary chunk boundaries
        meter.feed(body[start:start + 7])
    assert meter.finish() == {"input_tokens": 1200, "cached_input_tokens": 1000, "output_tokens": 80}
    plain = UsageMeter()
    plain.feed(json.dumps({"id": "r", "usage": {"input_tokens": 5, "output_tokens": 2}}).encode())
    assert plain.finish() == {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 2}


@pytest.fixture()
def gateway(tmp_path):
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps([{"label": "k1", "key": "sk-one"}, {"label": "k2", "key": "sk-two"}]), encoding="utf-8")
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'api.db').as_posix()}", storage_dir=tmp_path / "storage",
                        dev_echo_codes=True, admin_emails=("admin@example.com",), provider_keys_file=str(keys),
                        gateway_upstream_url="https://upstream.example/v1", gateway_job_budget_input_tokens=3000,
                        public_base_url="https://autoppt.example")
    client = TestClient(create_app(settings))
    seen: list[httpx.Request] = []
    plan = {"k1_status": 429}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers["authorization"] == "Bearer sk-one" and plan["k1_status"]:
            return httpx.Response(plan["k1_status"], headers={"retry-after": "120"}, json={"error": "rate limited"})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-x"}]})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(COMPLETED))

    client.app.state.gateway_transport = httpx.MockTransport(upstream)
    admin, user = login(client, "admin@example.com"), login(client, "student@example.com")
    worker = client.post("/api/admin/workers", json={"name": "w", "capabilities": ["all"]}, headers=admin).json()
    client.post("/api/admin/credits/adjust", json={"email": "student@example.com", "credits": 500, "memo": "t"},
                headers={**admin, "Idempotency-Key": "g1"})
    client.tmp = tmp_path
    sha = upload(client, user, "论文.docx", docx_bytes())
    job = client.post("/api/jobs", json={"request": request_body(sha)}, headers=user).json()
    total = client.post(f"/api/jobs/{job['id']}/estimate", headers=user).json()["total"]
    client.post(f"/api/jobs/{job['id']}/submit", json={"confirm_total": total}, headers=user)
    wh = {"Authorization": f"Bearer {worker['token']}"}
    lease = client.post("/api/worker/lease", json={"template_version": ""}, headers=wh).json()
    return {"client": client, "admin": admin, "user": user, "worker": wh, "lease": lease, "seen": seen, "plan": plan}


def test_job_token_forwards_with_pooled_keys_and_meters_usage(gateway):
    client, lease, seen = gateway["client"], gateway["lease"], gateway["seen"]
    issued = client.post(f"/api/worker/jobs/{lease['job_id']}/model-token",
                         json={"lease_token": lease["lease_token"], "phase": "planning"}, headers=gateway["worker"]).json()
    assert issued["base_url"] == "https://autoppt.example/gateway/v1" and issued["budget_input_tokens"] == 3000
    headers = {"Authorization": f"Bearer {issued['token']}"}
    reply = client.post("/gateway/v1/responses", content=b'{"model":"gpt-x","stream":true}', headers=headers)
    assert reply.status_code == 200 and b"response.completed" in reply.content
    assert [r.headers["authorization"] for r in seen] == ["Bearer sk-one", "Bearer sk-two"]  # 429 -> next key
    assert seen[-1].content == b'{"model":"gpt-x","stream":true}' and str(seen[-1].url) == "https://upstream.example/v1/responses"
    assert client.get("/gateway/v1/models", headers=headers).json()["data"][0]["id"] == "gpt-x"
    report = client.get("/api/admin/gateway", headers=gateway["admin"]).json()
    keys = {k["label"]: k for k in report["keys"]}
    assert keys["k1"]["errors"] == 1 and keys["k1"]["resting_s"] > 100 and "sk-" not in json.dumps(report)
    token = report["tokens"][0]
    assert (token["input_tokens"], token["cached_input_tokens"], token["output_tokens"], token["requests"]) == (1200, 1000, 80, 2)

    # Budget: 3000 input tokens per job; the third call crosses it, the fourth is refused.
    client.post("/gateway/v1/responses", content=b"{}", headers=headers)
    client.post("/gateway/v1/responses", content=b"{}", headers=headers)
    refused = client.post("/gateway/v1/responses", content=b"{}", headers=headers)
    assert refused.status_code == 429 and "budget" in refused.text
    again = client.post(f"/api/worker/jobs/{lease['job_id']}/model-token",
                        json={"lease_token": lease["lease_token"], "phase": "planning"}, headers=gateway["worker"])
    assert again.status_code == 429  # a new token does not reset the job's budget


def test_gateway_refuses_bad_tokens_paths_and_stopped_jobs(gateway):
    client, lease = gateway["client"], gateway["lease"]
    assert client.post("/gateway/v1/responses", content=b"{}").status_code == 401
    assert client.post("/gateway/v1/responses", content=b"{}", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post(f"/api/worker/jobs/{lease['job_id']}/model-token",
                       json={"lease_token": "wrong", "phase": "planning"}, headers=gateway["worker"]).status_code >= 400
    issued = client.post(f"/api/worker/jobs/{lease['job_id']}/model-token",
                         json={"lease_token": lease["lease_token"], "phase": "planning"}, headers=gateway["worker"]).json()
    headers = {"Authorization": f"Bearer {issued['token']}"}
    assert client.post("/gateway/v1/files", content=b"{}", headers=headers).status_code == 404
    newer = client.post(f"/api/worker/jobs/{lease['job_id']}/model-token",
                        json={"lease_token": lease["lease_token"], "phase": "planning"}, headers=gateway["worker"]).json()
    assert client.post("/gateway/v1/responses", content=b"{}", headers=headers).status_code == 401  # replaced
    with client.app.state.session_factory() as session:
        from autoppt_api.models import ModelToken, now

        token = session.query(ModelToken).filter(ModelToken.revoked.is_(False)).one()
        token.expires_at = now() - timedelta(seconds=1)
        session.commit()
    assert client.post("/gateway/v1/responses", content=b"{}",
                       headers={"Authorization": f"Bearer {newer['token']}"}).status_code == 401  # expired
    gateway["plan"]["k1_status"] = 503
    assert client.get("/api/admin/gateway", headers=gateway["admin"]).json()["enabled"] is True


def test_gateway_is_off_without_keys(tmp_path):
    client = TestClient(create_app(Settings(database_url=f"sqlite:///{(tmp_path / 'a.db').as_posix()}",
                                            storage_dir=tmp_path / "s")))
    assert client.post("/gateway/v1/responses", content=b"{}", headers={"Authorization": "Bearer x"}).status_code == 404


def test_codex_runs_against_the_gateway_with_the_lease_token(tmp_path, monkeypatch):
    from autoppt_service.codex_exec import CodexProfile, run_codex_exec
    from autoppt_service.worker_client import gateway_environment

    script = tmp_path / "fake.py"
    script.write_text("import sys, json, os; open(sys.argv[0] + '.argv', 'w').write(json.dumps("
                      "{'argv': sys.argv[1:], 'token': os.environ.get('AUTOPPT_GATEWAY_TOKEN')}))", encoding="utf-8")
    profile = CodexProfile(name="api", codex_home=tmp_path / "home", gateway=True)
    (tmp_path / "home").mkdir()
    missing = run_codex_exec("hi", cwd=tmp_path / "job", profile=profile, run_dir=tmp_path / "r0",
                             command=[sys.executable, str(script)])
    assert missing.errors and missing.errors[0].startswith("gateway_token_missing")
    with gateway_environment({"base_url": "https://autoppt.example/gateway/v1", "token": "t-123"}):
        run_codex_exec("hi", cwd=tmp_path / "job", profile=profile, run_dir=tmp_path / "r1",
                       command=[sys.executable, str(script)])
    import os

    assert "AUTOPPT_GATEWAY_TOKEN" not in os.environ  # only for the phase
    seen = json.loads((tmp_path / "fake.py.argv").read_text(encoding="utf-8"))
    argv = seen["argv"]
    assert seen["token"] == "t-123" and argv[-1] == "-"
    assert 'model_providers.autoppt.base_url="https://autoppt.example/gateway/v1"' in argv
    assert 'model_providers.autoppt.env_key="AUTOPPT_GATEWAY_TOKEN"' in argv and 'model_provider="autoppt"' in argv
