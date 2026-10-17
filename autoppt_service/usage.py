"""Usage records: agent turns, Office time and phase durations, in ``service/usage.jsonl``.

Records are append-only JSON lines.  Three kinds:

* ``agent_turn`` — one ``codex exec`` turn (``usage`` is the ``turn.completed``
  token block).  Records written by ``codex_imagegen_step`` have no ``kind``
  but carry ``usage``; they count as agent turns too.
* ``office`` — seconds PowerPoint was busy for a render/edit/QA operation.
* ``phase`` — wall-clock duration of one phase run.

:func:`summarize_usage` totals them per phase; with a price table it adds a
cost estimate (prices are inputs; nothing here is a published price).
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from autoppt_service.jobfiles import append_jsonl, read_jsonl, service_path, utc_now

USAGE_SUMMARY_SCHEMA = "autoppt-usage-summary-v1"
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def usage_log(job_dir: Path) -> Path:
    return service_path(job_dir, "usage.jsonl")


def record_agent_turn(job_dir: Path, phase: str, result: Any, **extra: Any) -> dict[str, Any]:
    """Log one turn; ``result`` is a ``CodexExecResult`` or its ``to_dict()``."""
    data = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    record = {
        "timestamp": utc_now(),
        "kind": "agent_turn",
        "phase": phase,
        "profile": data.get("profile", ""),
        "thread_id": data.get("thread_id", ""),
        "usage": data.get("usage") or {},
        "duration_s": data.get("duration_s"),
        "status": "ok" if data.get("returncode") == 0 and not data.get("timed_out") else "failed",
        "rate_limited": bool(data.get("rate_limited")),
        **extra,
    }
    append_jsonl(usage_log(job_dir), record)
    return record


def record_office(job_dir: Path, phase: str, seconds: float, operation: str, **extra: Any) -> dict[str, Any]:
    record = {
        "timestamp": utc_now(),
        "kind": "office",
        "phase": phase,
        "operation": operation,
        "seconds": round(float(seconds), 3),
        **extra,
    }
    append_jsonl(usage_log(job_dir), record)
    return record


@contextmanager
def phase_timer(job_dir: Path, phase: str, **extra: Any) -> Iterator[dict[str, Any]]:
    """Time a phase run; the record is written even when the phase raises."""
    info: dict[str, Any] = {"status": "ok"}
    started_at = utc_now()
    started = time.monotonic()
    try:
        yield info
    except BaseException:
        info["status"] = "failed"
        raise
    finally:
        append_jsonl(
            usage_log(job_dir),
            {
                "timestamp": utc_now(),
                "kind": "phase",
                "phase": phase,
                "started_at": started_at,
                "duration_s": round(time.monotonic() - started, 3),
                **extra,
                **info,
            },
        )


def _kind(record: dict[str, Any]) -> str:
    return record.get("kind") or ("agent_turn" if "usage" in record else "other")


def summarize_usage(job_dir: Path, *, prices: dict[str, float] | None = None) -> dict[str, Any]:
    """Totals per phase.  ``prices`` = USD per million ``input_tokens`` /
    ``cached_input_tokens`` / ``output_tokens``, plus optional ``office_minute``."""
    phases: dict[str, dict[str, Any]] = {}

    def bucket(name: str) -> dict[str, Any]:
        return phases.setdefault(
            name,
            {"agent_turns": 0, "failed_turns": 0, "rate_limited_turns": 0, "agent_seconds": 0.0,
             **{field: 0 for field in TOKEN_FIELDS}, "office_seconds": 0.0, "wall_seconds": 0.0, "runs": 0},
        )

    for record in read_jsonl(usage_log(job_dir)):
        entry = bucket(str(record.get("phase") or "unknown"))
        kind = _kind(record)
        if kind == "agent_turn":
            entry["agent_turns"] += 1
            entry["agent_seconds"] += float(record.get("duration_s") or 0)
            if record.get("status") not in ("ok", "generated"):
                entry["failed_turns"] += 1
            if record.get("rate_limited"):
                entry["rate_limited_turns"] += 1
            usage = record.get("usage") or {}
            for field in TOKEN_FIELDS:
                entry[field] += int(usage.get(field) or 0)
        elif kind == "office":
            entry["office_seconds"] += float(record.get("seconds") or 0)
        elif kind == "phase":
            entry["wall_seconds"] += float(record.get("duration_s") or 0)
            entry["runs"] += 1

    totals = {key: 0 for key in next(iter(phases.values()), {})}
    for entry in phases.values():
        entry["office_minutes"] = round(entry["office_seconds"] / 60, 2)
        for key, value in entry.items():
            totals[key] = totals.get(key, 0) + value
    for container in (*phases.values(), totals):
        for key in ("agent_seconds", "office_seconds", "wall_seconds", "office_minutes"):
            if key in container:
                container[key] = round(container[key], 3)
    summary: dict[str, Any] = {"schema_version": USAGE_SUMMARY_SCHEMA, "phases": phases, "totals": totals}
    if prices:
        # Codex usage: input_tokens includes cached_input_tokens, and output_tokens
        # includes reasoning_output_tokens, so only the uncached remainder is billed as input.
        def cost(entry: dict[str, Any]) -> float:
            uncached = max(entry.get("input_tokens", 0) - entry.get("cached_input_tokens", 0), 0)
            tokens = (
                uncached * prices.get("input_tokens", 0.0)
                + entry.get("cached_input_tokens", 0) * prices.get("cached_input_tokens", prices.get("input_tokens", 0.0))
                + entry.get("output_tokens", 0) * prices.get("output_tokens", 0.0)
            ) / 1_000_000
            return round(tokens + entry.get("office_minutes", 0) * prices.get("office_minute", 0.0), 6)

        summary["cost_usd"] = {name: cost(entry) for name, entry in phases.items()}
        summary["cost_usd_total"] = round(sum(summary["cost_usd"].values()), 6)
        summary["prices"] = dict(prices)
    return summary
