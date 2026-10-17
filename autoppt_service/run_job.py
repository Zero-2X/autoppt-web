"""Run one job phase on this machine, between checkpoints.

    python -m autoppt_service.run_job request.json --job-dir D:\\jobs\\J001 --job-id J001 --phase intake --uploads uploads.json
    python -m autoppt_service.run_job --job-dir D:\\jobs\\J001 --job-id J001 --phase planning --mock
    python -m autoppt_service.run_job --job-dir D:\\jobs\\J001 --job-id J001 --respond answer.json
    python -m autoppt_service.run_job request.json --job-dir ... --job-id ... --uploads ... --phase all --mock --auto-respond

Each phase first applies an answered checkpoint (approve moves on, revise or
redo goes back one phase), then runs only if the job state matches.  A phase
that reaches a checkpoint writes ``service/checkpoint.json`` and stops; the
answer is written with ``--respond`` (or by the backend) before the next run.

Real routes available now: ``intake``; ``planning`` (one Codex session per
attempt following the job's AGENTS.md, checked by :mod:`autoppt_service.planning`,
needs ``--template`` and ``--profiles``); ``masters`` (the Codex ImageGen step,
reading the ``image-prompts.json`` that planning writes); and image-deck
packaging through the workflow's ``build_image_deck.py``.  Native
reconstruction is not enabled yet and stops with :class:`PhaseNotReady`.

``--mock`` runs every phase without Codex or Office.  Mock output is structural
evidence only: every artefact is labelled mock, and the mock acceptance record
never carries a release tier.  ``--auto-respond`` (mock only) answers
checkpoints automatically so CI can run the whole flow.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFont

from autoppt_service.acceptance import write_acceptance_record
from autoppt_service.checkpoint import (
    close_checkpoint,
    closed_checkpoints,
    open_checkpoint,
    pending_checkpoint,
    submit_response,
)
from autoppt_service.intake import run_intake
from autoppt_service.jobfiles import (
    HANDOFF_DIR,
    EVIDENCE_DIR,
    read_json,
    relative_to_job,
    service_path,
    sha256_file,
    utc_now,
    write_json_atomic,
)
from autoppt_service.previews import (
    build_master_previews,
    build_native_previews,
    checkpoint_previews,
    discover_slides,
)
from autoppt_service.repo_paths import ensure_autoppt_on_path
from autoppt_service.status import (
    CHECKPOINT_STATES,
    PHASE_STATES,
    load_job_state,
    next_state,
    save_job_state,
    transition,
    write_agent_status,
)
from autoppt_service.usage import phase_timer, record_agent_turn, summarize_usage

PHASES = ("intake", "planning", "masters", "reconstruct", "package")
MASTERS_DIR = "assets/slides"
NATIVE_RENDER_DIR = "workspace/office-render"
OUTPUT_DIR = "output"
MOCK_NOTICE = "MOCK RUN: structural evidence only, not a real visual result or delivery tier."
REWORK_TARGET = {"cp1_outline": "planning", "cp2_masters": "masters", "cp3_native_preview": "reconstructing"}


class PhaseError(RuntimeError):
    """The job is not in a state where this phase can run."""


class PhaseNotReady(RuntimeError):
    """The real (non-mock) route for this phase is not built yet."""


# ---------------------------------------------------------------- state driver


def apply_answered_checkpoint(job_dir: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Archive an answered checkpoint and move the state on (approve) or back (revise/redo)."""
    if not service_path(job_dir, "checkpoint-response.json").exists():
        return None
    checkpoint, response = close_checkpoint(job_dir)
    state = load_job_state(job_dir)
    if state["state"] not in CHECKPOINT_STATES:
        raise PhaseError(f"answered checkpoint {checkpoint['checkpoint_id']} but job is in {state['state']}")
    if response["decision"] == "approve" or state["state"] == "cp_v_text":
        target = next_state(state)
    else:
        target = REWORK_TARGET[state["state"]]
    save_job_state(job_dir, transition(state, target, reason=f"{checkpoint['checkpoint_id']}: {response['decision']}"))
    return checkpoint, response


