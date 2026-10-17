"""P6 operations: self-check, watchdog, disk guard, template pinning, health, purge, crash recovery."""

from __future__ import annotations

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
    report = run_checks(jobs_root=tmp_path, include_office=False)
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
    worker_b = ApiWorker(api, second["token"], tmp_path / "machine-b", mock=True, heartbeat_s=3600)
    result = worker_b.run_once()  # fresh machine: restores the intake snapshot, then plans
    assert result["phase"] == "planning" and result["job_state"] == "cp1_outline"
    assert not (tmp_path / "machine-b" / job["id"]).exists()  # nothing of the job stays on the machine after reporting
    phases = api.get(f"/api/jobs/{job['id']}", headers=user).json()["phases"]
    assert [p["status"] for p in phases if p["phase"] == "planning"] == ["expired", "done"]
    planning_charges = [row for row in credits(api, user)["statement"] if row["kind"] == "settle"]
    assert len(planning_charges) == 1  # planning settled exactly once
    ledger_ok(api)
