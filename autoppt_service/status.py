"""Job state machine (Worker-owned) and the agent's progress file.

Two files, two writers, so they never race:

* ``service/job-state.json`` — owned by the Worker.  The state machine from
  development-checklist section 5; every change is checked against the job's
  phase plan (which depends on service, tier and enabled checkpoints).
* ``service/status.json`` — written by the agent inside a phase: progress,
  current slide, warnings, or ``blocked`` with a reason.  The Worker polls it
  with :class:`StatusWatcher` and forwards it to the backend; an invalid file
  is reported, never trusted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from autoppt_service.jobfiles import read_json, service_path, utc_now, write_json_atomic

JOB_STATE_SCHEMA = "autoppt-job-state-v1"
AGENT_STATUS_SCHEMA = "autoppt-agent-status-v1"

BACKEND_STATES = ("draft", "estimated", "paid", "queued")
CHECKPOINT_STATES = {
    "cp1_outline": "outline",
    "cp2_masters": "masters",
    "cp_v_text": "vectorize_text",
    "cp3_native_preview": "native_preview",
}
WORKER_STATES = (
    "intake",
    "planning",
    "cp1_outline",
    "masters",
    "cp2_masters",
    "image_deck_ready",
    "cp_v_text",
    "reconstructing",
    "office_qa",
    "cp3_native_preview",
    "packaging",
    "delivered",
)
TERMINAL_STATES = ("delivered", "failed", "cancelled")
ALL_STATES = (*BACKEND_STATES, *WORKER_STATES, "blocked", "failed", "cancelled")

# Going back for another round after a checkpoint answer or a failed QA.
REWORK_EDGES = {
    ("cp1_outline", "planning"),
    ("cp2_masters", "masters"),
    ("cp3_native_preview", "reconstructing"),
    ("office_qa", "reconstructing"),
}

# Agent phases (the ``--phase`` values of run_job) and the states they run in.
PHASE_STATES = {
    "intake": ("intake",),
    "planning": ("planning",),
    "masters": ("masters",),
    "reconstruct": ("reconstructing", "office_qa"),
    "package": ("packaging",),
}


class TransitionError(ValueError):
    pass


def phase_plan(request: dict[str, Any]) -> list[str]:
    """Ordered states for a validated (normalized) job request."""
    review = request.get("review", {})
    tier = request["delivery"]["tier"]
    plan = ["draft", "estimated", "paid", "queued", "intake"]
    if request["service"] == "vectorize":
        return [*plan, "cp_v_text", "reconstructing", "office_qa", "cp3_native_preview", "packaging", "delivered"]
    plan.append("planning")
    if review.get("outline_checkpoint", True):
        plan.append("cp1_outline")
    if tier != "planning_package":
        plan.append("masters")
        if review.get("master_checkpoint", True):
            plan.append("cp2_masters")
        plan.append("image_deck_ready")
    if tier == "native_editable":
        # CP3 can never be switched off for the native tier.
        plan += ["reconstructing", "office_qa", "cp3_native_preview"]
    return [*plan, "packaging", "delivered"]


def can_transition(state: dict[str, Any], target: str) -> bool:
    current = state["state"]
    plan = state["plan"]
    if target not in ALL_STATES or current in TERMINAL_STATES:
        return False
    if target in ("failed", "cancelled"):
        return True
    if current == "blocked":
        return target == state.get("blocked_from")
    if target == "blocked":
        return True
    if (current, target) in REWORK_EDGES:
        return target in plan
    if current in plan and target in plan:
        return plan.index(target) == plan.index(current) + 1
    return False


def new_job_state(job_id: str, request: dict[str, Any], *, state: str = "queued") -> dict[str, Any]:
    plan = phase_plan(request)
    if state not in plan:
        raise TransitionError(f"{state} is not in this job's plan")
    now = utc_now()
    return {
        "schema_version": JOB_STATE_SCHEMA,
        "job_id": job_id,
        "state": state,
        "plan": plan,
        "blocked_from": None,
        "blocked_reason": "",
        "history": [{"from": None, "to": state, "at": now, "reason": "created"}],
        "updated_at": now,
    }


def transition(state: dict[str, Any], target: str, *, reason: str = "") -> dict[str, Any]:
    if not can_transition(state, target):
        raise TransitionError(f"illegal transition {state['state']} -> {target}")
    updated = dict(state)
    now = utc_now()
    if target == "blocked":
        updated["blocked_from"] = state["state"]
        updated["blocked_reason"] = reason
    elif state["state"] == "blocked":
        updated["blocked_from"] = None
        updated["blocked_reason"] = ""
    updated["history"] = [*state["history"], {"from": state["state"], "to": target, "at": now, "reason": reason}]
    updated["state"] = target
    updated["updated_at"] = now
    return updated


def next_state(state: dict[str, Any]) -> str | None:
    plan, current = state["plan"], state["state"]
    if current not in plan or current in TERMINAL_STATES:
        return None
    index = plan.index(current)
    return plan[index + 1] if index + 1 < len(plan) else None


def load_job_state(job_dir: Path) -> dict[str, Any] | None:
    return read_json(service_path(job_dir, "job-state.json"))


def save_job_state(job_dir: Path, state: dict[str, Any]) -> None:
    write_json_atomic(service_path(job_dir, "job-state.json"), state)


def advance(job_dir: Path, target: str, *, reason: str = "") -> dict[str, Any]:
    state = load_job_state(job_dir)
    if state is None:
        raise TransitionError(f"no job-state.json in {job_dir}")
    state = transition(state, target, reason=reason)
    save_job_state(job_dir, state)
    return state


# ---------------------------------------------------------------- agent status

AGENT_STATUS_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema_version", "phase", "state", "updated_at"],
    "properties": {
        "schema_version": {"const": AGENT_STATUS_SCHEMA},
        "phase": {"enum": list(PHASE_STATES)},
        "state": {"enum": ["running", "waiting_checkpoint", "blocked", "done", "failed"]},
        "progress": {"type": "number", "minimum": 0, "maximum": 1},
        "current_slide": {"type": ["string", "null"], "maxLength": 32},
        "slides_done": {"type": "integer", "minimum": 0},
        "slides_total": {"type": "integer", "minimum": 0},
        "message": {"type": "string", "maxLength": 500},
        "warnings": {"type": "array", "maxItems": 50, "items": {"type": "string", "maxLength": 500}},
        "blocked_reason": {"type": "string", "maxLength": 1000},
        "updated_at": {"type": "string"},
    },
    "allOf": [
        {"if": {"properties": {"state": {"const": "blocked"}}}, "then": {"required": ["blocked_reason"]}},
    ],
}
_STATUS_VALIDATOR = Draft202012Validator(AGENT_STATUS_JSON_SCHEMA)


def validate_agent_status(payload: Any) -> list[str]:
    return [
        f"{'.'.join(map(str, error.absolute_path)) or '$'}: {error.message}"
        for error in _STATUS_VALIDATOR.iter_errors(payload)
    ]


def write_agent_status(job_dir: Path, phase: str, state: str = "running", **fields: Any) -> dict[str, Any]:
    payload = {"schema_version": AGENT_STATUS_SCHEMA, "phase": phase, "state": state, **fields, "updated_at": utc_now()}
    errors = validate_agent_status(payload)
    if errors:
        raise ValueError("; ".join(errors))
    write_json_atomic(service_path(job_dir, "status.json"), payload)
    return payload


class StatusWatcher:
    """Report ``service/status.json`` whenever it changes (call :meth:`poll` periodically)."""

    def __init__(self, job_dir: Path) -> None:
        self.path = service_path(job_dir, "status.json")
        self._last: tuple[int, int] | None = None

    def poll(self) -> dict[str, Any] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        if key == self._last:
            return None
        self._last = key
        try:
            payload = read_json(self.path)
        except ValueError as exc:  # half-written or not JSON
            return {"valid": False, "errors": [f"status.json is not JSON: {exc}"], "status": None}
        errors = validate_agent_status(payload)
        return {"valid": not errors, "errors": errors, "status": payload}
