"""Stop agent output from leaking credentials or other customers' data.

The Codex sandbox (``workspace-write``) blocks network and writes outside the
job, but not reads.  A prompt-injected agent could copy the account's
``auth.json`` tokens onto a slide, which would then be delivered to whoever
uploaded the malicious document.  Before anything leaves the worker,
:func:`scan_outputs` checks every file the agent can write for

* the actual secret strings of this machine's Codex profiles (``auth.json``
  and its backups), held only in memory and never logged, and
* generic credential shapes (JWTs, API keys, private keys, ``auth.json`` paths).

Findings name the file and the kind of match, never the matched text.
:func:`sanitize_lesson` strips customer specifics from lesson candidates.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

# Agent-written outputs.  Uploads (source-materials, intake) are the customer's own data and are not scanned.
SCANNED_DIRS = ("workspace/presentation_handoff", "workspace/vectorize", "output", "service", "assets/slides")
SCANNED_ROOT_FILES = ("deck-spec.json", "image-prompts.json", "AGENTS.md")
SKIPPED_PARTS = ("codex-runs", "codex-scratch", "checkpoints", "previews")
TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".xml", ".csv", ".html", ".svg"}
OFFICE_SUFFIXES = {".pptx", ".docx", ".xlsx"}
MIN_SECRET_LEN = 16

GENERIC_PATTERNS = {
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    "api_key": re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "auth_file": re.compile(r"(?i)\.codex[\\/]+auth\.json|\brefresh_token\b|\bid_token\b"),
}


def profile_secrets(codex_homes: Iterable[Path]) -> set[str]:
    """Secret strings from every ``auth.json*`` in the given Codex homes (kept in memory only)."""
    secrets: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str) and len(value) >= MIN_SECRET_LEN:
            secrets.add(value)
            for part in value.split("."):  # each JWT segment on its own
                if len(part) >= MIN_SECRET_LEN:
                    secrets.add(part)

    for home in codex_homes:
        for path in Path(home).glob("auth.json*"):
            try:
                collect(json.loads(path.read_text(encoding="utf-8-sig")))
            except (OSError, ValueError):
                continue
    return secrets


def _office_text(path: Path) -> str:
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            return "\n".join(
                archive.read(name).decode("utf-8", errors="replace")
                for name in archive.namelist()
                if name.endswith(".xml") and archive.getinfo(name).file_size < 20 * 1024 * 1024
            )
    except (zipfile.BadZipFile, OSError):
        return ""


def _candidate_files(job_dir: Path) -> Iterable[Path]:
    for name in SCANNED_ROOT_FILES:
        if (job_dir / name).is_file():
            yield job_dir / name
    for folder in SCANNED_DIRS:
        root = job_dir / folder
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and not set(path.relative_to(job_dir).parts) & set(SKIPPED_PARTS):
                if path.suffix.lower() in TEXT_SUFFIXES | OFFICE_SUFFIXES:
                    yield path


def scan_text(text: str, secrets: set[str]) -> list[str]:
    kinds = [kind for kind, pattern in GENERIC_PATTERNS.items() if pattern.search(text)]
    if any(secret in text for secret in secrets):
        kinds.insert(0, "profile_secret")
    return kinds


def scan_outputs(job_dir: Path, secrets: set[str]) -> list[dict[str, Any]]:
    job_dir = Path(job_dir)
    findings = []
    for path in _candidate_files(job_dir):
        if path.suffix.lower() in OFFICE_SUFFIXES:
            text = _office_text(path)
        else:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        kinds = scan_text(text, secrets)
        if kinds:
            findings.append({"file": path.relative_to(job_dir).as_posix(), "kinds": kinds})
    return findings


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*%?")
LESSON_FIELDS = ("problem", "root_cause", "method_change", "lesson", "evidence", "category", "phase")


def sanitize_lesson(lesson: dict[str, Any], job_terms: Sequence[str]) -> dict[str, Any]:
    """Keep only pattern-level fields; remove customer terms, numbers and paths."""
    terms = sorted({t.strip() for t in job_terms if t and len(t.strip()) >= 2}, key=len, reverse=True)
    clean: dict[str, Any] = {}
    for key in LESSON_FIELDS:
        if key not in lesson:
            continue
        text = str(lesson[key])[:2000]
        for term in terms:
            text = text.replace(term, "[客户内容]")
        text = re.sub(r"[A-Za-z]:[\\/][^\s\"']*", "[路径]", text)
        text = _NUMBER.sub("<n>", text)
        for kind, pattern in GENERIC_PATTERNS.items():
            text = pattern.sub(f"[{kind}]", text)
        clean[key] = text
    clean["sanitized"] = True
    return clean
