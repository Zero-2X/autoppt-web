"""Turn an approved CP3 (native preview) answer into an acceptance record.

The record has the same fields as the workflow repository's
``docs/vectorization-history/zhicang-native-acceptance.json``: the user's
reply verbatim, the preview they accepted, the master hash, the scope and the
``native-priority-approved`` tier.  It authorises only that tier for this job;
editing, content, layout, residue and all-slide comparison still apply, and
strict pixel results stay disclosed.  Extra fields (job/checkpoint ids, every
reviewed preview, per-slide master hashes) are added for the service.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from autoppt_service.checkpoint import closed_checkpoints
from autoppt_service.jobfiles import job_relative, read_json, service_path, sha256_file, write_json_atomic

TIER = "native-priority-approved"
FONT_NAMES = {
    "office_native_yahei": "Microsoft YaHei",
    "office_native_dengxian": "DengXian",
}


class AcceptanceError(ValueError):
    pass


def master_set_sha256(master_sha256: dict[str, str]) -> str:
    """One hash for the reviewed master set: sha256 over sorted ``slide_id:sha256`` lines."""
    lines = "".join(f"{slide_id}:{digest}\n" for slide_id, digest in sorted(master_sha256.items()))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def build_acceptance_record(
    job_dir: Path,
    checkpoint: dict[str, Any],
    response: dict[str, Any],
    request: dict[str, Any],
    *,
    job_id: str,
) -> dict[str, Any]:
    if checkpoint.get("type") != "native_preview":
        raise AcceptanceError("acceptance records come only from a native_preview (CP3) checkpoint")
    if response.get("decision") != "approve":
        raise AcceptanceError("CP3 was not approved")
    if response.get("responder") != "user" or not str(response.get("user_reply", "")).strip():
        raise AcceptanceError("CP3 approval must carry the user's own reply")
    if response.get("checkpoint_id") != checkpoint.get("checkpoint_id"):
        raise AcceptanceError("response does not belong to this checkpoint")
    reviewed = list(response.get("reviewed_previews") or [])
    if not reviewed:
        raise AcceptanceError("CP3 approval must list the reviewed previews")
    previews = {preview["path"]: preview for preview in checkpoint["previews"]}
    for path in reviewed:
        if path not in previews:
            raise AcceptanceError(f"reviewed preview is not part of the checkpoint: {path}")
        expected = previews[path].get("sha256")
        if expected and sha256_file(job_relative(job_dir, path)) != expected:
            raise AcceptanceError(f"reviewed preview changed after the checkpoint: {path}")
    primary = next((p for p in reviewed if previews[p]["kind"] == "side_by_side"), reviewed[0])

    masters = dict(checkpoint.get("master_sha256") or {})
    delivery = request.get("delivery", {})
    font_policy = delivery.get("font_policy", "")
    if font_policy not in FONT_NAMES:
        raise AcceptanceError("native tier requires an accepted Office font policy")
    slide_count = len(masters)
    title = (request.get("cover") or {}).get("title") or job_id
    scope_parts = dict(checkpoint.get("editability_scope") or {})
    return {
        "schema_version": 1,
        "approved_at": str(response.get("answered_at", ""))[:10],
        "source": f"web checkpoint response {checkpoint['checkpoint_id']} (round {checkpoint.get('round', 1)})",
        "user_reply": response["user_reply"],
        "accepted_reference": primary,
        "policy": {
            "font": FONT_NAMES[font_policy],
            "semantic_native_priority": True,
            "nontext_pixel_threshold_is_release_gate": False,
            "image_comparison_still_required": True,
            "all_slides_office_review_required": True,
            "no_stale_semantic_lettering": True,
            "photos_and_complex_artwork": "bounded movable assets; disclose raster scope",
        },
        "scope": (
            f"This {slide_count}-slide {title} reconstruction only (job {job_id}). "
            "Allows the kinds of non-text differences shown in the reviewed previews. "
            "No approval for missing content, flattened ordinary diagrams, or other jobs."
        ),
        "master_sha256": master_set_sha256(masters),
        "tier": TIER,
        # Service extensions.
        "job_id": job_id,
        "checkpoint_id": checkpoint["checkpoint_id"],
        "responder": response["responder"],
        "reviewed_previews": [
            {"path": path, "kind": previews[path]["kind"], "slide_id": previews[path].get("slide_id"),
             "sha256": previews[path].get("sha256")}
            for path in reviewed
        ],
        "master_sha256_by_slide": masters,
        "editability_scope": scope_parts,
        "annotations": list(response.get("annotations") or []),
    }


def write_acceptance_record(job_dir: Path, *, job_id: str) -> dict[str, Any]:
    """Find the last approved CP3 among closed checkpoints and write ``service/acceptance.json``."""
    request = read_json(service_path(job_dir, "job-request.json"))
    if request is None:
        raise AcceptanceError("service/job-request.json is missing")
    approved = [
        (checkpoint, response)
        for checkpoint, response in closed_checkpoints(job_dir)
        if checkpoint.get("type") == "native_preview" and response.get("decision") == "approve"
    ]
    if not approved:
        raise AcceptanceError("no approved CP3 checkpoint")
    record = build_acceptance_record(job_dir, *approved[-1], request, job_id=job_id)
    write_json_atomic(service_path(job_dir, "acceptance.json"), record)
    return record
