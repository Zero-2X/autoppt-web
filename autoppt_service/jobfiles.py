"""Job-directory layout and file helpers shared by the service modules.

A job directory holds everything for one order, so a different Worker can pick
it up after a checkpoint::

    <job_dir>/
      AGENTS.md                      service-mode instructions for codex exec
      source-materials/              uploaded originals (read-only for the agent)
      workspace/intake/              parser output (media, unpacked archives)
      workspace/evidence_workspace/  source manifest, evidence ledger, chart data
      workspace/presentation_handoff/ planning brief, then slide_brief.json
      service/                       contract files between agent, Worker and backend

Files under ``service/`` are written atomically (temp file + rename) because
another process may be reading them.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

SERVICE_DIR = "service"
SOURCE_DIR = "source-materials"
INTAKE_DIR = "workspace/intake"
EVIDENCE_DIR = "workspace/evidence_workspace"
HANDOFF_DIR = "workspace/presentation_handoff"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def service_path(job_dir: Path, name: str) -> Path:
    return Path(job_dir) / SERVICE_DIR / name


def write_json_atomic(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not Path(path).exists():
        return []
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue  # a half-written last line from a crashed writer
        if isinstance(value, dict):
            records.append(value)
    return records


def job_relative(job_dir: Path, value: str | Path) -> Path:
    """Resolve a job-relative path and refuse anything that leaves the job directory."""
    root = Path(job_dir).resolve()
    raw = str(value).replace("\\", "/")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":") or ".." in PurePosixPath(raw).parts:
        raise ValueError(f"path must be relative to the job directory: {value}")
    resolved = (root / raw).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path leaves the job directory: {value}")
    return resolved


def relative_to_job(job_dir: Path, path: Path) -> str:
    return Path(path).resolve().relative_to(Path(job_dir).resolve()).as_posix()
