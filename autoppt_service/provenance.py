"""Ingest and verify Codex ``exec-`` ImageGen host records for a job workspace.

Current Codex hosts (desktop app and ``codex exec``) record each built-in
``image_gen`` call in the rollout JSONL as an ``item_completed`` Extension of
kind ``image_gen.generation`` with an ``exec-`` id, the prompt the agent passed
(``revisedPrompt``), the base64 PNG (``result``) and the saved copy
(``savedPath``).  The workflow repository's handoff only ingests legacy
``ig_`` records, so the service validates host records here, without changing
that repository.

Through an API-key provider (measured 2026-10-19 with a Responses-compatible proxy) the same built-in
tool is recorded as a ``response_item`` of type ``image_generation_call`` with an ``ig_`` id, the
prompt (``revised_prompt``) and the base64 PNG, and no saved copy.  Both forms are accepted; the
sidecar records which form and which ``model_provider`` produced the image, so a master made through
a proxy is never presented as a direct-account image.

A host record is accepted only when its thread matches the rollout file, the
saved copy (if present) is named after the thread and id and has the same
bytes, and the decoded payload hash becomes the slide image hash.  The sidecar
keeps the workflow's provenance fields, so the repository's ImageGen-first gate
accepts it in normal mode (``exec-`` hosts are not ``ig_`` strong ids).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOST_IMAGEGEN_KIND = "image_gen.generation"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PROVENANCE_SCHEMA_VERSION = "builtin-imagegen-provenance-v1"
DIRECT_FINAL_GENERATION_MODE = "direct_final_slide_imagegen"
VERIFY_SCHEMA_VERSION = "service-masters-verification-v1"


class HostRecordBlocked(RuntimeError):
    def __init__(self, issues: list[str]) -> None:
        self.issues = list(issues)
        super().__init__("; ".join(self.issues))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


RESPONSE_IMAGEGEN_TYPE = "image_generation_call"


def host_records(rollout: Path) -> list[dict[str, Any]]:
    """Return every built-in image_gen record (host Extension or Responses call) in rollout order."""
    records: list[dict[str, Any]] = []
    session: dict[str, Any] = {}
    with rollout.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if event.get("type") == "session_meta":
                session = payload
                continue
            if event.get("type") == "response_item" and payload.get("type") == RESPONSE_IMAGEGEN_TYPE:
                passthrough = payload.get("internal_chat_message_metadata_passthrough") or {}
                records.append(
                    {
                        "timestamp": event.get("timestamp"),
                        "line_number": line_number,
                        "id": str(payload.get("id") or ""),
                        "status": str(payload.get("status") or ""),
                        "revised_prompt": str(payload.get("revised_prompt") or ""),
                        "result": str(payload.get("result") or ""),
                        "thread_id": str(session.get("id") or ""),
                        "turn_id": str(passthrough.get("turn_id") or ""),
                        "saved_path": "",
                        "record_kind": "responses-image-generation-call",
                        "model_provider": str(session.get("model_provider") or ""),
                    }
                )
                continue
            if payload.get("type") != "item_completed":
                continue
            item = payload.get("item")
            if not (isinstance(item, dict) and item.get("type") == "Extension" and item.get("kind") == HOST_IMAGEGEN_KIND):
                continue
            records.append(
                {
                    "timestamp": event.get("timestamp"),
                    "line_number": line_number,
                    "id": str(item.get("id") or ""),
                    "status": str(item.get("status") or ""),
                    "revised_prompt": str(item.get("revisedPrompt") or ""),
                    "result": str(item.get("result") or ""),
                    "thread_id": str(payload.get("thread_id") or ""),
                    "turn_id": str(payload.get("turn_id") or ""),
                    "saved_path": str(item.get("savedPath") or ""),
                    "record_kind": "codex-host-extension",
                    "model_provider": str(session.get("model_provider") or ""),
                }
            )
    return records


ACCEPTED_ID_PREFIXES = ("exec-", "ig_")


def completed_host_records(rollout: Path) -> list[tuple[int, dict[str, Any]]]:
    return [
        (index, record)
        for index, record in enumerate(host_records(rollout))
        if record["status"].lower() == "completed" and record["id"].startswith(ACCEPTED_ID_PREFIXES) and record["result"]
    ]


ACCEPTED_PROMPT_MATCHES = ("exact", "normalized")
_MARKERS = ("<<<SLIDE_PROMPT", "SLIDE_PROMPT>>>")


def prompt_match(expected: str, actual: str) -> str:
    """``exact`` / ``normalized`` are accepted; anything else is a mismatch with a named cause.

    ``marker_included`` (measured 2026-10-17 with 5,000-character prompts): the agent passed the
    prompt verbatim plus a marker line.  Still rejected, because the image model saw the marker
    text and might render it, but the retry can tell the agent exactly what went wrong.
    """
    if actual == expected:
        return "exact"
    if " ".join(actual.split()) == " ".join(expected.split()):
        return "normalized"
    stripped = actual
    for marker in _MARKERS:
        stripped = stripped.replace(marker, "")
    if stripped != actual and " ".join(stripped.split()) == " ".join(expected.split()):
        return "marker_included"
    return "different"


def _host_evidence(record: dict[str, Any], image_bytes: bytes, rollout: Path) -> tuple[dict[str, Any], list[str]]:
    imagegen_id = record["id"]
    thread_id = record["thread_id"]
    digest = _sha256_bytes(image_bytes)
    saved_path = Path(record["saved_path"]) if record["saved_path"] else None
    saved_present = bool(saved_path and saved_path.is_file())
    saved_match = bool(saved_present and _sha256_file(saved_path) == digest)
    issues: list[str] = []
    if not thread_id or thread_id not in rollout.name:
        issues.append(f"host_record_thread_mismatch:{imagegen_id}")
    if saved_path is not None:
        if saved_path.stem != imagegen_id or saved_path.parent.name != thread_id:
            issues.append(f"host_saved_path_identity_mismatch:{imagegen_id}")
        if saved_present and not saved_match:
            issues.append(f"host_saved_image_sha256_mismatch:{imagegen_id}")
    evidence = {
        "kind": HOST_IMAGEGEN_KIND if record.get("record_kind") == "codex-host-extension" else RESPONSE_IMAGEGEN_TYPE,
        "model_provider": record.get("model_provider", ""),
        "thread_id": thread_id,
        "turn_id": record["turn_id"],
        "saved_path": record["saved_path"],
        "saved_path_present": saved_present,
        "saved_path_sha256_match": saved_match,
        "result_sha256": digest,
    }
    return evidence, issues


def _workspace(handoff_path: Path, handoff: dict[str, Any]) -> Path:
    value = handoff.get("workspace_dir")
    if value:
        path = Path(str(value))
        return path if path.is_absolute() else (handoff_path.parent / path).resolve()
    return handoff_path.parent.resolve()


def ingest_host_record(
    handoff_path: Path,
    slide_id: str,
    *,
    rollout: Path,
    record_index: int,
    require_prompt_match: bool = True,
) -> dict[str, Any]:
    """Write the slide PNG and its provenance sidecar from one host record.

    ``handoff_path`` is the workflow's ``builtin-imagegen-handoff.json``
    produced by its unchanged ``prepare_handoff``.
    """
    handoff_path = handoff_path.resolve()
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    slides = handoff.get("slides") if isinstance(handoff.get("slides"), list) else []
    target = next((item for item in slides if str(item.get("slide_id")) == slide_id), None)
    if target is None:
        raise HostRecordBlocked([f"unknown_slide_id:{slide_id}"])
    usable = dict(completed_host_records(rollout))
    record = usable.get(record_index)
    if record is None:
        raise HostRecordBlocked([f"record_is_not_completed_host_imagegen:{record_index}"])
    try:
        image_bytes = base64.b64decode(record["result"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HostRecordBlocked([f"host_result_decode_failed:{exc.__class__.__name__}"]) from exc
    if not image_bytes.startswith(PNG_SIGNATURE):
        raise HostRecordBlocked([f"host_result_not_png:{slide_id}"])
    evidence, issues = _host_evidence(record, image_bytes, rollout)
    match = prompt_match(str(target.get("prompt") or ""), record["revised_prompt"])
    if require_prompt_match and match not in ACCEPTED_PROMPT_MATCHES:
        issues.append(f"host_record_prompt_mismatch:{slide_id}:{match}")
    if issues:
        raise HostRecordBlocked(issues)

    workspace = _workspace(handoff_path, handoff)
    output = Path(str(target.get("final_path") or f"assets/slides/{slide_id}.png"))
    output = output if output.is_absolute() else (workspace / output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(image_bytes)
    digest = _sha256_file(output)
    generated_at = _utc_now()
    sidecar_path = workspace / "assets" / "generated" / f"{slide_id}-pipeline.json"
    sidecar = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "slide_id": slide_id,
        "status": "generated",
        "backend": "builtin",
        "provenance_kind": "builtin-imagegen",
        "generation_mode": DIRECT_FINAL_GENERATION_MODE,
        "id": record["id"],
        "imagegen_id": record["id"],
        "record_kind": record.get("record_kind", "codex-host-extension"),
        "model_provider": record.get("model_provider", ""),
        "provenance_strength": "validated-host-record",
        "model": "unconfirmed",
        "session_jsonl": str(rollout.resolve()),
        "record_index": record_index,
        "record_line_number": record["line_number"],
        "timestamp": record["timestamp"],
        "revised_prompt": record["revised_prompt"][:4000],
        "prompt_match": match,
        "prompt_sha256": target.get("prompt_sha256", ""),
        "final_path": str(output.relative_to(workspace)) if output.is_relative_to(workspace) else str(output),
        "output_path": str(output),
        "sha256": digest,
        "bytes": len(image_bytes),
        "generated_at": generated_at,
        "host_record": evidence,
    }
    _write_json(sidecar_path, sidecar)
    target.update(
        {
            "status": "generated",
            "record_index": record_index,
            "imagegen_id": record["id"],
            "provenance_strength": "validated-host-record",
            "prompt_match": match,
            "sha256": digest,
            "bytes": len(image_bytes),
            "generated_at": generated_at,
            "session_jsonl": str(rollout.resolve()),
            "provenance_sidecar": str(sidecar_path.relative_to(workspace)),
        }
    )
    handoff["status"] = "completed" if all(item.get("status") == "generated" for item in slides) else "in_progress"
    _write_json(handoff_path, handoff)
    return {"status": "generated", "slide_id": slide_id, "output": str(output), "imagegen_id": record["id"], "prompt_match": match}


def verify_masters(handoff_path: Path) -> dict[str, Any]:
    """Check every slide image against its sidecar and host payload hash."""
    handoff_path = handoff_path.resolve()
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    workspace = _workspace(handoff_path, handoff)
    issues: list[str] = []
    verified = 0
    slides = handoff.get("slides") if isinstance(handoff.get("slides"), list) else []
    for item in slides:
        slide_id = str(item.get("slide_id") or "")
        if item.get("status") != "generated":
            issues.append(f"slide_not_generated:{slide_id}")
            continue
        image = Path(str(item.get("final_path") or f"assets/slides/{slide_id}.png"))
        image = image if image.is_absolute() else workspace / image
        sidecar_path = workspace / "assets" / "generated" / f"{slide_id}-pipeline.json"
        if not image.is_file() or not sidecar_path.is_file():
            issues.append(f"slide_output_or_sidecar_missing:{slide_id}")
            continue
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        digest = _sha256_file(image)
        host = sidecar.get("host_record") if isinstance(sidecar.get("host_record"), dict) else {}
        if not (
            str(sidecar.get("id") or "").startswith(ACCEPTED_ID_PREFIXES)
            and sidecar.get("provenance_strength") == "validated-host-record"
            and sidecar.get("sha256") == digest
            and host.get("result_sha256") == digest
        ):
            issues.append(f"slide_provenance_unverified:{slide_id}")
            continue
        verified += 1
    report = {
        "schema_version": VERIFY_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "handoff": str(handoff_path),
        "verdict": "pass" if not issues and slides else "blocked",
        "slides_expected": len(slides),
        "slides_verified": verified,
        "issues": issues if slides else ["handoff_has_no_slides"],
    }
    _write_json(workspace / "references" / "service-masters-verification.json", report)
    return report
