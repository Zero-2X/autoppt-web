"""Run the gold sample set (intake + real planning) and record time, tokens and checks.

    python scripts/run_samples.py <samples_dir> --jobs-root D:/autoppt-jobs/samples --template <dir> --profiles profiles.json

Each sample is a folder with ``request.json`` and the files it names (``uploads`` are
matched by file name).  Results are appended to ``<jobs_root>/sample-results.jsonl``
with the worker template version, so every template upgrade can be compared with the
previous one.  This uses real Codex quota: run it deliberately, not in CI.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autoppt_service.jobfiles import read_json, read_jsonl, service_path, utc_now  # noqa: E402
from autoppt_service.run_job import run_phase  # noqa: E402
from autoppt_service.status import load_job_state  # noqa: E402
from autoppt_service.usage import summarize_usage  # noqa: E402
from autoppt_service.worker_template import load_template_manifest  # noqa: E402


def run_sample(sample: Path, jobs_root: Path, *, template: Path, profiles: Path | None) -> dict:
    request = json.loads((sample / "request.json").read_text(encoding="utf-8-sig"))
    uploads = {entry["file_id"]: str(sample / entry["name"]) for entry in request["inputs"].get("files", [])}
    job_id = f"{sample.name}-{time.strftime('%Y%m%d%H%M%S')}"
    job_dir = jobs_root / job_id
    started = time.monotonic()
    result = {"sample": sample.name, "job_id": job_id, "started_at": utc_now(),
              "template_version": load_template_manifest(template)["version"]}
    intake = run_phase(job_dir, "intake", job_id=job_id, request=request, uploads=uploads)
    result["intake"] = intake
    if intake["job_state"] == "planning":
        result["planning"] = run_phase(job_dir, "planning", job_id=job_id, template=template, profiles_path=profiles)
    state = load_job_state(job_dir) or {}
    report = read_json(service_path(job_dir, "planning-report.json")) or {}
    result.update(
        wall_seconds=round(time.monotonic() - started, 1),
        final_state=state.get("state"),
        blocked_reason=state.get("blocked_reason", ""),
        planning_attempts=len(report.get("attempts", [])),
        planning_warnings=report.get("warnings", []),
        slides=report.get("slides"),
        usage=summarize_usage(job_dir)["totals"],
        turns=[{k: r.get(k) for k in ("phase", "duration_s", "status", "usage_source", "usage")}
               for r in read_jsonl(service_path(job_dir, "usage.jsonl")) if r.get("kind") == "agent_turn"],
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("samples", type=Path)
    parser.add_argument("--jobs-root", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--profiles", type=Path)
    parser.add_argument("--only", nargs="*", default=[])
    args = parser.parse_args()
    args.jobs_root.mkdir(parents=True, exist_ok=True)
    out = args.jobs_root / "sample-results.jsonl"
    for sample in sorted(p for p in args.samples.iterdir() if (p / "request.json").exists()):
        if args.only and sample.name not in args.only:
            continue
        result = run_sample(sample, args.jobs_root, template=args.template, profiles=args.profiles)
        with out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(json.dumps({k: result[k] for k in ("sample", "final_state", "wall_seconds", "slides")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
