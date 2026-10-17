from __future__ import annotations

import json
import sys
from pathlib import Path

from autoppt_service.codex_exec import CodexProfile, ProfilePool, build_exec_args, parse_events
from autoppt_service.codex_imagegen_step import PROMPT_CLOSE, PROMPT_OPEN, build_agent_prompt, generate_masters
from autoppt_workflow.ppt.builtin_imagegen_handoff import REQUIRED_PROMPT_POLICY

# The fake CLI emulates what ``codex exec`` leaves behind: JSONL events on
# stdout plus a rollout under ``$CODEX_HOME/sessions`` holding one
# ``image_gen.generation`` record and the saved PNG.  No image backend runs.
FAKE_CODEX = r'''
import base64, json, os, sys, uuid
from pathlib import Path

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
home = Path(os.environ["CODEX_HOME"])
state = home / "calls.txt"
calls = int(state.read_text()) + 1 if state.exists() else 1
state.write_text(str(calls))
mode = os.environ.get("FAKE_CODEX_MODE", "ok")
prompt = sys.stdin.buffer.read().decode("utf-8")  # strict: the runner must send UTF-8
slide_prompt = prompt.split("<<<SLIDE_PROMPT\n", 1)[1].split("\nSLIDE_PROMPT>>>", 1)[0]
thread = str(uuid.uuid4())
print(json.dumps({"type": "thread.started", "thread_id": thread}))
if mode == "rate_limit":
    print(json.dumps({"type": "error", "message": "You've hit your usage limit."}))
    print(json.dumps({"type": "turn.failed", "error": {"message": "usage limit"}}))
    sys.exit(1)
if mode == "paraphrase" or (mode == "paraphrase_once" and calls == 1):
    slide_prompt = "A plain slide."
records = 2 if mode == "two_calls" else 1
lines = []
for _ in range(records):
    exec_id = f"exec-{uuid.uuid4()}"
    saved = home / "generated_images" / thread / f"{exec_id}.png"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_bytes(PNG)
    lines.append(json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "thread_id": thread, "turn_id": "t1",
        "item": {"type": "Extension", "kind": "image_gen.generation", "id": exec_id, "status": "completed",
                 "revisedPrompt": slide_prompt, "result": base64.b64encode(PNG).decode(), "savedPath": str(saved)}}}, ensure_ascii=False))
rollout = home / "sessions" / "2026" / "10" / "16" / f"rollout-2026-10-16T00-00-00-{thread}.jsonl"
rollout.parent.mkdir(parents=True, exist_ok=True)
rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 5}}))
'''


def _fake_command(tmp_path: Path) -> list[str]:
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE_CODEX, encoding="utf-8")
    return [sys.executable, str(script)]


def _prompts(workspace: Path, count: int = 2) -> Path:
    policy = {
        **REQUIRED_PROMPT_POLICY,
        "output_mode": "direct_final_slide_imagegen",
        "imagegen_required": True,
        "per_slide_imagegen_required": True,
    }
    slides = [
        {"slide_id": f"S{index:02d}", "prompt_zh": f"第{index}页：生成一张完整的中文学术PPT页面，标题“测试{index}”。", "final_path": f"assets/slides/S{index:02d}.png"}
        for index in range(1, count + 1)
    ]
    path = workspace / "image-prompts.json"
    workspace.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"deck_title": "测试", "prompt_policy": policy, "slides": slides}, ensure_ascii=False), encoding="utf-8")
    return path


def _profiles(tmp_path: Path, *names: str) -> list[CodexProfile]:
    profiles = [CodexProfile(name=name, codex_home=tmp_path / "homes" / name) for name in names]
    for profile in profiles:
        profile.codex_home.mkdir(parents=True, exist_ok=True)
    return profiles


def test_agent_prompt_wraps_slide_prompt_verbatim() -> None:
    text = build_agent_prompt("标题“A”\n第二行")
    assert f"{PROMPT_OPEN}\n标题“A”\n第二行\n{PROMPT_CLOSE}" in text


