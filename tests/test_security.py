"""P7: leak guard, lesson sanitising, trace purge, encryption at rest, repository secret scan."""

from __future__ import annotations

import io
import json
import re
import subprocess
import zipfile
from pathlib import Path

import pytest

from autoppt_service.leakguard import profile_secrets, sanitize_lesson, scan_outputs, scan_text
from autoppt_service.worker_client import ApiWorker, snapshot_bytes

from test_api import api, credits, docx_bytes, ledger_ok, login, request_body, upload, world  # noqa: F401  (fixtures)

FAKE_ACCESS = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhY2NvdW50LXRlc3QifQ.c2lnbmF0dXJlLWZvci10ZXN0cw"
FAKE_REFRESH = "rt_test_ZmFrZS1yZWZyZXNoLXRva2VuLWZvci10ZXN0aW5n"


def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": FAKE_ACCESS, "refresh_token": FAKE_REFRESH,
                                                           "account_id": "short"}}), encoding="utf-8")
    (home / "auth.json.bootstrap-bak-1").write_text(json.dumps({"tokens": {"refresh_token": "rt_old_backup_token_value_1234"}}),
                                                    encoding="utf-8")
    return home


def test_profile_secrets_cover_backups_and_jwt_segments(tmp_path):
    secrets = profile_secrets([fake_home(tmp_path)])
    assert FAKE_ACCESS in secrets and FAKE_REFRESH in secrets and "rt_old_backup_token_value_1234" in secrets
    assert "eyJzdWIiOiJhY2NvdW50LXRlc3QifQ" in secrets  # the payload segment alone is also caught
    assert "short" not in secrets


def test_scan_finds_leaks_in_json_and_pptx_but_not_in_uploads(tmp_path):
    secrets = profile_secrets([fake_home(tmp_path)])
    job = tmp_path / "job"
    (job / "workspace/presentation_handoff").mkdir(parents=True)
    (job / "source-materials").mkdir()
    (job / "output").mkdir()
    (job / "workspace/presentation_handoff/deck-plan.json").write_text(
        json.dumps({"slides": [{"exact_text": [f"token {FAKE_REFRESH}"]}]}), encoding="utf-8")
    (job / "source-materials/f_01__paper.md").write_text(f"the user's own text {FAKE_REFRESH}", encoding="utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", f"<a:t>{FAKE_ACCESS.split('.')[1]}</a:t>")
    (job / "output/native-deck.pptx").write_bytes(buffer.getvalue())
    findings = {f["file"]: f["kinds"] for f in scan_outputs(job, secrets)}
    assert set(findings) == {"workspace/presentation_handoff/deck-plan.json", "output/native-deck.pptx"}
    assert findings["workspace/presentation_handoff/deck-plan.json"][0] == "profile_secret"
    assert scan_text("see C:/Users/x/.codex/auth.json", set()) == ["auth_file"]
    assert scan_text("normal 城市内涝 slide text 47 分钟", secrets) == []


def test_worker_refuses_to_upload_leaked_output(api, world, tmp_path, monkeypatch):
    home = fake_home(tmp_path)
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps([{"name": "p", "codex_home": str(home)}]), encoding="utf-8")
    import autoppt_service.worker_client as client_module

    real_run_phase = client_module.run_phase

    def leaking_run_phase(job_dir, phase, **kwargs):
        result = real_run_phase(job_dir, phase, **kwargs)
        if phase == "planning":  # a prompt-injected agent copied the account token onto a slide
            brief = job_dir / "workspace/presentation_handoff/slide_brief.json"
            data = json.loads(brief.read_text(encoding="utf-8"))
            data["slides"][0]["title"] = FAKE_ACCESS
            brief.write_text(json.dumps(data), encoding="utf-8")
        return result

    monkeypatch.setattr(client_module, "run_phase", leaking_run_phase)
    worker = ApiWorker(api, world["worker_token"], tmp_path / "jobs", mock=True, profiles=profiles, heartbeat_s=3600)
    job = api.post("/api/jobs", json={"request": request_body(world["sha"], "planning_package")}, headers=world["user"]).json()
    total = api.post(f"/api/jobs/{job['id']}/estimate", headers=world["user"]).json()["total"]
    api.post(f"/api/jobs/{job['id']}/submit", json={"confirm_total": total}, headers=world["user"])
    assert worker.run_once()["job_state"] == "planning"
    result = worker.run_once()
    assert result["failed"] == "leak_guard" and result["findings"][0]["kinds"][0] == "profile_secret"
    detail = api.get(f"/api/jobs/{job['id']}", headers=world["user"]).json()
    assert detail["state"] == "failed" and detail["hold"]["status"] == "closed"
    assert not (tmp_path / "jobs" / job["id"]).exists()
    from autoppt_api.models import Artifact, JobPhase

    with api.app.state.session_factory() as session:
        planning = session.query(JobPhase).filter_by(job_id=job["id"], phase="planning").one()
        assert "security" in planning.result["error"] and FAKE_ACCESS not in planning.result["error"]
        snapshots = session.query(Artifact).filter_by(job_id=job["id"], kind="snapshot").count()
        assert snapshots == 1  # only the intake snapshot; nothing from the leaking phase was uploaded
    ledger_ok(api)