def resume_blocked(job_dir: Path, *, reason: str = "resumed by operator") -> dict[str, Any]:
    """Return a blocked job to the state it was blocked from (after the cause was fixed)."""
    state = load_job_state(job_dir)
    if state is None or state["state"] != "blocked":
        raise PhaseError("job is not blocked")
    state = transition(state, state["blocked_from"], reason=reason)
    save_job_state(job_dir, state)
    return state


def _enter(job_dir: Path, phase: str) -> dict[str, Any]:
    state = load_job_state(job_dir)
    if state is None:
        raise PhaseError("job has no state yet; run intake first")
    service_path(job_dir, "deferred.json").unlink(missing_ok=True)
    apply_answered_checkpoint(job_dir)
    state = load_job_state(job_dir)
    if state["state"] in CHECKPOINT_STATES and pending_checkpoint(job_dir) is not None:
        raise PhaseError(f"waiting for the answer to the open {CHECKPOINT_STATES[state['state']]} checkpoint")
    if state["state"] == "image_deck_ready" and phase in ("reconstruct", "package"):
        state = transition(state, next_state(state), reason="image deck ready")
        save_job_state(job_dir, state)
    if state["state"] not in PHASE_STATES[phase]:
        raise PhaseError(f"phase {phase} cannot run while the job is in state {state['state']}")
    return state


def _round(job_dir: Path, kind: str) -> int:
    return 1 + sum(1 for checkpoint, _ in closed_checkpoints(job_dir) if checkpoint["type"] == kind)


def _last_response(job_dir: Path, kind: str) -> dict[str, Any] | None:
    answered = [response for checkpoint, response in closed_checkpoints(job_dir) if checkpoint["type"] == kind]
    return answered[-1] if answered else None


def _checkpoint_or_advance(job_dir: Path, state: dict[str, Any], build: Any, reason: str) -> dict[str, Any]:
    """Open the plan's next checkpoint if it has one, otherwise move to the next state."""
    target = next_state(state)
    if target in CHECKPOINT_STATES:
        open_checkpoint(job_dir, build())
    state = transition(state, target, reason=reason)
    save_job_state(job_dir, state)
    return state


def _mock_turn(job_dir: Path, phase: str, **extra: Any) -> None:
    record_agent_turn(
        job_dir,
        phase,
        {"profile": "mock", "returncode": 0, "thread_id": "mock", "usage": {}, "duration_s": 0.0},
        mock=True,
        **extra,
    )


