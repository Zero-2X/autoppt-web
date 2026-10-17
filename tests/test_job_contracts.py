"""status.py, checkpoint.py, acceptance.py and usage.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from autoppt_service.acceptance import (
    AcceptanceError,
    build_acceptance_record,
    master_set_sha256,
    write_acceptance_record,
)
from autoppt_service.checkpoint import (
    CheckpointError,
    close_checkpoint,
    closed_checkpoints,
    open_checkpoint,
    pending_checkpoint,
    submit_response,
    validate_checkpoint,
)
from autoppt_service.jobfiles import job_relative, read_jsonl, service_path, sha256_file, write_json_atomic
from autoppt_service.status import (
    StatusWatcher,
    TransitionError,
    can_transition,
    new_job_state,
    phase_plan,
    transition,
    validate_agent_status,
    write_agent_status,
)
from autoppt_service.usage import phase_timer, record_agent_turn, record_office, summarize_usage


def request(tier="native_editable", **review) -> dict:
    return {
        "service": "new_deck",
        "delivery": {"tier": tier, "font_policy": "office_native_yahei"},
        "review": {"outline_checkpoint": True, "master_checkpoint": True, "native_preview_checkpoint": True, **review},
        "cover": {"title": "内涝预警"},
    }


# ---------------------------------------------------------------- status


def test_phase_plans_follow_tier_and_checkpoints():
    native = phase_plan(request())
    assert native[native.index("intake"):] == [
        "intake", "planning", "cp1_outline", "masters", "cp2_masters", "image_deck_ready",
        "reconstructing", "office_qa", "cp3_native_preview", "packaging", "delivered",
    ]
    image = phase_plan(request("image_deck", outline_checkpoint=False, master_checkpoint=False))
    assert "cp1_outline" not in image and "cp2_masters" not in image and "reconstructing" not in image
    planning = phase_plan(request("planning_package"))
    assert planning[-3:] == ["cp1_outline", "packaging", "delivered"]
    # CP3 stays even if a request tried to switch it off.
    assert "cp3_native_preview" in phase_plan(request(native_preview_checkpoint=False))
    vectorize = phase_plan({"service": "vectorize", "delivery": {"tier": "native_editable"}})
    assert vectorize[vectorize.index("intake") + 1] == "cp_v_text"


def test_transitions():
    state = new_job_state("J1", request())
    assert state["state"] == "queued"
    state = transition(state, "intake")
    with pytest.raises(TransitionError):
        transition(state, "masters")  # skipping planning
    state = transition(transition(state, "planning"), "cp1_outline")
    assert can_transition(state, "planning")  # rework edge
    blocked = transition(state, "blocked", reason="missing data")
    assert blocked["blocked_from"] == "cp1_outline"
    assert not can_transition(blocked, "masters")
    resumed = transition(blocked, "cp1_outline")
    assert resumed["blocked_from"] is None
    cancelled = transition(resumed, "cancelled")
    assert not can_transition(cancelled, "planning")
    assert [h["to"] for h in cancelled["history"]][-2:] == ["cp1_outline", "cancelled"]


def test_agent_status_and_watcher(tmp_path):
    watcher = StatusWatcher(tmp_path)
    assert watcher.poll() is None
    write_agent_status(tmp_path, "masters", current_slide="S03", slides_done=2, slides_total=10, progress=0.2)
    seen = watcher.poll()
    assert seen["valid"] and seen["status"]["current_slide"] == "S03"
    assert watcher.poll() is None  # unchanged
    with pytest.raises(ValueError):
        write_agent_status(tmp_path, "masters", "blocked")  # blocked needs a reason
    assert validate_agent_status({"schema_version": "x"})
    service_path(tmp_path, "status.json").write_text("{broken", encoding="utf-8")
    assert watcher.poll()["valid"] is False


# ---------------------------------------------------------------- checkpoints


def png(path: Path, colour="white") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 18), colour).save(path)
    return path


def native_checkpoint(job: Path, **extra) -> dict:
    png(job / "assets/slides/S01.png")
    png(job / "service/previews/S01-side.png", "red")
    payload = {
        "schema_version": "autoppt-checkpoint-v1",
        "checkpoint_id": "cp3-native-r1",
        "type": "native_preview",
        "round": 1,
        "previews": [{"path": "service/previews/S01-side.png", "kind": "side_by_side", "slide_id": "S01"}],
        "questions": [{"question_id": "q", "text": "接受吗？", "kind": "approve"}],
        "master_sha256": {"S01": sha256_file(job / "assets/slides/S01.png")},
        "editability_scope": {"native": ["text"], "image": ["photos"]},
    }
    payload.update(extra)
    return payload


def approve(**extra) -> dict:
    return {
        "checkpoint_id": "cp3-native-r1",
        "decision": "approve",
        "responder": "user",
        "user_reply": "接受预览中的非文字差异，优先真实可编辑",
        "reviewed_previews": ["service/previews/S01-side.png"],
        **extra,
    }


def test_checkpoint_validation_rules(tmp_path):
    payload = native_checkpoint(tmp_path)
    assert validate_checkpoint(tmp_path, payload) == []
    escaped = dict(payload, previews=[{"path": "../outside.png", "kind": "side_by_side", "slide_id": "S01"}])
    assert any("relative" in issue for issue in validate_checkpoint(tmp_path, escaped))
    no_scope = {k: v for k, v in payload.items() if k != "editability_scope"}
    assert any("editability_scope" in issue for issue in validate_checkpoint(tmp_path, no_scope))
    no_side = dict(payload, previews=[dict(payload["previews"][0], kind="slide")])
    assert any("side_by_side" in issue for issue in validate_checkpoint(tmp_path, no_side))
    outline = {"schema_version": "autoppt-checkpoint-v1", "checkpoint_id": "cp1", "type": "outline", "round": 1, "previews": payload["previews"], "questions": []}
    assert any("slide_manifest_sha256" in issue for issue in validate_checkpoint(tmp_path, outline))


def test_open_answer_close_cycle(tmp_path):
    opened = open_checkpoint(tmp_path, native_checkpoint(tmp_path))
    assert opened["previews"][0]["sha256"]  # filled in
    with pytest.raises(CheckpointError):
        open_checkpoint(tmp_path, native_checkpoint(tmp_path))  # one at a time
    assert pending_checkpoint(tmp_path)["checkpoint_id"] == "cp3-native-r1"
    submit_response(tmp_path, approve())
    assert pending_checkpoint(tmp_path) is None
    checkpoint, response = close_checkpoint(tmp_path)
    assert response["user_reply"].startswith("接受")
    assert not service_path(tmp_path, "checkpoint.json").exists()
    assert len(closed_checkpoints(tmp_path)) == 1


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"responder": "admin"}, "answered by the user"),
        ({"user_reply": "  "}, "own reply"),
        ({"reviewed_previews": []}, "list the previews"),
        ({"reviewed_previews": ["service/other.png"]}, "not part of this checkpoint"),
        ({"decision": "redo_slides"}, "not allowed"),
        ({"checkpoint_id": "other"}, "does not match"),
    ],
)
def test_cp3_cannot_be_auto_approved(tmp_path, changes, expected):
    open_checkpoint(tmp_path, native_checkpoint(tmp_path))
    with pytest.raises(CheckpointError) as caught:
        submit_response(tmp_path, approve(**changes))
    assert expected in str(caught.value)


def test_redo_and_paid_rounds(tmp_path):
    png(tmp_path / "assets/slides/S01.png")
    master = {
        "checkpoint_id": "cp2-r2",
        "type": "masters",
        "round": 2,
        "previews": [{"path": "assets/slides/S01.png", "kind": "slide", "slide_id": "S01"}],
        "questions": [],
        "master_sha256": {"S01": sha256_file(tmp_path / "assets/slides/S01.png")},
    }
    open_checkpoint(tmp_path, master)
    base = {"checkpoint_id": "cp2-r2", "responder": "user", "user_reply": "第一页重做"}
    with pytest.raises(CheckpointError, match="redo_slide_ids"):
        submit_response(tmp_path, {**base, "decision": "redo_slides"})
    with pytest.raises(CheckpointError, match="unknown slides"):
        submit_response(tmp_path, {**base, "decision": "redo_slides", "redo_slide_ids": ["S09"], "charge_confirmed": True})
    with pytest.raises(CheckpointError, match="charge_confirmed"):
        submit_response(tmp_path, {**base, "decision": "redo_slides", "redo_slide_ids": ["S01"]})
    submit_response(tmp_path, {**base, "decision": "redo_slides", "redo_slide_ids": ["S01"], "charge_confirmed": True})


def test_job_relative_rejects_escape(tmp_path):
    for bad in ("../x", "/etc/passwd", "C:/Windows", "a/../../x"):
        with pytest.raises(ValueError):
            job_relative(tmp_path, bad)
    assert job_relative(tmp_path, "service/x.json") == (tmp_path / "service/x.json").resolve()


# ---------------------------------------------------------------- acceptance


def test_acceptance_record_matches_repository_format(tmp_path):
    zhicang_keys = {
        "schema_version", "approved_at", "source", "user_reply", "accepted_reference",
        "policy", "scope", "master_sha256", "tier",
    }
    write_json_atomic(service_path(tmp_path, "job-request.json"), request())
    open_checkpoint(tmp_path, native_checkpoint(tmp_path))
    submit_response(tmp_path, approve(answered_at="2027-05-20T10:00:00Z"))
    close_checkpoint(tmp_path)
    record = write_acceptance_record(tmp_path, job_id="J1")
    assert zhicang_keys <= set(record)
    assert record["tier"] == "native-priority-approved"
    assert record["user_reply"] == "接受预览中的非文字差异，优先真实可编辑"
    assert record["approved_at"] == "2027-05-20"
    assert record["accepted_reference"] == "service/previews/S01-side.png"
    assert record["policy"]["font"] == "Microsoft YaHei"
    assert record["policy"]["nontext_pixel_threshold_is_release_gate"] is False
    assert record["master_sha256"] == master_set_sha256(record["master_sha256_by_slide"])
    assert json.loads(service_path(tmp_path, "acceptance.json").read_text(encoding="utf-8")) == record


def test_acceptance_refuses_unapproved_or_changed_previews(tmp_path):
    checkpoint = open_checkpoint(tmp_path, native_checkpoint(tmp_path))
    with pytest.raises(AcceptanceError, match="not approved"):
        build_acceptance_record(tmp_path, checkpoint, approve(decision="revise"), request(), job_id="J1")
    png(tmp_path / "service/previews/S01-side.png", "blue")  # preview replaced after review
    with pytest.raises(AcceptanceError, match="changed"):
        build_acceptance_record(tmp_path, checkpoint, approve(), request(), job_id="J1")
    with pytest.raises(AcceptanceError, match="no approved CP3"):
        write_json_atomic(service_path(tmp_path, "job-request.json"), request())
        write_acceptance_record(tmp_path, job_id="J1")


# ---------------------------------------------------------------- usage


def test_usage_summary_counts_turns_office_and_phases(tmp_path):
    record_agent_turn(
        tmp_path,
        "planning",
        {"profile": "p1", "returncode": 0, "thread_id": "t", "duration_s": 12.5,
         "usage": {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 200, "reasoning_output_tokens": 50}},
    )
    record_agent_turn(tmp_path, "planning", {"profile": "p2", "returncode": 1, "rate_limited": True, "usage": {}})
    # Shape written by codex_imagegen_step (no "kind").
    with service_path(tmp_path, "usage.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"phase": "masters", "status": "generated", "usage": {"input_tokens": 10, "output_tokens": 5}, "duration_s": 3}) + "\n")
        handle.write("{half-written\n")
    record_office(tmp_path, "reconstruct", 90, "render")
    with phase_timer(tmp_path, "planning"):
        pass
    with pytest.raises(RuntimeError):
        with phase_timer(tmp_path, "masters"):
            raise RuntimeError("boom")

    summary = summarize_usage(tmp_path, prices={"input_tokens": 2.0, "cached_input_tokens": 0.5, "output_tokens": 10.0, "office_minute": 0.1})
    planning = summary["phases"]["planning"]
    assert planning["agent_turns"] == 2 and planning["failed_turns"] == 1 and planning["rate_limited_turns"] == 1
    assert planning["input_tokens"] == 1000 and planning["runs"] == 1
    assert summary["phases"]["masters"]["agent_turns"] == 1 and summary["phases"]["masters"]["failed_turns"] == 0
    assert summary["phases"]["reconstruct"]["office_minutes"] == 1.5
    # 600 uncached x $2 + 400 cached x $0.5 + 200 output x $10, per million tokens.
    assert summary["cost_usd"]["planning"] == pytest.approx((600 * 2 + 400 * 0.5 + 200 * 10) / 1e6)
    assert summary["cost_usd"]["reconstruct"] == pytest.approx(0.15)
    statuses = [r.get("status") for r in read_jsonl(service_path(tmp_path, "usage.jsonl")) if r.get("kind") == "phase"]
    assert statuses == ["ok", "failed"]