def test_lessons_are_sanitised():
    lesson = {"problem": "封面“城市内涝预警研究”标题 47 分钟被截断", "method_change": "缩小字号到 28 pt，见 D:/autoppt-jobs/J1/x.png",
              "lesson": f"不要复制 {FAKE_ACCESS}", "customer_notes": "张三 的论文", "evidence": "论文.docx 第 3 页"}
    clean = sanitize_lesson(lesson, ["城市内涝预警研究", "论文.docx", "张三"])
    text = json.dumps(clean, ensure_ascii=False)
    for leaked in ("城市内涝预警研究", "47", "28", "D:/autoppt-jobs", FAKE_ACCESS, "张三", "论文.docx"):
        assert leaked not in text
    assert "customer_notes" not in clean and clean["sanitized"] is True


def test_snapshot_excludes_raw_agent_events(tmp_path):
    job = tmp_path / "job"
    run = job / "service/codex-runs/planning-r1-a1"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text("{}", encoding="utf-8")
    (run / "stderr.txt").write_text("x", encoding="utf-8")
    (run / "prompt.txt").write_text("phase planning", encoding="utf-8")
    names = zipfile.ZipFile(io.BytesIO(snapshot_bytes(job))).namelist()
    assert names == ["service/codex-runs/planning-r1-a1/prompt.txt"]


def test_codex_traces_are_purged_after_reporting(tmp_path):
    home = tmp_path / "home"
    rollout = home / "sessions/2026/10/17/rollout-2026-10-17T00-00-00-thread-abc.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}", encoding="utf-8")
    images = home / "generated_images/thread-abc"
    images.mkdir(parents=True)
    (images / "exec-1.png").write_bytes(b"x")
    other = home / "sessions/2026/10/17/rollout-2026-10-17T00-00-00-thread-other.jsonl"
    other.write_text("{}", encoding="utf-8")
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps([{"name": "p", "codex_home": str(home)}]), encoding="utf-8")
    job = tmp_path / "job"
    (job / "service").mkdir(parents=True)
    (job / "service/usage.jsonl").write_text(json.dumps({"profile": "p", "thread_id": "thread-abc"}) + "\n", encoding="utf-8")
    ApiWorker(None, "t", tmp_path, profiles=profiles)._purge_codex_traces(job)
    assert not rollout.exists() and not images.exists() and other.exists()  # only this job's threads


def test_encryption_at_rest(tmp_path):
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    from autoppt_api.app import create_app
    from autoppt_api.config import Settings

    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'a.db').as_posix()}", storage_dir=tmp_path / "s",
                        dev_echo_codes=True, storage_key=Fernet.generate_key().decode())
    client = TestClient(create_app(settings))
    headers = login(client, "enc@example.com")
    data = docx_bytes()
    sha = upload(client, headers, "论文.docx", data)
    blob = next((tmp_path / "s" / "blobs").rglob(sha))
    assert blob.read_bytes() != data and b"word/document.xml" not in blob.read_bytes()
    assert client.app.state.storage.read_bytes(f"blobs/{sha[:2]}/{sha}") == data
    from autoppt_api.security import sign_url_token

    token = sign_url_token(settings.secret_key, purpose="download", ref=f"{sha}|论文.docx", ttl_s=60)
    response = client.get(f"/api/files/{token}")
    assert response.status_code == 200 and response.content == data


def test_repository_contains_no_secrets():
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    patterns = [re.compile(p) for p in (r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}", r"\bsk-[A-Za-z0-9]{32,}",
                                        r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"(?i)aws_secret_access_key\s*=")]
    offenders = []
    for name in tracked:
        path = root / name
        if path.suffix in {".png", ".jpg", ".pptx", ".zip"} or not path.is_file() or name == "tests/test_security.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        offenders += [f"{name}: {p.pattern[:20]}" for p in patterns if p.search(text)]
    assert offenders == []


def test_codex_runs_force_the_restricted_token_sandbox_on_windows(monkeypatch):
    from autoppt_service import codex_exec

    monkeypatch.setattr(codex_exec.os, "name", "nt")
    monkeypatch.delenv("AUTOPPT_CODEX_WINDOWS_SANDBOX", raising=False)
    args = codex_exec.build_exec_args(sandbox="workspace-write")
    assert args[args.index("-s") + 1] == "workspace-write"
    assert 'windows.sandbox="unelevated"' in args  # "elevated" without setup ran unsandboxed in the probe
    monkeypatch.setenv("AUTOPPT_CODEX_WINDOWS_SANDBOX", "elevated")
    assert 'windows.sandbox="elevated"' in codex_exec.build_exec_args()
    monkeypatch.setenv("AUTOPPT_CODEX_WINDOWS_SANDBOX", "")
    assert not any(a.startswith("windows.sandbox") for a in codex_exec.build_exec_args())
    monkeypatch.setattr(codex_exec.os, "name", "posix")
    assert not any(a.startswith("windows.sandbox") for a in codex_exec.build_exec_args(windows_sandbox="auto"))
