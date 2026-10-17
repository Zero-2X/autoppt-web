"""Generate slide masters unattended: one fresh ``codex exec`` thread per slide.

The Worker cannot call the built-in ``image_gen`` tool itself, so it asks a
headless Codex agent to make exactly one call with the slide prompt verbatim.
The handoff queue comes from the workflow repository's unchanged
``prepare_handoff``; the PNG and provenance come from that thread's rollout
record (validated in ``provenance.py``), never from the agent's own reply.

A separate thread per slide makes the record unambiguous (exactly one usable
call) and lets slides rotate across account profiles.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from autoppt_service.codex_exec import (
    CodexProfile,
    ProfilePool,
    find_rollout,
    load_profiles,
    run_codex_exec,
)
from autoppt_service.provenance import (
    HostRecordBlocked,
    completed_host_records,
    ingest_host_record,
    verify_masters,
)
from autoppt_service.repo_paths import ensure_autoppt_on_path

ensure_autoppt_on_path()
from autoppt_workflow.ppt.builtin_imagegen_handoff import prepare_handoff  # noqa: E402  (read-only use)

REPORT_SCHEMA = "codex-imagegen-step-report-v1"
PROMPT_OPEN = "<<<SLIDE_PROMPT"
PROMPT_CLOSE = "SLIDE_PROMPT>>>"


def build_agent_prompt(slide_prompt: str, *, retry_note: str = "") -> str:
    lines = [
        "You are the image step of an automated presentation pipeline.",
        "Call the built-in image generation tool (image_gen) exactly once.",
        f"The prompt is exactly the lines between the line {PROMPT_OPEN} and the line {PROMPT_CLOSE}.",
        "Those two marker lines are not part of the prompt: never pass them to the tool.",
        "Pass the prompt character for character: do not translate, shorten, summarise, reword, or add to it.",
        "Do not draw the image with code, do not use any API key or external service,",
        "and do not run shell commands. After the tool returns, reply only with: done",
    ]
    if retry_note:
        lines.append(retry_note)
    return "\n".join([*lines, PROMPT_OPEN, slide_prompt, PROMPT_CLOSE, ""])


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _reference_images(workspace: Path, slide: dict[str, Any]) -> list[Path]:
    images = []
    for value in slide.get("reference_images") or []:
        path = Path(str(value))
        images.append(path if path.is_absolute() else workspace / path)
    return images


def _attempt_slide(
    *,
    handoff_path: Path,
    workspace: Path,
    slide: dict[str, Any],
    prompt_slide: dict[str, Any],
    profile: CodexProfile,
    attempt: int,
    scratch_root: Path,
    command: Sequence[str] | None,
    timeout_s: float,
    reasoning_effort: str | None,
    retry_note: str,
) -> dict[str, Any]:
    slide_id = str(slide["slide_id"])
    run_dir = workspace / "service" / "codex-runs" / f"{slide_id}-attempt{attempt}"
    result = run_codex_exec(
        build_agent_prompt(str(slide["prompt"]), retry_note=retry_note),
        cwd=scratch_root / f"{slide_id}-attempt{attempt}",
        profile=profile,
        run_dir=run_dir,
        command=command,
        timeout_s=timeout_s,
        reasoning_effort=reasoning_effort,
        images=_reference_images(workspace, prompt_slide),
    )
    outcome: dict[str, Any] = {"attempt": attempt, **result.to_dict(), "status": "failed", "issues": list(result.errors)}
    if not result.ok:
        outcome["issues"].append("codex_exec_failed")
        return outcome
    rollout = find_rollout(profile, result.thread_id)
    if rollout is None:
        outcome["issues"].append("rollout_not_found")
        return outcome
    outcome["rollout"] = str(rollout)
    usable = completed_host_records(rollout)
    if len(usable) != 1:
        outcome["issues"].append(f"expected_one_host_imagegen_call:{len(usable)}")
        return outcome
    try:
        ingested = ingest_host_record(
            handoff_path,
            slide_id,
            rollout=rollout,
            record_index=usable[0][0],
            require_prompt_match=True,
        )
    except HostRecordBlocked as exc:
        outcome["issues"].extend(exc.issues)
        return outcome
    outcome.update(status="generated", imagegen_id=ingested["imagegen_id"], prompt_match=ingested["prompt_match"])
    return outcome


def generate_masters(
    prompts_path: Path,
    *,
    profiles: Sequence[CodexProfile],
    slide_ids: Sequence[str] | None = None,
    max_attempts: int = 2,
    scratch_root: Path | None = None,
    command: Sequence[str] | None = None,
    timeout_s: float = 900.0,
    reasoning_effort: str | None = "low",
) -> dict[str, Any]:
    """Generate every pending slide, resuming an existing handoff if present."""
    prompts_path = prompts_path.expanduser().resolve()
    workspace = prompts_path.parent
    handoff_path = workspace / "builtin-imagegen-handoff.json"
    if not handoff_path.exists():
        prepare_handoff(prompts_path, output=handoff_path)
    prompt_slides = {
        str(item.get("slide_id")): item
        for item in json.loads(prompts_path.read_text(encoding="utf-8-sig")).get("slides", [])
        if isinstance(item, dict)
    }
    scratch = (scratch_root or workspace / "service" / "codex-scratch").resolve()
    pool = ProfilePool(profiles)
    usage_log = workspace / "service" / "usage.jsonl"
    wanted = set(slide_ids or [])
    report_slides: list[dict[str, Any]] = []

    for slide in json.loads(handoff_path.read_text(encoding="utf-8")).get("slides", []):
        slide_id = str(slide.get("slide_id"))
        if wanted and slide_id not in wanted:
            continue
        if slide.get("status") == "generated":
            report_slides.append({"slide_id": slide_id, "status": "generated", "attempts": [], "resumed": True})
            continue
        attempts: list[dict[str, Any]] = []
        retry_note = ""
        for attempt in range(1, max_attempts + 1):
            profile = pool.acquire()
            if profile is None:
                attempts.append({"attempt": attempt, "status": "failed", "issues": ["no_available_codex_profile"]})
                break
            outcome = _attempt_slide(
                handoff_path=handoff_path,
                workspace=workspace,
                slide=slide,
                prompt_slide=prompt_slides.get(slide_id, {}),
                profile=profile,
                attempt=attempt,
                scratch_root=scratch,
                command=command,
                timeout_s=timeout_s,
                reasoning_effort=reasoning_effort,
                retry_note=retry_note,
            )
            attempts.append(outcome)
            _append_jsonl(
                usage_log,
                {
                    "timestamp": _utc_now(),
                    "phase": "masters",
                    "slide_id": slide_id,
                    "attempt": attempt,
                    "profile": outcome.get("profile", profile.name),
                    "thread_id": outcome.get("thread_id", ""),
                    "usage": outcome.get("usage", {}),
                    "duration_s": outcome.get("duration_s"),
                    "status": outcome["status"],
                },
            )
            if outcome["status"] == "generated":
                break
            if outcome.get("rate_limited"):
                pool.rest(profile)
            mismatches = [issue for issue in outcome["issues"] if issue.startswith("host_record_prompt_mismatch")]
            if any(issue.endswith(":marker_included") for issue in mismatches):
                retry_note = (f"The previous attempt passed a marker line ({PROMPT_CLOSE}) to the tool. Pass only the lines "
                              "strictly between the two marker lines, without either marker.")
            elif mismatches:
                retry_note = "The previous attempt changed the prompt text. Copy it exactly this time."
        status = "generated" if attempts and attempts[-1]["status"] == "generated" else "blocked"
        report_slides.append({"slide_id": slide_id, "status": status, "attempts": attempts})

    verification = verify_masters(handoff_path)
    report = {
        "schema_version": REPORT_SCHEMA,
        "generated_at": _utc_now(),
        "prompts": str(prompts_path),
        "handoff": str(handoff_path),
        "profiles": [profile.name for profile in profiles],
        "slides": report_slides,
        "handoff_verdict": verification.get("verdict"),
        "handoff_issues": verification.get("issues", []),
    }
    report_path = workspace / "references" / "codex-imagegen-step-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


NUMBER_GUARD = ("Do not show any number, ratio, multiple or percentage that is not written in the exact on-slide "
                "text above; do not compute or derive new figures.")


def prepare_redo(workspace: Path, slide_ids: Sequence[str], notes: dict[str, str], *, round_no: int) -> list[str]:
    """Reopen slides rejected at master review with the reviewer's correction added to their prompt.

    The rejected master is archived under ``assets/slides/_rejected/``; the slide's prompt in the
    handoff and in ``image-prompts.json`` gets a revision block (so the verbatim-prompt check
    applies to the revised prompt) and its status goes back to pending.  Returns the reopened ids.
    """
    import hashlib
    import shutil

    workspace = Path(workspace)
    handoff_path = workspace / "builtin-imagegen-handoff.json"
    prompts_path = workspace / "image-prompts.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    prompts = json.loads(prompts_path.read_text(encoding="utf-8-sig"))
    wanted = set(slide_ids)
    reopened = []
    for slide in handoff["slides"]:
        if slide["slide_id"] not in wanted:
            continue
        note = notes.get(slide["slide_id"], "").strip() or "Fix what the reviewer rejected."
        delta = f"\n\nRevision requested at master review round {round_no}: {note}\n{NUMBER_GUARD}"
        current = workspace / slide["final_path"]
        if current.exists():
            archive = workspace / "assets" / "slides" / "_rejected" / f"{slide['slide_id']}-r{round_no}{current.suffix}"
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(current, archive)
        slide.setdefault("revisions", []).append({
            "round": round_no, "note": note, "previous_imagegen_id": slide.get("imagegen_id"),
            "previous_sha256": slide.get("sha256"), "at": _utc_now(),
        })
        slide["prompt"] = f"{slide['prompt']}{delta}"
        slide["prompt_sha256"] = hashlib.sha256(slide["prompt"].encode("utf-8")).hexdigest()
        slide["status"] = "pending"
        for item in prompts.get("slides", []):
            if str(item.get("slide_id")) == slide["slide_id"]:
                item["prompt_zh"] = slide["prompt"]
                item["retry_prompt_delta"] = delta.strip()
        reopened.append(slide["slide_id"])
    handoff_path.write_text(json.dumps(handoff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prompts_path.write_text(json.dumps(prompts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return reopened


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompts", help="image-prompts.json of the run workspace")
    parser.add_argument("--profiles", help='JSON list of {"name", "codex_home"}; default uses the current CODEX_HOME')
    parser.add_argument("--slides", default="", help="Comma-separated slide ids; default all pending")
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=900.0, help="Seconds per codex exec call")
    parser.add_argument("--reasoning-effort", default="low")
    parser.add_argument("--scratch-root", help="Empty working root for the agent; default <workspace>/service/codex-scratch")
    args = parser.parse_args(argv)
    report = generate_masters(
        Path(args.prompts),
        profiles=load_profiles(Path(args.profiles) if args.profiles else None),
        slide_ids=[item.strip() for item in args.slides.split(",") if item.strip()],
        max_attempts=args.max_attempts,
        scratch_root=Path(args.scratch_root) if args.scratch_root else None,
        timeout_s=args.timeout,
        reasoning_effort=args.reasoning_effort or None,
    )
    print(json.dumps({key: report[key] for key in ("handoff_verdict", "handoff_issues", "report_path")}, ensure_ascii=False, indent=2))
    return 0 if report["handoff_verdict"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
