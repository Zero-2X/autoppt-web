"""Worker self-check: run before a machine takes jobs; also decides its capabilities.

    python -m autoppt_service.worker_check [--template <dir>] [--profiles profiles.json] [--json-out report.json]

Checks Python and packages, the Codex CLI version, the worker template (and
that it was not edited), Codex profile homes, disk space, Office (PowerPoint
COM), the Office fonts, Windows OCR for Chinese, Node, and the workflow's own
``check_editable_backends.py``.  Capabilities: ``intake`` and ``package`` need
the basics; ``codex`` needs the CLI, a verified template and a profile;
``office`` needs PowerPoint and both fonts.  A machine that fails the basics
must not start the worker loop.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

MIN_CODEX = (0, 163, 0)
MIN_FREE_GB = 10.0
REQUIRED_MODULES = ("jsonschema", "numpy", "openpyxl", "PIL", "pypdf", "docx", "pptx", "httpx", "psutil")
FONTS = {"Microsoft YaHei": "msyh.ttc", "DengXian": "Deng.ttf"}


def _check(name: str, ok: bool, detail: str = "", *, required_for: Sequence[str] = ()) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail, "required_for": list(required_for)}


def _run(argv: Sequence[str], timeout: float = 60) -> tuple[int, str]:
    try:
        completed = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=timeout, check=False)
        return completed.returncode, (completed.stdout + completed.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)


def check_python() -> list[dict[str, Any]]:
    checks = [_check("python>=3.11", sys.version_info >= (3, 11), sys.version.split()[0], required_for=["basics"])]
    missing = [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]
    checks.append(_check("python packages", not missing, "missing: " + ", ".join(missing) if missing else "ok", required_for=["basics"]))
    return checks


def check_disk(path: Path, min_free_gb: float = MIN_FREE_GB) -> dict[str, Any]:
    path.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(path).free / 1024**3
    return _check(f"disk free at {path}", free >= min_free_gb, f"{free:.1f} GB free (need {min_free_gb} GB)", required_for=["basics"])


def codex_version(binary: str | None = None) -> tuple[int, ...] | None:
    from autoppt_service.codex_exec import codex_command

    try:
        command = [binary] if binary else codex_command()
    except FileNotFoundError:
        return None
    code, output = _run([*command, "--version"])
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
    return tuple(int(x) for x in match.groups()) if code == 0 and match else None


def check_codex(template: Path | None, profiles: Path | None) -> list[dict[str, Any]]:
    version = codex_version()
    checks = [_check("codex cli >= 0.163.0", version is not None and version >= MIN_CODEX,
                     ".".join(map(str, version)) if version else "not found (set AUTOPPT_CODEX_BIN)", required_for=["codex"])]
    if template is None:
        checks.append(_check("worker template", False, "no --template given", required_for=["codex"]))
    else:
        from autoppt_service.worker_template import verify_template

        try:
            result = verify_template(template)
            checks.append(_check("worker template unchanged", result["matching"], result["version"], required_for=["codex"]))
        except Exception as exc:  # noqa: BLE001 - report, do not crash the check
            checks.append(_check("worker template unchanged", False, str(exc), required_for=["codex"]))
    from autoppt_service.codex_exec import load_profiles

    try:
        homes = [p for p in load_profiles(profiles)]
        missing = [p.name for p in homes if p.codex_home is not None and not p.codex_home.is_dir()]
        checks.append(_check("codex profiles", bool(homes) and not missing,
                             f"{len(homes)} profiles" + (f"; missing homes: {', '.join(missing)}" if missing else ""),
                             required_for=["codex"]))
    except (OSError, ValueError, KeyError) as exc:
        checks.append(_check("codex profiles", False, str(exc), required_for=["codex"]))
    return checks


def check_office(fonts_dir: Path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts") -> list[dict[str, Any]]:
    checks = []
    if sys.platform != "win32":
        return [_check("windows", False, sys.platform, required_for=["office"])]
    try:
        import win32com.client  # noqa: F401

        code, output = _run(["powershell", "-NoProfile", "-Command",
                             "$p = New-Object -ComObject PowerPoint.Application; $v = $p.Version; $p.Quit(); $v"], timeout=120)
        checks.append(_check("powerpoint com", code == 0 and bool(output.strip()), output.strip()[-80:], required_for=["office"]))
    except ImportError:
        checks.append(_check("powerpoint com", False, "pywin32 not installed", required_for=["office"]))
    for family, filename in FONTS.items():
        checks.append(_check(f"font {family}", (fonts_dir / filename).exists(), filename, required_for=["office"]))
    code, output = _run(["powershell", "-NoProfile", "-Command",
                         "Add-Type -AssemblyName System.Runtime.WindowsRuntime; "
                         "$null=[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]; "
                         "[Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag }"])
    checks.append(_check("windows ocr zh-Hans", code == 0 and "zh-Hans" in output, output.replace("\n", ", ")[:120],
                         required_for=["office"]))
    return checks


def check_node() -> dict[str, Any]:
    code, output = _run(["node", "--version"])
    return _check("node", code == 0, output, required_for=[])


def check_workflow_backends() -> dict[str, Any]:
    from autoppt_service.repo_paths import ensure_autoppt_on_path

    script = ensure_autoppt_on_path() / "autopptskills" / "scripts" / "check_editable_backends.py"
    code, output = _run([sys.executable, "-B", str(script)], timeout=120)
    return _check("workflow editable backends", code == 0, output[-300:], required_for=[])


def capabilities(checks: list[dict[str, Any]]) -> list[str]:
    failed = {need for check in checks if not check["ok"] for need in check["required_for"]}
    if "basics" in failed:
        return []
    caps = ["intake", "package"]
    if "codex" not in failed:
        caps.append("codex")
    if "office" not in failed:
        caps.append("office")
    return caps


def run_checks(
    *,
    template: Path | None = None,
    profiles: Path | None = None,
    jobs_root: Path = Path("."),
    include_office: bool = True,
    extra: Sequence[Callable[[], dict[str, Any]]] = (),
    min_free_gb: float = MIN_FREE_GB,
) -> dict[str, Any]:
    checks = [*check_python(), check_disk(jobs_root, min_free_gb), *check_codex(template, profiles)]
    if include_office:
        checks += check_office()
    checks.append(check_node())
    checks += [fn() for fn in extra]
    caps = capabilities(checks)
    return {"schema_version": "autoppt-worker-check-v1", "ok": bool(caps), "capabilities": caps, "checks": checks}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check this machine before it takes AutoPPT jobs.")
    parser.add_argument("--template", type=Path)
    parser.add_argument("--profiles", type=Path)
    parser.add_argument("--jobs-root", type=Path, default=Path("."))
    parser.add_argument("--skip-office", action="store_true")
    parser.add_argument("--with-workflow-check", action="store_true")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    report = run_checks(template=args.template, profiles=args.profiles, jobs_root=args.jobs_root,
                        include_office=not args.skip_office,
                        extra=[check_workflow_backends] if args.with_workflow_check else [])
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.json_out:
        args.json_out.write_text(payload, encoding="utf-8")
    sys.stdout.buffer.write(payload.encode("utf-8"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
