"""Checkpoint files: the agent asks, stops, and the user's answer comes back as a file.

* The agent writes ``service/checkpoint.json`` (type, previews, questions) and
  ends its turn.  It never asks the user directly.
* The backend writes the user's answer to ``service/checkpoint-response.json``
  through :func:`submit_response`, which validates it against the open
  checkpoint.
* The Worker reads the answer, archives both files under
  ``service/checkpoints/<checkpoint_id>/`` (:func:`close_checkpoint`) and
  starts the next phase in a fresh agent session.

Rules from the checklist: CP3 (native preview) is never auto-approved and
needs the user's own words; a CP2 redo names the slides; rounds beyond the
free allowance need a confirmed charge.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from autoppt_service.jobfiles import (
    job_relative,
    read_json,
    service_path,
    sha256_file,
    utc_now,
    write_json_atomic,
)

CHECKPOINT_SCHEMA = "autoppt-checkpoint-v1"
RESPONSE_SCHEMA = "autoppt-checkpoint-response-v1"
CHECKPOINT_TYPES = ("outline", "masters", "vectorize_text", "native_preview")
PREVIEW_KINDS = ("slide", "thumbnail", "contact_sheet", "side_by_side", "diff_heatmap", "document")
DECISIONS = {
    "outline": ("approve", "revise"),
    "masters": ("approve", "redo_slides"),
    "vectorize_text": ("approve", "revise"),
    "native_preview": ("approve", "revise"),
}
DEFAULT_FREE_ROUNDS = 2

_SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
CHECKPOINT_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema_version", "checkpoint_id", "type", "round", "previews", "questions"],
    "properties": {
        "schema_version": {"const": CHECKPOINT_SCHEMA},
        "checkpoint_id": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"},
        "type": {"enum": list(CHECKPOINT_TYPES)},
        "round": {"type": "integer", "minimum": 1},
        "summary": {"type": "string", "maxLength": 4000},
        "previews": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "kind"],
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "kind": {"enum": list(PREVIEW_KINDS)},
                    "slide_id": {"type": "string"},
                    "sha256": _SHA,
                    "caption": {"type": "string", "maxLength": 300},
                },
            },
        },
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["question_id", "text", "kind"],
                "properties": {
                    "question_id": {"type": "string"},
                    "text": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "kind": {"enum": ["approve", "choice", "text", "slides"]},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "slide_manifest_sha256": _SHA,
        "master_sha256": {"type": "object", "additionalProperties": _SHA},
        "editability_scope": {"type": "object"},
        "created_at": {"type": "string"},
    },
}
RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "checkpoint_id", "decision", "responder", "user_reply"],
    "properties": {
        "schema_version": {"const": RESPONSE_SCHEMA},
        "checkpoint_id": {"type": "string"},
        "decision": {"enum": sorted({d for values in DECISIONS.values() for d in values})},
        "responder": {"enum": ["user", "admin"]},
        "user_reply": {"type": "string", "maxLength": 10000},
        "annotations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text"],
                "properties": {"slide_id": {"type": "string"}, "text": {"type": "string", "minLength": 1}},
            },
        },
        "redo_slide_ids": {"type": "array", "items": {"type": "string"}},
        "answers": {"type": "object"},
        "charge_confirmed": {"type": "boolean"},
        "reviewed_previews": {"type": "array", "items": {"type": "string"}},
        "answered_at": {"type": "string"},
    },
}
_CHECKPOINT_VALIDATOR = Draft202012Validator(CHECKPOINT_JSON_SCHEMA)
_RESPONSE_VALIDATOR = Draft202012Validator(RESPONSE_JSON_SCHEMA)


class CheckpointError(ValueError):
    def __init__(self, issues: list[str]) -> None:
        self.issues = list(issues)
        super().__init__("; ".join(self.issues))


def _schema_issues(validator: Draft202012Validator, payload: Any) -> list[str]:
    return [
        f"{'.'.join(map(str, error.absolute_path)) or '$'}: {error.message}"
        for error in validator.iter_errors(payload)
    ]


def validate_checkpoint(job_dir: Path, checkpoint: Any, *, fill_hashes: bool = False) -> list[str]:
    """Check shape, that previews exist inside the job, hashes match, and type-specific needs."""
    issues = _schema_issues(_CHECKPOINT_VALIDATOR, checkpoint)
    if issues:
        return issues
    kinds = set()
    slide_ids = set()
    for index, preview in enumerate(checkpoint["previews"]):
        try:
            path = job_relative(job_dir, preview["path"])
        except ValueError as exc:
            issues.append(f"previews[{index}]: {exc}")
            continue
        if not path.is_file():
            issues.append(f"previews[{index}]: missing file {preview['path']}")
            continue
        actual = sha256_file(path)
        if "sha256" in preview and preview["sha256"] != actual:
            issues.append(f"previews[{index}]: sha256 mismatch for {preview['path']}")
        elif fill_hashes:
            preview["sha256"] = actual
        kinds.add(preview["kind"])
        if preview.get("slide_id"):
            slide_ids.add(preview["slide_id"])
    kind = checkpoint["type"]
    if not checkpoint["previews"]:
        issues.append("a checkpoint needs at least one preview")
    if kind == "outline" and "slide_manifest_sha256" not in checkpoint:
        issues.append("outline checkpoint needs slide_manifest_sha256")
    if kind in ("masters", "native_preview") and not checkpoint.get("master_sha256"):
        issues.append(f"{kind} checkpoint needs master_sha256 per slide")
    if kind == "native_preview":
        if "side_by_side" not in kinds:
            issues.append("native_preview checkpoint needs side_by_side previews (master vs native)")
        if "editability_scope" not in checkpoint:
            issues.append("native_preview checkpoint must state editability_scope (native vs image parts)")
        missing = sorted(set(checkpoint.get("master_sha256", {})) - slide_ids)
        if missing:
            issues.append(f"native_preview checkpoint has no preview for slides: {', '.join(missing)}")
    return issues


def open_checkpoint(job_dir: Path, checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Validate and write ``service/checkpoint.json`` (used by agents' helper scripts and the mock)."""
    if pending_checkpoint(job_dir) is not None:
        raise CheckpointError(["another checkpoint is still open"])
    payload = {"schema_version": CHECKPOINT_SCHEMA, "created_at": utc_now(), **checkpoint}
    issues = validate_checkpoint(job_dir, payload, fill_hashes=True)
    if issues:
        raise CheckpointError(issues)
    write_json_atomic(service_path(job_dir, "checkpoint.json"), payload)
    return payload


