"""Build the pinned worker template and install its skill into Codex profiles.

The template is a whitelist copy of the read-only AutoPPT repository that
agents read and run during a job:

* ``autopptskills/``, ``autoppt_workflow/`` and ``scripts/`` as they are;
* ``improvement/`` reduced to reusable lessons (ledger fields that describe
  the pattern, the style handbook) with case names and paths redacted;
* ``docs/`` only for paths passed with ``--include-doc``;
* ``SERVICE_AGENTS.md`` from this service.

Excluded: ``deliverables/``, ``reviews/``, ``runs/``, ``交付/``,
``智搬小匠_*``, ``.tmp/``, root-level PPTX files, caches and VCS data.
Text files (md/json/jsonl/yaml/txt) get the redactions from
``templates/redactions.json``; code is never rewritten, but any redaction term
found in it is listed in ``template-manifest.json`` for an administrator.

    python -m autoppt_service.worker_template build --out D:\\autoppt-worker\\templates
    python -m autoppt_service.worker_template install --template <dir> --profiles profiles.json
    python -m autoppt_service.worker_template verify --template <dir> --profiles profiles.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from autoppt_service.jobfiles import sha256_file, utc_now, write_json_atomic
from autoppt_service.repo_paths import autoppt_repo

TEMPLATE_SCHEMA = "autoppt-worker-template-v1"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
SERVICE_AGENTS = TEMPLATES_DIR / "SERVICE_AGENTS.md"
REDACTIONS = TEMPLATES_DIR / "redactions.json"

WHOLE_DIRS = ("autopptskills", "autoppt_workflow", "scripts")
IMPROVEMENT_FILES = ("ppt-improvement-ledger.jsonl", "style-handbook.md")
LEDGER_FIELDS = (
    "schema_version", "timestamp", "scope", "category", "problem", "root_cause",
    "fix", "method_change", "lesson", "reusable_rule", "status",
)
EXCLUDED_TOP = {"deliverables", "reviews", "runs", "交付", ".tmp", ".git", "skills"}
EXCLUDED_PREFIXES = ("智搬小匠_",)
SKIPPED_PARTS = {"__pycache__", ".pytest_cache", ".git", ".tmp"}
SKIPPED_SUFFIXES = {".pyc", ".pyo", ".pptx", ".zip"}
TEXT_SUFFIXES = {".md", ".json", ".jsonl", ".yaml", ".yml", ".txt"}
CODE_SUFFIXES = {".py", ".ps1", ".mjs", ".js", ".ts", ".sh"}
SKILL_NAME = "autopptskills"

DEFAULT_BUDGETS = {"planning_timeout_s": 1800, "planning_attempts": 2,
                   "reconstruct_timeout_s": 4 * 3600, "reconstruct_attempts": 3}


class TemplateError(RuntimeError):
    pass


def load_redactions(path: Path = REDACTIONS) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        "terms": dict(data.get("terms", {})),
        "patterns": [(re.compile(item["pattern"]), item["replacement"]) for item in data.get("patterns", [])],
    }


def redact(text: str, redactions: dict[str, Any]) -> tuple[str, int]:
    count = 0
    for term, replacement in redactions["terms"].items():
        count += text.count(term)
        text = text.replace(term, replacement)
    for pattern, replacement in redactions["patterns"]:
        text, hits = pattern.subn(replacement, text)
        count += hits
    return text, count


def _skip(relative: PurePosixPath) -> bool:
    return bool(set(relative.parts) & SKIPPED_PARTS) or relative.suffix.lower() in SKIPPED_SUFFIXES


def _git_identity(repo: Path) -> dict[str, Any]:
    def git(*args: str) -> str:
        completed = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
        return completed.stdout.strip() if completed.returncode == 0 else ""

    commit = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain", "--", *WHOLE_DIRS, "improvement"))
    return {"commit": commit, "dirty": dirty}


def _copy_tree(repo: Path, top: str, out: Path, redactions: dict[str, Any], report: dict[str, Any]) -> None:
    source_root = repo / top
    if not source_root.is_dir():
        raise TemplateError(f"missing {top}/ in {repo}")
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        relative = PurePosixPath(path.relative_to(repo).as_posix())
        if _skip(relative):
            continue
        target = out / Path(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        suffix = path.suffix.lower()
        if suffix in TEXT_SUFFIXES:
            text, hits = redact(path.read_text(encoding="utf-8", errors="replace"), redactions)
            target.write_text(text, encoding="utf-8")
            if hits:
                report["redacted_files"][str(relative)] = hits
        else:
            shutil.copy2(path, target)
            if suffix in CODE_SUFFIXES:
                text = path.read_text(encoding="utf-8", errors="replace")
                found = sorted(term for term in redactions["terms"] if term in text)
                if found:
                    report["unredacted_code_hits"][str(relative)] = found


def _copy_improvement(repo: Path, out: Path, redactions: dict[str, Any], report: dict[str, Any]) -> None:
    target_dir = out / "improvement"
    target_dir.mkdir(parents=True, exist_ok=True)
    ledger = repo / "improvement" / "ppt-improvement-ledger.jsonl"
    lines = []
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            kept = {key: record[key] for key in LEDGER_FIELDS if key in record}
            text, hits = redact(json.dumps(kept, ensure_ascii=False), redactions)
            report["redactions"] += hits
            lines.append(text)
    (target_dir / "ppt-improvement-ledger.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    report["ledger_records"] = len(lines)
    handbook = repo / "improvement" / "style-handbook.md"
    if handbook.exists():
        text, hits = redact(handbook.read_text(encoding="utf-8"), redactions)
        report["redactions"] += hits
        (target_dir / "style-handbook.md").write_text(text, encoding="utf-8")


def inventory(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not _skip(PurePosixPath(path.relative_to(root).as_posix()))
    }


def build_template(
    out_root: Path,
    *,
    repo: Path | None = None,
    include_docs: Sequence[str] = (),
    version: str | None = None,
    redactions_path: Path = REDACTIONS,
) -> Path:
    """Write ``<out_root>/<version>/``; refuses to overwrite an existing version."""
    repo = (repo or autoppt_repo()).resolve()
    out_root = Path(out_root).resolve()
    if out_root == repo or repo in out_root.parents:
        raise TemplateError("the template must be built outside the read-only repository")
    identity = _git_identity(repo)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    version = version or f"{(identity['commit'] or 'nogit')[:12]}{'-dirty' if identity['dirty'] else ''}-{stamp}"
    out = out_root / version
    if out.exists():
        raise TemplateError(f"template version {version} already exists; versions are immutable")
    staging = out_root / f".{version}.staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    redactions = load_redactions(redactions_path)
    report: dict[str, Any] = {"redactions": 0, "redacted_files": {}, "unredacted_code_hits": {}, "ledger_records": 0}
    try:
        for top in WHOLE_DIRS:
            _copy_tree(repo, top, staging, redactions, report)
        _copy_improvement(repo, staging, redactions, report)
        for doc in include_docs:
            relative = PurePosixPath(doc.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "docs":
                raise TemplateError(f"--include-doc must be a path under docs/: {doc}")
            source = repo / Path(*relative.parts)
            if not source.is_file():
                raise TemplateError(f"missing doc: {doc}")
            target = staging / Path(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            text, hits = redact(source.read_text(encoding="utf-8"), redactions)
            report["redactions"] += hits
            target.write_text(text, encoding="utf-8")
        shutil.copy2(SERVICE_AGENTS, staging / "SERVICE_AGENTS.md")
        files = inventory(staging)
        excluded = sorted(
            entry.name for entry in repo.iterdir()
            if entry.name in EXCLUDED_TOP or entry.name.startswith(EXCLUDED_PREFIXES)
            or (entry.is_file() and entry.suffix.lower() == ".pptx")
        )
        manifest = {
            "schema_version": TEMPLATE_SCHEMA,
            "version": version,
            "built_at": utc_now(),
            "source_repo": str(repo),
            "source_commit": identity["commit"],
            "source_dirty": identity["dirty"],
            "included": [*WHOLE_DIRS, "improvement (lessons only)", *include_docs, "SERVICE_AGENTS.md"],
            "excluded_present_in_repo": excluded,
            "redaction": {
                "terms": len(redactions["terms"]),
                "replacements": report["redactions"] + sum(report["redacted_files"].values()),
                "redacted_files": report["redacted_files"],
                "unredacted_code_hits": report["unredacted_code_hits"],
                "ledger_records": report["ledger_records"],
            },
            "files": files,
        }
        write_json_atomic(staging / "template-manifest.json", manifest)
        staging.rename(out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return out


def load_template_manifest(template: Path) -> dict[str, Any]:
    path = Path(template) / "template-manifest.json"
    if not path.is_file():
        raise TemplateError(f"not a worker template (no template-manifest.json): {template}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_template(template: Path) -> dict[str, Any]:
    """Compare the template on disk with its manifest (detects edits after the build)."""
    manifest = load_template_manifest(template)
    actual = inventory(Path(template))
    actual.pop("template-manifest.json", None)
    expected = manifest["files"]
    return {
        "version": manifest["version"],
        "matching": actual == expected,
        "missing": sorted(expected.keys() - actual.keys()),
        "extra": sorted(actual.keys() - expected.keys()),
        "different": sorted(k for k in expected.keys() & actual.keys() if expected[k] != actual[k]),
    }


def install_skill(template: Path, codex_home: Path) -> Path:
    """Install the template's skill as ``<codex_home>/skills/autopptskills``, keeping a backup."""
    source = Path(template) / SKILL_NAME
    if not source.is_dir():
        raise TemplateError(f"template has no {SKILL_NAME}/")
    skills_dir = Path(codex_home).resolve() / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    target = skills_dir / SKILL_NAME
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    staging = skills_dir / f".{SKILL_NAME}.staging-{stamp}"
    shutil.copytree(source, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if inventory(staging) != inventory(source):
        shutil.rmtree(staging, ignore_errors=True)
        raise TemplateError("staged skill does not match the template")
    manifest = load_template_manifest(template)
    (staging / ".autoppt-template.json").write_text(
        json.dumps({"version": manifest["version"], "installed_at": utc_now()}, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if target.exists():
        target.rename(skills_dir / f"{SKILL_NAME}.backup-{stamp}")
    staging.rename(target)
    return target


def installed_skill_status(template: Path, codex_home: Path) -> dict[str, Any]:
    target = Path(codex_home) / "skills" / SKILL_NAME
    expected = inventory(Path(template) / SKILL_NAME)
    actual = inventory(target)
    actual.pop(".autoppt-template.json", None)
    marker = target / ".autoppt-template.json"
    return {
        "codex_home": str(codex_home),
        "installed_version": json.loads(marker.read_text(encoding="utf-8"))["version"] if marker.exists() else None,
        "matching": bool(expected) and expected == actual,
        "missing": sorted(expected.keys() - actual.keys()),
        "different": sorted(k for k in expected.keys() & actual.keys() if expected[k] != actual[k]),
    }


def render_service_agents(template: Path, *, job_id: str, budgets: dict[str, int] | None = None) -> str:
    template = Path(template).resolve()
    manifest = load_template_manifest(template)
    budgets = {**DEFAULT_BUDGETS, **(budgets or {})}
    values = {
        "TEMPLATE_ROOT": template.as_posix(),
        "TEMPLATE_VERSION": manifest["version"],
        "JOB_ID": job_id,
        "PLANNING_TIMEOUT_MIN": str(budgets["planning_timeout_s"] // 60),
        "PLANNING_ATTEMPTS": str(budgets["planning_attempts"]),
        "RECONSTRUCT_TIMEOUT_MIN": str(budgets["reconstruct_timeout_s"] // 60),
        "RECONSTRUCT_ATTEMPTS": str(budgets["reconstruct_attempts"]),
    }
    text = (template / "SERVICE_AGENTS.md").read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    leftover = re.findall(r"\{\{[A-Z_]+\}\}", text)
    if leftover:
        raise TemplateError(f"unfilled placeholders in SERVICE_AGENTS.md: {', '.join(sorted(set(leftover)))}")
    return text


def write_job_agents(job_dir: Path, template: Path, *, job_id: str, budgets: dict[str, int] | None = None) -> Path:
    """(Re)write the job's ``AGENTS.md`` before every agent phase, so an edited copy never persists."""
    path = Path(job_dir) / "AGENTS.md"
    path.write_text(render_service_agents(template, job_id=job_id, budgets=budgets), encoding="utf-8")
    return path


def _profiles(path: Path) -> list[Any]:
    from autoppt_service.codex_exec import load_profiles

    return [profile for profile in load_profiles(path) if profile.codex_home is not None]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build, install and verify the AutoPPT worker template.")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--version")
    build.add_argument("--include-doc", action="append", default=[])
    for name in ("install", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--template", type=Path, required=True)
        command.add_argument("--profiles", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "build":
        out = build_template(args.out, include_docs=args.include_doc, version=args.version)
        payload: Any = {"template": str(out), **{k: v for k, v in load_template_manifest(out).items() if k != "files"}}
        code = 0
    else:
        check = verify_template(args.template)
        if not check["matching"]:
            payload, code = {"template": check}, 2
        else:
            results = []
            for profile in _profiles(args.profiles):
                if args.command == "install":
                    install_skill(args.template, profile.codex_home)
                results.append({"profile": profile.name, **installed_skill_status(args.template, profile.codex_home)})
            payload = {"template": check, "profiles": results}
            code = 0 if all(item["matching"] for item in results) else 2
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
