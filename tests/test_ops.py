"""P6 operations: self-check, watchdog, disk guard, template pinning, health, purge, crash recovery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import psutil
import pytest

from autoppt_service import watchdog
from autoppt_service.worker_check import capabilities, run_checks
from autoppt_service.worker_client import ApiWorker

from test_api import api, credits, docx_bytes, ledger_ok, login, request_body, upload, world  # noqa: F401  (fixtures)


def test_capabilities_follow_failed_checks():
    def check(ok, *needs):
        return {"name": "x", "ok": ok, "detail": "", "required_for": list(needs)}

    assert capabilities([check(True, "basics"), check(True, "codex"), check(True, "office")]) == ["intake", "package", "codex", "office"]
    assert capabilities([check(True, "basics"), check(False, "codex"), check(True, "office")]) == ["intake", "package", "office"]
    assert capabilities([check(False, "basics"), check(True, "codex")]) == []


def test_self_check_runs_on_this_machine(tmp_path):
    report = run_checks(jobs_root=tmp_path, include_office=False, min_free_gb=0)
    names = {c["name"] for c in report["checks"]}
    assert {"python>=3.11", "python packages", "codex cli >= 0.163.0", "worker template"} <= names
    assert "codex" not in report["capabilities"]  # no template given
    assert report["capabilities"][:2] == ["intake", "package"]


def test_watchdog_redirects_temp_and_restores(tmp_path):
    before = os.environ.get("TEMP")
    with watchdog.guarded_phase(tmp_path / "job", timeout_s=60) as report:
        assert os.environ["TEMP"] == str(tmp_path / "job" / "tmp")
        assert Path(os.environ["TMP"]).is_dir()
    assert os.environ.get("TEMP") == before
    assert not (tmp_path / "job" / "tmp").exists() and report["killed"] == []


def test_watchdog_kills_hung_processes_on_timeout(tmp_path, monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        proc = psutil.Process(child.pid)
        # Stand-in for a hung POWERPNT.EXE: only this child is treated as an Office process.
        monkeypatch.setattr(watchdog, "office_processes", lambda started_after=None: [proc] if proc.is_running() else [])
        with pytest.raises(watchdog.PhaseTimeout):
            with watchdog.guarded_phase(tmp_path / "job", timeout_s=0.3) as report:
                time.sleep(1.0)
        assert report["killed"] and child.wait(timeout=10) is not None
    finally:
        if child.poll() is None:
            child.kill()


def test_disk_guard_skips_leasing(tmp_path, api, world):
    worker = ApiWorker(api, world["worker_token"], tmp_path / "jobs", mock=True, min_free_gb=10**9)
    assert worker.run_once()["skipped"] == "disk_low"
    ok, free = watchdog.disk_ok(tmp_path, 0)
    assert ok and free > 0


def test_template_pinning(api, world):
    from autoppt_api.models import Job

    admin = world["admin"]
    other = api.post("/api/admin/workers", json={"name": "v2", "capabilities": ["all"]}, headers=admin).json()
    job = api.post("/api/jobs", json={"request": request_body(world["sha"])}, headers=world["user"]).json()
    total = api.post(f"/api/jobs/{job['id']}/estimate", headers=world["user"]).json()["total"]
    api.post(f"/api/jobs/{job['id']}/submit", json={"confirm_total": total}, headers=world["user"])
    with api.app.state.session_factory() as session:
        session.get(Job, job["id"]).template_version = "v1"
        session.commit()
    headers = {"Authorization": f"Bearer {other['token']}"}
    assert api.post("/api/worker/lease", json={"template_version": "v2"}, headers=headers).status_code == 204
    assert api.post("/api/worker/lease", json={"template_version": "v1"}, headers=headers).json()["job_id"] == job["id"]


def test_health_reports_alerts(api, world):
    admin = world["admin"]
    job = api.post("/api/jobs", json={"request": request_body(world["sha"])}, headers=world["user"]).json()
    total = api.post(f"/api/jobs/{job['id']}/estimate", headers=world["user"]).json()["total"]
    api.post(f"/api/jobs/{job['id']}/submit", json={"confirm_total": total}, headers=world["user"])
    report = api.get("/api/admin/health", headers=admin).json()
    kinds = {a["kind"] for a in report["alerts"]}
    assert report["queue"]["waiting"] == 1 and report["queue"]["by_phase"] == {"intake": 1}
    assert {"no_worker_online", "work_without_workers"} <= kinds
    world["worker"].run_once()  # the worker sends a heartbeat with its lease
    kinds = {a["kind"] for a in api.get("/api/admin/health", headers=admin).json()["alerts"]}
    assert "no_worker_online" not in kinds
    from autoppt_api.monitoring import health, notify_admins
    from autoppt_api.models import Notification

    with api.app.state.session_factory() as session:
        report = health(session)
        sent = notify_admins(session, {**report, "alerts": [{"kind": "test", "detail": "x"}]})
        session.commit()
        assert sent == 1 and session.query(Notification).filter(Notification.subject.like("AutoPPT 告警%")).count() == 1


def test_intermediate_artifacts_are_purged_after_a_week(api, world):
    from autoppt_api.lifecycle import purge_intermediate
    from autoppt_api.models import Artifact, Job, now

    job = api.post("/api/jobs", json={"request": request_body(world["sha"], "planning_package")}, headers=world["user"]).json()
    total = api.post(f"/api/jobs/{job['id']}/estimate", headers=world["user"]).json()["total"]
    api.post(f"/api/jobs/{job['id']}/submit", json={"confirm_total": total}, headers=world["user"])
    for _ in range(2):
        world["worker"].run_once()
    cp = api.get(f"/api/jobs/{job['id']}/checkpoint", headers=world["user"]).json()
    api.post(f"/api/jobs/{job['id']}/checkpoint/{cp['checkpoint_id']}/response", json={"decision": "approve", "user_reply": "好"},
             headers=world["user"])
    assert world["worker"].run_once()["job_state"] == "delivered"
    with api.app.state.session_factory() as session:
        kinds = lambda: sorted({a.kind for a in session.query(Artifact).filter(Artifact.job_id == job["id"])})  # noqa: E731
        assert kinds() == ["deliverable", "preview", "snapshot"]
        assert purge_intermediate(session, api.app.state.storage) == 0  # finished today
        session.get(Job, job["id"]).finished_at = now() - timedelta(days=8)
        session.flush()
        assert purge_intermediate(session, api.app.state.storage) > 0
        session.commit()
        assert kinds() == ["deliverable"]
    detail = api.get(f"/api/jobs/{job['id']}", headers=world["user"]).json()
    assert all(api.get(d["url"]).status_code == 200 for d in detail["deliverables"])  # downloads still work


def test_worker_crash_mid_phase_is_recovered_by_another_worker_without_double_charging(api, world, tmp_path):
    from autoppt_api.models import Job, now

    admin, user = world["admin"], world["user"]
    job = api.post("/api/jobs", json={"request": request_body(world["sha"], "image_deck")}, headers=user).json()
    total = api.post(f"/api/jobs/{job['id']}/estimate", headers=user).json()["total"]
    api.post(f"/api/jobs/{job['id']}/submit", json={"confirm_total": total}, headers=user)
    assert world["worker"].run_once()["job_state"] == "planning"  # worker A: intake, snapshot uploaded
    # Worker A leases planning and dies without reporting.
    lease = api.post("/api/worker/lease", json={}, headers={"Authorization": f"Bearer {world['worker_token']}"}).json()
    assert lease["phase"] == "planning"
    with api.app.state.session_factory() as session:
        session.get(Job, job["id"]).lease_expires_at = now() - timedelta(seconds=1)
        session.commit()
    second = api.post("/api/admin/workers", json={"name": "b", "capabilities": ["all"]}, headers=admin).json()
    worker_b = ApiWorker(api, second["token"], tmp_path / "machine-b", mock=True, heartbeat_s=3600, min_free_gb=0)
    result = worker_b.run_once()  # fresh machine: restores the intake snapshot, then plans
    assert result["phase"] == "planning" and result["job_state"] == "cp1_outline"
    assert not (tmp_path / "machine-b" / job["id"]).exists()  # nothing of the job stays on the machine after reporting
    phases = api.get(f"/api/jobs/{job['id']}", headers=user).json()["phases"]
    assert [p["status"] for p in phases if p["phase"] == "planning"] == ["expired", "done"]
    planning_charges = [row for row in credits(api, user)["statement"] if row["kind"] == "settle"]
    assert len(planning_charges) == 1  # planning settled exactly once
    ledger_ok(api)


def test_reconstruction_needs_both_codex_and_office(api, world):
    from autoppt_api.models import Job

    admin = world["admin"]
    office_only = api.post("/api/admin/workers", json={"name": "office-only", "capabilities": ["office"]}, headers=admin).json()
    full = api.post("/api/admin/workers", json={"name": "full", "capabilities": ["codex", "office"]}, headers=admin).json()
    job = api.post("/api/jobs", json={"request": request_body(world["sha"])}, headers=world["user"]).json()
    total = api.post(f"/api/jobs/{job['id']}/estimate", headers=world["user"]).json()["total"]
    api.post(f"/api/jobs/{job['id']}/submit", json={"confirm_total": total}, headers=world["user"])
    with api.app.state.session_factory() as session:
        row = session.get(Job, job["id"])
        row.state, row.pending_phase = "reconstructing", "reconstruct"
        session.commit()
    assert api.post("/api/worker/lease", json={}, headers={"Authorization": f"Bearer {office_only['token']}"}).status_code == 204
    assert api.post("/api/worker/lease", json={}, headers={"Authorization": f"Bearer {full['token']}"}).json()["phase"] == "reconstruct"


def test_job_cost_alert_on_tokens_and_on_cost_over_estimate(api, world):
    from autoppt_api.models import PricingRule, UsageRecord
    from autoppt_api.monitoring import health
    from autoppt_api.pricing import DEFAULT_RULES

    job = api.post("/api/jobs", json={"request": request_body(world["sha"])}, headers=world["user"]).json()
    total = api.post(f"/api/jobs/{job['id']}/estimate", headers=world["user"]).json()["total"]
    api.post(f"/api/jobs/{job['id']}/submit", json={"confirm_total": total}, headers=world["user"])
    with api.app.state.session_factory() as session:
        session.add(UsageRecord(job_id=job["id"], phase="reconstruct", kind="agent_turn", input_tokens=41_000_000,
                                cached_input_tokens=38_000_000, output_tokens=280_000, business_key="u1"))
        session.commit()
        assert not [a for a in health(session)["alerts"] if a["kind"] == "job_cost"]  # 41 M: under 60 M, no rates
        session.add(UsageRecord(job_id=job["id"], phase="reconstruct", kind="agent_turn", input_tokens=25_000_000,
                                business_key="u2"))
        session.commit()
        alert = next(a for a in health(session)["alerts"] if a["kind"] == "job_cost")
        assert f"{job['id'][:8]} used 66.0 M input tokens" in alert["detail"]

        # With API-key rates (D2), a job whose cost exceeds 1.5x its estimate's value is reported too.
        rules = {**DEFAULT_RULES, "credit_value_cents": 10,
                 "cost_rates": {**DEFAULT_RULES["cost_rates"], "input_per_million_cents": 1000}}
        for rule in session.query(PricingRule).filter(PricingRule.active.is_(True)):
            rule.active = False
        session.add(PricingRule(version=99, active=True, rules=rules, note="test"))
        session.commit()
        alert = next(a for a in health(session)["alerts"] if a["kind"] == "job_cost")
        assert "against an estimate worth" in alert["detail"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Firewall script")
def test_worker_firewall_plan_and_refusal():
    script = Path(__file__).resolve().parents[1] / "scripts" / "worker_firewall.ps1"

    def run(*args: str):
        command = f"& '{script}' -AllowHost localhost,127.0.0.1 -ModelHost example.invalid -SystemHost '' {' '.join(args)}"
        return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)

    plan = json.loads(run().stdout)  # no -Apply: prints the plan, changes nothing
    assert plan["default_outbound"] == "Block" and plan["unresolved"] == ["example.invalid"]
    allowed = next(rule for rule in plan["rules"] if rule["name"].endswith("allowed hosts"))
    assert "127.0.0.1" in allowed["remote_address"].split(",")
    refused = run("-Apply")  # an unresolvable host is refused before any rule is touched
    assert refused.returncode != 0 and "cannot resolve example.invalid" in refused.stderr + refused.stdout


def test_overlapping_phases_restore_temp_to_a_folder_that_exists(tmp_path, monkeypatch):
    # Three workers in one process (the soak test): phases start and end in any order.
    monkeypatch.setenv("TEMP", str(tmp_path / "original"))
    monkeypatch.setenv("TMP", str(tmp_path / "original"))
    a = watchdog.guarded_phase(tmp_path / "job-a", timeout_s=60)
    b = watchdog.guarded_phase(tmp_path / "job-b", timeout_s=60)
    a.__enter__()
    b.__enter__()
    assert os.environ["TEMP"] == str(tmp_path / "job-b" / "tmp")
    a.__exit__(None, None, None)  # A ends first: TEMP stays on B's live folder, never on A's deleted one
    assert os.environ["TEMP"] == str(tmp_path / "job-b" / "tmp") and (tmp_path / "job-b" / "tmp").is_dir()
    b.__exit__(None, None, None)
    assert os.environ["TEMP"] == os.environ["TMP"] == str(tmp_path / "original")


def test_worker_retries_transport_errors_but_never_a_lease(tmp_path):
    import httpx

    calls = []

    class Flaky:
        def __init__(self, failures):
            self.failures = failures

        def post(self, url, **kwargs):
            calls.append(url)
            if self.failures:
                self.failures -= 1
                raise httpx.ReadError("[WinError 10053] connection aborted")
            return httpx.Response(200, json={"ok": True})

    worker = ApiWorker(Flaky(2), "t", tmp_path, mock=True, min_free_gb=0)
    assert worker._post("/api/worker/jobs/j1/heartbeat", {}) == {"ok": True}
    assert calls == ["/api/worker/jobs/j1/heartbeat"] * 3
    calls.clear()
    worker = ApiWorker(Flaky(1), "t", tmp_path, mock=True, min_free_gb=0)
    with pytest.raises(httpx.ReadError):
        worker._post("/api/worker/lease", {})
    assert calls == ["/api/worker/lease"]


def test_health_separates_retry_backoff_from_account_limits(api, world):
    from autoppt_api.models import Job, JobEvent, now
    from autoppt_api.monitoring import health

    job = api.post("/api/jobs", json={"request": request_body(world["sha"])}, headers=world["user"]).json()
    total = api.post(f"/api/jobs/{job['id']}/estimate", headers=world["user"]).json()["total"]
    api.post(f"/api/jobs/{job['id']}/submit", json={"confirm_total": total}, headers=world["user"])
    with api.app.state.session_factory() as session:
        row = session.get(Job, job["id"])
        row.not_before = now() + timedelta(minutes=2)
        session.add(JobEvent(job_id=row.id, kind="retry", message="intake failed, retry 2: ReadError"))
        session.commit()
        kinds = {a["kind"] for a in health(session)["alerts"]}
    assert "retry_backoff" in kinds and "account_limits" not in kinds