# ---------------------------------------------------------------- mock artefacts


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _mock_image(path: Path, slide_id: str, label: str, *, aspect: str, tint: tuple[int, int, int]) -> None:
    width, height = (1600, 900) if aspect == "16:9" else (1200, 900)
    image = Image.new("RGB", (width, height), (247, 249, 252))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 110), fill=tint)
    draw.text((60, 30), f"{slide_id}  {label}", fill=(255, 255, 255), font=_font(48))
    draw.rectangle((60, 170, width // 2 - 30, height - 80), outline=tint, width=6)
    draw.rectangle((width // 2 + 30, 170, width - 60, height - 80), fill=(225, 232, 240))
    draw.text((80, height - 70), "MOCK - not a real slide", fill=(180, 35, 45), font=_font(32))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")


def _image_deck(path: Path, images: list[Path], aspect: str) -> None:
    from pptx import Presentation
    from pptx.util import Emu

    presentation = Presentation()
    presentation.slide_width = Emu(12192000)
    presentation.slide_height = Emu(6858000 if aspect == "16:9" else 9144000)
    for image in images:
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_picture(str(image), 0, 0, presentation.slide_width, presentation.slide_height)
    path.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(path)


def _mock_native_deck(path: Path, slides: list[dict[str, Any]], aspect: str) -> None:
    from pptx import Presentation
    from pptx.util import Emu, Pt

    presentation = Presentation()
    presentation.slide_width = Emu(12192000)
    presentation.slide_height = Emu(6858000 if aspect == "16:9" else 9144000)
    for slide in slides:
        page = presentation.slides.add_slide(presentation.slide_layouts[6])
        box = page.shapes.add_textbox(Emu(457200), Emu(228600), Emu(11277600), Emu(914400))
        box.text_frame.text = f"[MOCK] {slide['title']}"
        box.text_frame.paragraphs[0].runs[0].font.size = Pt(28)
    path.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(path)


# ---------------------------------------------------------------- phases


def phase_intake(job_dir: Path, *, job_id: str, request: dict[str, Any], uploads: dict[str, str]) -> dict[str, Any]:
    with phase_timer(job_dir, "intake"):
        report = run_intake(request, uploads, job_dir, job_id=job_id)
    return {"phase": "intake", "status": report["status"], "job_state": report.get("job_state")}


def _resolve_template(template: Path | None) -> Path:
    value = template or (Path(os.environ["AUTOPPT_WORKER_TEMPLATE"]) if os.getenv("AUTOPPT_WORKER_TEMPLATE") else None)
    if value is None:
        raise PhaseNotReady("this phase needs a worker template: pass --template or set AUTOPPT_WORKER_TEMPLATE "
                            "(build one with python -m autoppt_service.worker_template build)")
    return Path(value)


def _real_planning(
    job_dir: Path,
    state: dict[str, Any],
    *,
    job_id: str,
    template: Path | None,
    profiles_path: Path | None,
    command: Sequence[str] | None,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    from autoppt_service.codex_exec import load_profiles
    from autoppt_service.planning import DECK_PLAN, PlanningBlocked, PlanningDeferred, run_planning_agent

    template = _resolve_template(template)
    round_no = _round(job_dir, "outline")
    previous = _last_response(job_dir, "outline")
    revision = previous if previous and previous["decision"] == "revise" else None
    with phase_timer(job_dir, "planning", round=round_no):
        try:
            report = run_planning_agent(
                job_dir,
                job_id=job_id,
                template=template,
                profiles=load_profiles(profiles_path),
                round_no=round_no,
                revision=revision,
                command=command,
                reasoning_effort=reasoning_effort,
            )
        except PlanningDeferred as exc:
            # Not the user's problem: the job stays in planning and is retried after the reset.
            write_json_atomic(
                service_path(job_dir, "deferred.json"),
                {"phase": "planning", "reason": exc.message, "retry_after": exc.retry_after, "at": utc_now()},
            )
            return {"phase": "planning", "job_state": state["state"], "deferred": True,
                    "reason": exc.message, "retry_after": exc.retry_after}
        except PlanningBlocked as exc:
            state = transition(state, "blocked", reason="planning: " + "; ".join(exc.issues)[:980])
            save_job_state(job_dir, state)
            return {"phase": "planning", "job_state": state["state"], "issues": exc.issues}
        outline = job_dir / HANDOFF_DIR / "outline-preview.md"
        summary = f"{report['slides']} slides planned"
        if report["warnings"]:
            summary += "; please check: " + "; ".join(report["warnings"])
        state = _checkpoint_or_advance(
            job_dir,
            state,
            lambda: {
                "checkpoint_id": f"cp1-outline-r{round_no}",
                "type": "outline",
                "round": round_no,
                "summary": summary[:4000],
                "previews": [{"path": relative_to_job(job_dir, outline), "kind": "document"}],
                "questions": [{"question_id": "approve_outline", "text": "大纲是否可以进入母版生成？", "kind": "approve"}],
                "slide_manifest_sha256": sha256_file(job_dir / DECK_PLAN),
            },
            "planning done",
        )
    return {"phase": "planning", "job_state": state["state"], "slides": report["slides"], "warnings": report["warnings"]}


def phase_planning(
    job_dir: Path,
    *,
    mock: bool,
    job_id: str = "",
    template: Path | None = None,
    profiles_path: Path | None = None,
    command: Sequence[str] | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    state = _enter(job_dir, "planning")
    if not mock:
        return _real_planning(
            job_dir, state, job_id=job_id, template=template, profiles_path=profiles_path,
            command=command, reasoning_effort=reasoning_effort,
        )
    brief = read_json(job_dir / HANDOFF_DIR / "planning-brief.json")
    ledger = read_json(job_dir / EVIDENCE_DIR / "evidence-ledger.json") or {}
    with phase_timer(job_dir, "planning", mock=True):
        write_agent_status(job_dir, "planning", progress=0.1, message="mock planning")
        evidence_ids = [item["evidence_id"] for item in ledger.get("evidence_items", []) if item.get("item_type") != "user_statement"]
        roles = (brief.get("scenario") or {}).get("page_roles") or ["content"]
        count = (brief.get("slides") or {}).get("target") or len(roles)
        previous = _last_response(job_dir, "outline")
        notes = {a.get("slide_id"): a["text"] for a in (previous or {}).get("annotations", [])}
        cover = {item["field"]: item["text"] for item in brief.get("exact_text_whitelist", [])}
        slides = []
        for index in range(1, count + 1):
            slide_id = f"S{index:02d}"
            role = roles[min(index - 1, len(roles) - 1)] if index < count else roles[-1]
            slides.append(
                {
                    "slide_id": slide_id,
                    "title": cover.get("title", "Mock deck") if index == 1 else f"[MOCK] {role} {index}",
                    "page_role": role,
                    "core_question": f"[MOCK] 第 {index} 页要回答的问题",
                    "supporting_items": list(brief.get("must_include", []))[:2] if index == 2 else [],
                    "evidence_ids": evidence_ids[(index - 1) % len(evidence_ids):][:1] if evidence_ids else [],
                    "revision_note": notes.get(slide_id, ""),
                }
            )
        slide_brief = {"deck_title": cover.get("title", "Mock deck"), "mock": True, "notice": MOCK_NOTICE, "slides": slides}
        brief_path = job_dir / HANDOFF_DIR / "slide_brief.json"
        write_json_atomic(brief_path, slide_brief)
        outline = job_dir / HANDOFF_DIR / "outline-preview.md"
        outline.write_text(
            "\n".join([f"# {slide_brief['deck_title']} (mock outline)", "", MOCK_NOTICE, ""]
                      + [f"- {s['slide_id']} `{s['page_role']}` {s['title']} — {', '.join(s['evidence_ids']) or 'no evidence'}" for s in slides])
            + "\n",
            encoding="utf-8",
        )
        _mock_turn(job_dir, "planning")
        round_no = _round(job_dir, "outline")
        state = _checkpoint_or_advance(
            job_dir,
            state,
            lambda: {
                "checkpoint_id": f"cp1-outline-r{round_no}",
                "type": "outline",
                "round": round_no,
                "summary": f"{len(slides)} slides planned (mock)",
                "previews": [{"path": relative_to_job(job_dir, outline), "kind": "document"}],
                "questions": [{"question_id": "approve_outline", "text": "大纲是否可以进入母版生成？", "kind": "approve"}],
                "slide_manifest_sha256": sha256_file(brief_path),
            },
            "mock planning done",
        )
        write_agent_status(job_dir, "planning", "waiting_checkpoint" if state["state"] in CHECKPOINT_STATES else "done", progress=1.0)
    return {"phase": "planning", "job_state": state["state"], "slides": len(slides)}


def _masters_checkpoint(job_dir: Path, state: dict[str, Any], masters: dict[str, Path], *, mock: bool) -> dict[str, Any]:
    previews = build_master_previews(job_dir, masters)
    round_no = _round(job_dir, "masters")
    return _checkpoint_or_advance(
        job_dir,
        state,
        lambda: {
            "checkpoint_id": f"cp2-masters-r{round_no}",
            "type": "masters",
            "round": round_no,
            "summary": f"{len(masters)} masters" + (" (mock)" if mock else ""),
            "previews": checkpoint_previews(previews),
            "questions": [{"question_id": "approve_masters", "text": "母版是否通过？需要重做的页请勾选并写原因。", "kind": "slides"}],
            "master_sha256": {slide_id: sha256_file(path) for slide_id, path in masters.items()},
        },
        "masters ready",
    )


def phase_masters(job_dir: Path, *, mock: bool, profiles_path: Path | None = None) -> dict[str, Any]:
    state = _enter(job_dir, "masters")
    request = read_json(service_path(job_dir, "job-request.json"))
    slide_brief = read_json(job_dir / HANDOFF_DIR / "slide_brief.json") or {"slides": []}
    previous = _last_response(job_dir, "masters")
    redo = set(previous.get("redo_slide_ids") or []) if previous and previous["decision"] == "redo_slides" else set()
    with phase_timer(job_dir, "masters", mock=mock):
        if mock:
            for slide in slide_brief["slides"]:
                path = job_dir / MASTERS_DIR / f"{slide['slide_id']}.png"
                if redo and slide["slide_id"] not in redo and path.exists():
                    continue
                write_agent_status(job_dir, "masters", current_slide=slide["slide_id"], message="mock master")
                _mock_image(path, slide["slide_id"], "MOCK master", aspect=request["delivery"]["aspect_ratio"], tint=(23, 63, 95))
                _mock_turn(job_dir, "masters", slide_id=slide["slide_id"])
            write_json_atomic(job_dir / MASTERS_DIR / "mock-provenance.json", {"mock": True, "notice": MOCK_NOTICE})
        else:
            from autoppt_service.codex_exec import load_profiles
            from autoppt_service.codex_imagegen_step import generate_masters

            prompts = job_dir / "image-prompts.json"
            if not prompts.exists():
                raise PhaseNotReady("masters needs image-prompts.json at the job root (written by planning)")
            report = generate_masters(prompts, profiles=load_profiles(profiles_path), slide_ids=sorted(redo) or None)
            if report["handoff_verdict"] != "pass":
                state = transition(state, "blocked", reason="; ".join(report["handoff_issues"])[:1000] or "masters blocked")
                save_job_state(job_dir, state)
                return {"phase": "masters", "job_state": state["state"], "issues": report["handoff_issues"]}
        masters = {sid: path for sid, path in discover_slides(job_dir / MASTERS_DIR).items()}
        state = _masters_checkpoint(job_dir, state, masters, mock=mock)
    return {"phase": "masters", "job_state": state["state"], "masters": len(masters)}


def phase_reconstruct(job_dir: Path, *, mock: bool) -> dict[str, Any]:
    state = _enter(job_dir, "reconstruct")
    if not mock:
        raise PhaseNotReady("native reconstruction is not enabled yet (needs its SERVICE_AGENTS.md section and an Office worker)")
    request = read_json(service_path(job_dir, "job-request.json"))
    slide_brief = read_json(job_dir / HANDOFF_DIR / "slide_brief.json") or {"slides": []}
    masters = discover_slides(job_dir / MASTERS_DIR)
    aspect = request["delivery"]["aspect_ratio"]
    with phase_timer(job_dir, "reconstruct", mock=True):
        if state["state"] == "reconstructing":
            write_agent_status(job_dir, "reconstruct", message="mock reconstruction")
            _mock_native_deck(job_dir / OUTPUT_DIR / "native-deck.pptx", slide_brief["slides"], aspect)
            for slide_id in masters:
                _mock_image(job_dir / NATIVE_RENDER_DIR / f"{slide_id}.png", slide_id, "MOCK native", aspect=aspect, tint=(36, 92, 125))
            _mock_turn(job_dir, "reconstruct")
            state = transition(state, "office_qa", reason="mock reconstruction done")
            save_job_state(job_dir, state)
        natives = discover_slides(job_dir / NATIVE_RENDER_DIR)
        previews = build_native_previews(job_dir, masters, natives)
        round_no = _round(job_dir, "native_preview")
        state = _checkpoint_or_advance(
            job_dir,
            state,
            lambda: {
                "checkpoint_id": f"cp3-native-r{round_no}",
                "type": "native_preview",
                "round": round_no,
                "summary": f"{len(natives)} native slides (mock)",
                "previews": checkpoint_previews(previews),
                "questions": [{"question_id": "accept_native", "text": "是否接受原生版与母版之间的非文字差异？", "kind": "approve"}],
                "master_sha256": {slide_id: sha256_file(path) for slide_id, path in masters.items()},
                "editability_scope": {"native": ["text", "shapes", "charts"], "image": ["photos", "complex artwork"], "mock": True},
            },
            "mock office QA done",
        )
        write_agent_status(job_dir, "reconstruct", "waiting_checkpoint", progress=1.0)
    return {"phase": "reconstruct", "job_state": state["state"]}


def _build_image_deck_with_repo(job_dir: Path, output: Path) -> None:
    spec = job_dir / "deck-spec.json"
    if not spec.exists():
        raise PhaseNotReady("image-deck packaging needs deck-spec.json at the job root (written by planning)")
    script = ensure_autoppt_on_path() / "autopptskills" / "scripts" / "build_image_deck.py"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")  # never write into the read-only repository
    completed = subprocess.run(
        [sys.executable, "-B", str(script), str(spec), "--output", str(output),
         "--report", str(job_dir / OUTPUT_DIR / "image-deck-build-report.json")],
        cwd=str(job_dir), env=env, capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        raise PhaseError(f"build_image_deck.py failed: {completed.stderr.strip()[-1000:]}")


def phase_package(job_dir: Path, *, job_id: str, mock: bool) -> dict[str, Any]:
    state = _enter(job_dir, "package")
    request = read_json(service_path(job_dir, "job-request.json"))
    tier = request["delivery"]["tier"]
    files: list[Path] = [
        job_dir / HANDOFF_DIR / "planning-brief.json",
        job_dir / HANDOFF_DIR / "slide_brief.json",
        job_dir / EVIDENCE_DIR / "evidence-ledger.json",
    ]
    acceptance = None
    with phase_timer(job_dir, "package", mock=mock):
        if tier == "image_deck":
            deck = job_dir / OUTPUT_DIR / "image-deck.pptx"
            if mock:
                _image_deck(deck, list(discover_slides(job_dir / MASTERS_DIR).values()), request["delivery"]["aspect_ratio"])
            else:
                _build_image_deck_with_repo(job_dir, deck)
            files.append(deck)
        elif tier == "native_editable":
            files.append(job_dir / OUTPUT_DIR / "native-deck.pptx")
            acceptance = write_acceptance_record(job_dir, job_id=job_id)
            if mock:
                acceptance.update(tier="mock-no-release-tier", mock=True, notice=MOCK_NOTICE)
                write_json_atomic(service_path(job_dir, "acceptance.json"), acceptance)
            files.append(service_path(job_dir, "acceptance.json"))
        missing = [path for path in files if not path.exists()]
        if missing:
            state = transition(state, "blocked", reason="missing deliverables: " + ", ".join(relative_to_job(job_dir, p) for p in missing))
            save_job_state(job_dir, state)
            return {"phase": "package", "job_state": state["state"]}
        manifest = {
            "schema_version": "autoppt-delivery-manifest-v1",
            "job_id": job_id,
            "generated_at": utc_now(),
            "tier": tier,
            "repo_tier": acceptance["tier"] if acceptance else {"planning_package": "Planning package", "image_deck": "Image-only deck"}[tier],
            "mock": mock,
            "notice": MOCK_NOTICE if mock else (
                "原生可编辑版与母版之间存在经用户确认接受的字体和局部图形差异；不承诺逐像素一致。" if tier == "native_editable" else ""
            ),
            "files": [{"path": relative_to_job(job_dir, path), "sha256": sha256_file(path)} for path in files],
        }
        write_json_atomic(job_dir / OUTPUT_DIR / "delivery-manifest.json", manifest)
        bundle = job_dir / OUTPUT_DIR / f"{job_id}{'-MOCK' if mock else ''}.zip"
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in [*files, job_dir / OUTPUT_DIR / "delivery-manifest.json"]:
                archive.write(path, relative_to_job(job_dir, path))
        state = transition(state, "delivered", reason="packaged")
        save_job_state(job_dir, state)
        write_agent_status(job_dir, "package", "done", progress=1.0)
    # After the timer closes, so the package phase itself is counted.
    write_json_atomic(service_path(job_dir, "usage-summary.json"), summarize_usage(job_dir))
    return {"phase": "package", "job_state": state["state"], "bundle": relative_to_job(job_dir, bundle)}


def run_phase(
    job_dir: Path,
    phase: str,
    *,
    job_id: str,
    mock: bool = False,
    request: dict[str, Any] | None = None,
    uploads: dict[str, str] | None = None,
    profiles_path: Path | None = None,
    template: Path | None = None,
    command: Sequence[str] | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    if phase == "intake":
        if request is None:
            raise PhaseError("intake needs the job request")
        return phase_intake(job_dir, job_id=job_id, request=request, uploads=uploads or {})
    if phase == "planning":
        return phase_planning(
            job_dir, mock=mock, job_id=job_id, template=template, profiles_path=profiles_path,
            command=command, reasoning_effort=reasoning_effort,
        )
    if phase == "masters":
        return phase_masters(job_dir, mock=mock, profiles_path=profiles_path)
    if phase == "reconstruct":
        return phase_reconstruct(job_dir, mock=mock)
    if phase == "package":
        return phase_package(job_dir, job_id=job_id, mock=mock)
    raise PhaseError(f"unknown phase {phase}")


def mock_auto_response(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """CI-only answer that approves; marked as mock so it cannot pass as a real approval."""
    return {
        "checkpoint_id": checkpoint["checkpoint_id"],
        "decision": "approve",
        "responder": "user",
        "user_reply": "[mock] automated test approval, not a user decision",
        "reviewed_previews": [p["path"] for p in checkpoint["previews"] if p["kind"] in ("side_by_side", "slide", "document")][:3],
    }


def run_all(job_dir: Path, *, job_id: str, request: dict[str, Any], uploads: dict[str, str], auto_respond: bool) -> list[dict[str, Any]]:
    """Mock driver: run phases in order until delivered or waiting for an answer."""
    results = [run_phase(job_dir, "intake", job_id=job_id, request=request, uploads=uploads)]
    phase_for_state = {state: phase for phase, states in PHASE_STATES.items() for state in states}
    for _ in range(50):
        state = load_job_state(job_dir)["state"]
        if state in ("delivered", "blocked", "failed", "cancelled"):
            break
        if state in CHECKPOINT_STATES:
            checkpoint = pending_checkpoint(job_dir)
            if checkpoint is not None:
                if not auto_respond:
                    break
                submit_response(job_dir, mock_auto_response(checkpoint))
            if apply_answered_checkpoint(job_dir) is None:
                break  # a checkpoint state with nothing to answer: leave it for inspection
            continue
        if state == "image_deck_ready":
            target = next_state(load_job_state(job_dir))
            phase = phase_for_state[target]
        else:
            phase = phase_for_state[state]
        results.append(run_phase(job_dir, phase, job_id=job_id, mock=True))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a job phase between checkpoints.")
    parser.add_argument("request", nargs="?", type=Path, help="job request JSON (needed for intake)")
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--phase", choices=[*PHASES, "all"])
    parser.add_argument("--uploads", type=Path, help='JSON object {"file_id": "path"} for intake')
    parser.add_argument("--profiles", type=Path, help="Codex profiles JSON for the planning and masters phases")
    parser.add_argument("--template", type=Path, help="worker template directory (default: $AUTOPPT_WORKER_TEMPLATE)")
    parser.add_argument("--reasoning-effort", default=None, help="Codex reasoning effort for agent phases")
    parser.add_argument("--respond", type=Path, help="write this checkpoint answer, then exit")
    parser.add_argument("--resume", action="store_true", help="return a blocked job to the state it was blocked from, then exit")
    parser.add_argument("--mock", action="store_true", help="no Codex/Office; structural evidence only")
    parser.add_argument("--auto-respond", action="store_true", help="with --phase all --mock: answer checkpoints automatically")
    args = parser.parse_args(argv)

    if args.respond:
        payload: Any = submit_response(args.job_dir, read_json(args.respond))
    elif args.resume:
        payload = {"job_state": resume_blocked(args.job_dir)["state"]}
    else:
        if not args.phase:
            parser.error("give --phase or --respond")
        request = json.loads(args.request.read_text(encoding="utf-8-sig")) if args.request else None
        uploads = (read_json(args.uploads) or {}) if args.uploads else {}
        if args.phase == "all":
            if not args.mock:
                parser.error("--phase all is only available with --mock")
            if request is None:
                parser.error("--phase all needs the job request")
            payload = run_all(args.job_dir, job_id=args.job_id, request=request, uploads=uploads, auto_respond=args.auto_respond)
        else:
            if args.auto_respond:
                parser.error("--auto-respond is only for --phase all --mock")
            try:
                payload = run_phase(
                    args.job_dir, args.phase, job_id=args.job_id, mock=args.mock,
                    request=request, uploads=uploads, profiles_path=args.profiles,
                    template=args.template, reasoning_effort=args.reasoning_effort,
                )
            except PhaseNotReady as exc:
                payload, code = {"phase": args.phase, "not_ready": str(exc)}, 3
                sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
                return code
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