def pending_checkpoint(job_dir: Path) -> dict[str, Any] | None:
    """The open checkpoint that has no response yet, or ``None``."""
    checkpoint = read_json(service_path(job_dir, "checkpoint.json"))
    if checkpoint is None or service_path(job_dir, "checkpoint-response.json").exists():
        return None
    return checkpoint


def validate_response(
    checkpoint: dict[str, Any],
    response: Any,
    *,
    free_rounds: int = DEFAULT_FREE_ROUNDS,
) -> list[str]:
    issues = _schema_issues(_RESPONSE_VALIDATOR, response)
    if issues:
        return issues
    kind = checkpoint["type"]
    decision = response["decision"]
    if response["checkpoint_id"] != checkpoint["checkpoint_id"]:
        issues.append("checkpoint_id does not match the open checkpoint")
    if decision not in DECISIONS[kind]:
        issues.append(f"decision {decision!r} is not allowed for a {kind} checkpoint")
    if kind == "native_preview":
        # Mandatory human approval: never automatic, and the reply is kept verbatim.
        if response["responder"] != "user":
            issues.append("native_preview must be answered by the user, not an admin or automation")
        if not response["user_reply"].strip():
            issues.append("native_preview needs the user's own reply text")
        if decision == "approve" and not response.get("reviewed_previews"):
            issues.append("native_preview approval must list the previews the user reviewed")
    if decision == "redo_slides":
        redo = response.get("redo_slide_ids") or []
        known = set(checkpoint.get("master_sha256", {}))
        if not redo:
            issues.append("redo_slides needs redo_slide_ids")
        elif known and set(redo) - known:
            issues.append(f"unknown slides in redo_slide_ids: {', '.join(sorted(set(redo) - known))}")
    if decision == "revise" and not (response.get("annotations") or response["user_reply"].strip()):
        issues.append("revise needs annotations or a reply describing the change")
    if decision != "approve" and checkpoint["round"] >= free_rounds and not response.get("charge_confirmed"):
        issues.append(f"round {checkpoint['round'] + 1} exceeds the free rounds; charge_confirmed is required")
    preview_paths = {preview["path"] for preview in checkpoint["previews"]}
    unknown = sorted(set(response.get("reviewed_previews") or []) - preview_paths)
    if unknown:
        issues.append(f"reviewed_previews are not part of this checkpoint: {', '.join(unknown)}")
    return issues


def submit_response(job_dir: Path, response: dict[str, Any], *, free_rounds: int = DEFAULT_FREE_ROUNDS) -> dict[str, Any]:
    checkpoint = pending_checkpoint(job_dir)
    if checkpoint is None:
        raise CheckpointError(["no open checkpoint"])
    payload = {"schema_version": RESPONSE_SCHEMA, **response}
    payload.setdefault("answered_at", utc_now())
    issues = validate_response(checkpoint, payload, free_rounds=free_rounds)
    if issues:
        raise CheckpointError(issues)
    write_json_atomic(service_path(job_dir, "checkpoint-response.json"), payload)
    return payload


def close_checkpoint(job_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Archive the answered checkpoint; return ``(checkpoint, response)``."""
    checkpoint_path = service_path(job_dir, "checkpoint.json")
    response_path = service_path(job_dir, "checkpoint-response.json")
    checkpoint, response = read_json(checkpoint_path), read_json(response_path)
    if checkpoint is None or response is None:
        raise CheckpointError(["checkpoint has no response yet"])
    archive = service_path(job_dir, "checkpoints") / checkpoint["checkpoint_id"]
    if archive.exists():
        raise CheckpointError([f"checkpoint {checkpoint['checkpoint_id']} was already closed"])
    archive.mkdir(parents=True)
    shutil.move(str(checkpoint_path), archive / "checkpoint.json")
    shutil.move(str(response_path), archive / "checkpoint-response.json")
    return checkpoint, response


def closed_checkpoints(job_dir: Path) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    root = service_path(job_dir, "checkpoints")
    if not root.is_dir():
        return []
    pairs = []
    for folder in sorted(path for path in root.iterdir() if path.is_dir()):
        pairs.append((read_json(folder / "checkpoint.json"), read_json(folder / "checkpoint-response.json")))
    return sorted(pairs, key=lambda pair: pair[0].get("created_at", ""))