def test_exec_args_never_use_ephemeral_sessions() -> None:
    args = build_exec_args(reasoning_effort="low")
    assert "--ephemeral" not in args
    assert args[-1] == "-" and "--json" in args


def test_parse_events_reads_thread_usage_and_errors() -> None:
    thread, usage, errors = parse_events(
        [
            '{"type":"thread.started","thread_id":"t-1"}',
            '{"type":"error","message":"boom"}',
            '{"type":"turn.completed","usage":{"input_tokens":3}}',
            "not json",
        ]
    )
    assert (thread, usage, errors) == ("t-1", {"input_tokens": 3}, ["boom"])


def test_generate_masters_rotates_profiles_and_passes_handoff(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "ok")
    workspace = tmp_path / "job"
    report = generate_masters(_prompts(workspace), profiles=_profiles(tmp_path, "a", "b"), command=_fake_command(tmp_path))
    assert report["handoff_verdict"] == "pass", report["handoff_issues"]
    assert [slide["attempts"][0]["profile"] for slide in report["slides"]] == ["a", "b"]
    sidecar = json.loads((workspace / "assets" / "generated" / "S01-pipeline.json").read_text(encoding="utf-8"))
    assert sidecar["provenance_strength"] == "validated-host-record"
    assert sidecar["prompt_match"] == "exact"
    usage = (workspace / "service" / "usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(usage) == 2 and json.loads(usage[0])["usage"]["input_tokens"] == 100


def test_generate_masters_retries_after_paraphrased_prompt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "paraphrase_once")
    workspace = tmp_path / "job"
    report = generate_masters(_prompts(workspace, 1), profiles=_profiles(tmp_path, "a"), command=_fake_command(tmp_path))
    attempts = report["slides"][0]["attempts"]
    assert "host_record_prompt_mismatch:S01" in attempts[0]["issues"]
    assert attempts[1]["status"] == "generated"
    second_prompt = (workspace / "service" / "codex-runs" / "S01-attempt2" / "prompt.txt").read_bytes()
    assert b"Copy it exactly this time" in second_prompt
    assert b"\r\n" not in second_prompt
    assert report["handoff_verdict"] == "pass"


def test_generate_masters_rests_rate_limited_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "rate_limit")
    workspace = tmp_path / "job"
    report = generate_masters(_prompts(workspace, 1), profiles=_profiles(tmp_path, "a", "b"), command=_fake_command(tmp_path))
    attempts = report["slides"][0]["attempts"]
    assert [item["profile"] for item in attempts] == ["a", "b"]
    assert all(item["rate_limited"] for item in attempts)
    assert report["handoff_verdict"] == "blocked"


def test_generate_masters_blocks_thread_with_two_imagegen_calls(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "two_calls")
    workspace = tmp_path / "job"
    report = generate_masters(_prompts(workspace, 1), profiles=_profiles(tmp_path, "a"), command=_fake_command(tmp_path), max_attempts=1)
    assert "expected_one_host_imagegen_call:2" in report["slides"][0]["attempts"][0]["issues"]
    assert report["slides"][0]["status"] == "blocked"


def test_generate_masters_reports_profile_that_was_never_logged_in(tmp_path: Path) -> None:
    workspace = tmp_path / "job"
    missing = CodexProfile(name="new", codex_home=tmp_path / "homes" / "new")
    report = generate_masters(_prompts(workspace, 1), profiles=[missing], command=_fake_command(tmp_path), max_attempts=1)
    assert any(issue.startswith("codex_home_missing:") for issue in report["slides"][0]["attempts"][0]["issues"])


def test_profile_pool_skips_resting_profiles(tmp_path: Path) -> None:
    a, b = _profiles(tmp_path, "a", "b")
    pool = ProfilePool([a, b], cooldown_s=60)
    pool.rest(a)
    assert pool.acquire() == b
    assert pool.acquire() == b
    pool.rest(b)
    assert pool.acquire() is None
