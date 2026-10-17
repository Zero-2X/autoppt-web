from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from autoppt_service.provenance import HostRecordBlocked, completed_host_records, ingest_host_record, verify_masters
from autoppt_workflow.ppt.builtin_imagegen_handoff import REQUIRED_PROMPT_POLICY, prepare_handoff

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
THREAD_ID = "01a14507-b18c-7220-9730-5974c704f5a2"
EXEC_ID = "exec-57ee411b-1c82-4343-ba2f-af147a668997"
SLIDE_PROMPT = "生成一张完整、最终可用的中文PowerPoint幻灯片图片。"


def _handoff(tmp_path: Path) -> Path:
    policy = {
        **REQUIRED_PROMPT_POLICY,
        "output_mode": "direct_final_slide_imagegen",
        "imagegen_required": True,
        "per_slide_imagegen_required": True,
    }
    prompts = tmp_path / "image-prompts.json"
    prompts.write_text(
        json.dumps({"prompt_policy": policy, "slides": [{"slide_id": "S01", "prompt_zh": SLIDE_PROMPT, "final_path": "assets/slides/S01.png"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    handoff = tmp_path / "builtin-imagegen-handoff.json"
    prepare_handoff(prompts, output=handoff)
    return handoff


def _rollout(tmp_path: Path, *, prompt: str = SLIDE_PROMPT, saved_bytes: bytes | None = PNG_1X1, thread_in_name: bool = True) -> Path:
    """Emulate the ``item_completed`` record written by current Codex hosts."""
    saved = tmp_path / "generated_images" / THREAD_ID / f"{EXEC_ID}.png"
    if saved_bytes is not None:
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(saved_bytes)
    event = {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "thread_id": THREAD_ID,
            "turn_id": "t1",
            "item": {
                "type": "Extension",
                "kind": "image_gen.generation",
                "id": EXEC_ID,
                "status": "completed",
                "revisedPrompt": prompt,
                "result": base64.b64encode(PNG_1X1).decode("ascii"),
                "savedPath": str(saved),
            },
        },
    }
    name = f"rollout-2026-10-16T22-04-57-{THREAD_ID}.jsonl" if thread_in_name else "rollout-other.jsonl"
    path = tmp_path / name
    path.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _issues(handoff: Path, rollout: Path, **kwargs: object) -> list[str]:
    with pytest.raises(HostRecordBlocked) as exc:
        ingest_host_record(handoff, "S01", rollout=rollout, record_index=0, **kwargs)
    return exc.value.issues


def test_ingest_accepts_validated_host_record_and_verifies(tmp_path: Path) -> None:
    handoff = _handoff(tmp_path)
    rollout = _rollout(tmp_path)
    assert [index for index, _ in completed_host_records(rollout)] == [0]
    result = ingest_host_record(handoff, "S01", rollout=rollout, record_index=0)
    assert result["prompt_match"] == "exact"
    sidecar = json.loads((tmp_path / "assets" / "generated" / "S01-pipeline.json").read_text(encoding="utf-8"))
    assert sidecar["provenance_kind"] == "builtin-imagegen"
    assert sidecar["generation_mode"] == "direct_final_slide_imagegen"
    assert sidecar["model"] == "unconfirmed"
    assert sidecar["host_record"]["result_sha256"] == sidecar["sha256"]
    assert sidecar["host_record"]["saved_path_sha256_match"] is True
    report = verify_masters(handoff)
    assert report["verdict"] == "pass" and report["slides_verified"] == 1


def test_ingest_blocks_when_saved_copy_differs(tmp_path: Path) -> None:
    issues = _issues(_handoff(tmp_path), _rollout(tmp_path, saved_bytes=PNG_1X1 + b"tampered"))
    assert f"host_saved_image_sha256_mismatch:{EXEC_ID}" in issues


def test_ingest_blocks_record_from_another_thread(tmp_path: Path) -> None:
    issues = _issues(_handoff(tmp_path), _rollout(tmp_path, thread_in_name=False))
    assert f"host_record_thread_mismatch:{EXEC_ID}" in issues


def test_ingest_blocks_paraphrased_prompt_unless_relaxed(tmp_path: Path) -> None:
    handoff = _handoff(tmp_path)
    rollout = _rollout(tmp_path, prompt="A plain slide.")
    assert _issues(handoff, rollout) == ["host_record_prompt_mismatch:S01:different"]
    assert ingest_host_record(handoff, "S01", rollout=rollout, record_index=0, require_prompt_match=False)["prompt_match"] == "different"


def test_verify_rejects_tampered_slide_image(tmp_path: Path) -> None:
    handoff = _handoff(tmp_path)
    ingest_host_record(handoff, "S01", rollout=_rollout(tmp_path), record_index=0)
    (tmp_path / "assets" / "slides" / "S01.png").write_bytes(PNG_1X1 + b"edited")
    report = verify_masters(handoff)
    assert report["verdict"] == "blocked"
    assert report["issues"] == ["slide_provenance_unverified:S01"]


def test_repository_gate_accepts_sidecar_in_normal_mode(tmp_path: Path) -> None:
    """The workflow's ImageGen-first gate classifies the sidecar as built-in provenance."""
    import importlib.util

    from autoppt_service.repo_paths import autoppt_repo

    script = autoppt_repo() / "autopptskills" / "scripts" / "verify_imagegen_first_gate.py"
    spec = importlib.util.spec_from_file_location("gate", script)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    handoff = _handoff(tmp_path)
    ingest_host_record(handoff, "S01", rollout=_rollout(tmp_path), record_index=0)
    records = gate.provenance_records([tmp_path / "assets" / "generated"])
    assert [(record["accepted"], record["strength"]) for record in records] == [(True, "builtin-imagegen")]
