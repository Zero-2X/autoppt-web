"""Run ``codex exec`` headlessly under a selected Codex account profile.

Each profile is one ``CODEX_HOME`` directory that the operator logged in with
``codex login``.  This module never reads, copies, or writes credentials; it
only points the child process at a profile and records what the run used.

Do not pass ``--ephemeral``: built-in ImageGen provenance is read back from the
rollout JSONL that Codex persists under ``<CODEX_HOME>/sessions``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "usage limit", "usage_limit", "429", "quota")


GATEWAY_URL_ENV = "AUTOPPT_GATEWAY_URL"
GATEWAY_TOKEN_ENV = "AUTOPPT_GATEWAY_TOKEN"  # Codex keeps *TOKEN* variables out of agent shell commands


@dataclass(frozen=True)
class CodexProfile:
    """One logged-in Codex account; ``codex_home=None`` uses the default home.

    ``gateway=True`` (API-key mode, D2): no login; ``codex exec`` talks to the AutoPPT model gateway with the
    per-job token the Worker obtained for its lease (``AUTOPPT_GATEWAY_URL`` / ``AUTOPPT_GATEWAY_TOKEN``)."""

    name: str
    codex_home: Path | None = None
    gateway: bool = False

    def home(self) -> Path:
        if self.codex_home is not None:
            return self.codex_home
        env_home = os.getenv("CODEX_HOME", "").strip()
        return Path(env_home) if env_home else Path.home() / ".codex"

    def sessions_root(self) -> Path:
        return self.home() / "sessions"


@dataclass
class CodexExecResult:
    profile: str
    returncode: int | None
    thread_id: str
    duration_s: float
    usage: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    timed_out: bool = False
    events_path: str = ""
    last_message: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and bool(self.thread_id)

    @property
    def rate_limited(self) -> bool:
        text = " ".join(self.errors).lower()
        return any(marker in text for marker in RATE_LIMIT_MARKERS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "returncode": self.returncode,
            "thread_id": self.thread_id,
            "duration_s": round(self.duration_s, 3),
            "usage": self.usage,
            "errors": self.errors,
            "timed_out": self.timed_out,
            "rate_limited": self.rate_limited,
            "events_path": self.events_path,
        }


def load_profiles(path: Path | None) -> list[CodexProfile]:
    """Read ``[{"name": ..., "codex_home": ...}]``; no file means the default home."""
    if path is None:
        return [CodexProfile(name="default")]
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    profiles = []
    for item in payload.get("profiles", payload) if isinstance(payload, dict) else payload:
        home = str(item.get("codex_home") or "").strip()
        profiles.append(CodexProfile(name=str(item["name"]), codex_home=Path(home) if home else None,
                                     gateway=bool(item.get("gateway"))))
    if not profiles:
        raise ValueError(f"no Codex profiles in {path}")
    return profiles


class ProfilePool:
    """Round-robin over profiles, resting any that hit a usage or rate limit."""

    def __init__(self, profiles: Sequence[CodexProfile], *, cooldown_s: float = 900.0) -> None:
        if not profiles:
            raise ValueError("ProfilePool needs at least one profile")
        self._profiles = list(profiles)
        self._cooldown_s = cooldown_s
        self._resting_until: dict[str, float] = {}
        self._next = 0

    def acquire(self) -> CodexProfile | None:
        now = time.monotonic()
        for offset in range(len(self._profiles)):
            profile = self._profiles[(self._next + offset) % len(self._profiles)]
            if self._resting_until.get(profile.name, 0.0) <= now:
                self._next = (self._next + offset + 1) % len(self._profiles)
                return profile
        return None

    def rest(self, profile: CodexProfile) -> None:
        self._resting_until[profile.name] = time.monotonic() + self._cooldown_s


# The real runs used 0.163.0; an unpinned run on 2026-10-18 picked up 0.155.1 from PATH.  (Its HTTP 400
# "model not supported" turned out to be the account, not the CLI: see AUTOPPT_CODEX_MODEL.)
MIN_CODEX_VERSION = (0, 163, 0)
_checked_versions: dict[str, tuple[int, ...] | None] = {}


def codex_version(binary: str) -> tuple[int, ...] | None:
    """``(major, minor, patch)`` reported by ``codex --version``, or None when it cannot be read."""
    if binary not in _checked_versions:
        try:
            completed = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
            match = re.search(r"(\d+)\.(\d+)\.(\d+)", completed.stdout or "")
            _checked_versions[binary] = tuple(int(part) for part in match.groups()) if match else None
        except (OSError, subprocess.TimeoutExpired):
            _checked_versions[binary] = None
    return _checked_versions[binary]


def codex_command() -> list[str]:
    """Resolve the Codex CLI; ``AUTOPPT_CODEX_BIN`` pins a specific build.

    A CLI older than ``MIN_CODEX_VERSION`` is refused before any session starts.
    """
    pinned = os.getenv("AUTOPPT_CODEX_BIN", "").strip()
    found = pinned or shutil.which("codex")
    if not found:
        raise FileNotFoundError("codex CLI not found; install it or set AUTOPPT_CODEX_BIN")
    version = codex_version(found)
    if version is not None and version < MIN_CODEX_VERSION:
        wanted = ".".join(map(str, MIN_CODEX_VERSION))
        raise FileNotFoundError(f"codex CLI {'.'.join(map(str, version))} at {found} is older than {wanted}; "
                                "set AUTOPPT_CODEX_BIN to a current build")
    return [found]


def windows_sandbox_mode() -> str | None:
    """The Windows sandbox implementation every service run uses.

    Measured 2026-10-17 (Codex 0.163.0): with this machine's ``windows.sandbox = "elevated"``
    (elevated setup never completed) ``codex exec -s workspace-write`` reached the network and
    wrote outside the job directory.  With ``"unelevated"`` (restricted token) network and
    outside writes are blocked; reads are not restricted in either mode without the elevated
    setup.  ``AUTOPPT_CODEX_WINDOWS_SANDBOX`` overrides; an empty value leaves the config alone.
    """
    if os.name != "nt":
        return None
    value = os.getenv("AUTOPPT_CODEX_WINDOWS_SANDBOX", "unelevated").strip()
    return value or None


def gateway_args(base_url: str) -> list[str]:
    """Config overrides that point ``codex exec`` at the AutoPPT gateway; nothing is written to config.toml."""
    return [
        "-c", 'model_provider="autoppt"',
        "-c", 'model_providers.autoppt.name="AutoPPT gateway"',
        "-c", f'model_providers.autoppt.base_url="{base_url}"',
        "-c", f'model_providers.autoppt.env_key="{GATEWAY_TOKEN_ENV}"',
        "-c", 'model_providers.autoppt.wire_api="responses"',
    ]


def build_exec_args(
    *,
    sandbox: str = "read-only",
    model: str | None = None,
    reasoning_effort: str | None = None,
    images: Iterable[Path] = (),
    output_schema: Path | None = None,
    last_message_path: Path | None = None,
    windows_sandbox: str | None = "auto",
) -> list[str]:
    args = ["exec", "--skip-git-repo-check", "--json", "-s", sandbox]
    mode = windows_sandbox_mode() if windows_sandbox == "auto" else windows_sandbox
    if mode:
        args += ["-c", f'windows.sandbox="{mode}"']
    if model:
        args += ["-m", model]
    if reasoning_effort:
        args += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
    for image in images:
        args += ["-i", str(image)]
    if output_schema is not None:
        args += ["--output-schema", str(output_schema)]
    if last_message_path is not None:
        args += ["-o", str(last_message_path)]
    args.append("-")  # read the prompt from stdin so long CJK text is passed intact
    return args


def parse_events(lines: Iterable[str]) -> tuple[str, dict[str, Any], list[str]]:
    """Return ``(thread_id, usage, errors)`` from ``codex exec --json`` output."""
    thread_id = ""
    usage: dict[str, Any] = {}
    errors: list[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = str(event.get("thread_id") or "")
        elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
        elif kind == "error":
            errors.append(str(event.get("message") or ""))
        elif kind == "turn.failed":
            errors.append(str((event.get("error") or {}).get("message") or "turn_failed"))
    return thread_id, usage, errors


def run_codex_exec(
    prompt: str,
    *,
    cwd: Path,
    profile: CodexProfile,
    run_dir: Path,
    command: Sequence[str] | None = None,
    timeout_s: float = 900.0,
    **exec_options: Any,
) -> CodexExecResult:
    """Run one ``codex exec`` turn and keep its event log under ``run_dir``."""
    if profile.codex_home is not None and not profile.codex_home.is_dir():
        # A missing home means the operator never ran ``codex login`` for it.
        return CodexExecResult(
            profile=profile.name,
            returncode=None,
            thread_id="",
            duration_s=0.0,
            errors=[f"codex_home_missing:{profile.codex_home}"],
        )
    cwd.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    events_path = run_dir / "events.jsonl"
    last_message_path = run_dir / "last-message.txt"
    if not exec_options.get("model") and os.getenv("AUTOPPT_CODEX_MODEL", "").strip():
        # Measured 2026-10-18: the account stopped offering the model named in the shared config.toml
        # (HTTP 400 "model is not supported"); the Worker pins a model per run instead of editing that file.
        exec_options["model"] = os.environ["AUTOPPT_CODEX_MODEL"].strip()
    argv = [*(command or codex_command()), *build_exec_args(last_message_path=last_message_path, **exec_options)]
    env = dict(os.environ)
    if profile.gateway:
        if not (env.get(GATEWAY_URL_ENV) and env.get(GATEWAY_TOKEN_ENV)):
            return CodexExecResult(profile=profile.name, returncode=None, thread_id="", duration_s=0.0,
                                   errors=["gateway_token_missing: the Worker did not obtain a model token for this lease"])
        argv[-1:-1] = gateway_args(env[GATEWAY_URL_ENV])  # before the trailing "-" (prompt on stdin)
    if profile.codex_home is not None:
        env["CODEX_HOME"] = str(profile.codex_home)
    # Bytes, not text mode: Windows text pipes rewrite "\n" as "\r\n", which
    # would make the prompt Codex receives differ from the recorded prompt.
    prompt_bytes = prompt.encode("utf-8")
    (run_dir / "prompt.txt").write_bytes(prompt_bytes)
    started = time.monotonic()
    timed_out = False
    with events_path.open("wb") as events, (run_dir / "stderr.txt").open("wb") as stderr:
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd),
                env=env,
                input=prompt_bytes,
                stdout=events,
                stderr=stderr,
                timeout=timeout_s,
                check=False,
            )
            returncode: int | None = completed.returncode
        except subprocess.TimeoutExpired:
            returncode, timed_out = None, True
    duration = time.monotonic() - started
    thread_id, usage, errors = parse_events(events_path.read_text(encoding="utf-8", errors="replace").splitlines())
    if timed_out:
        errors.append(f"codex_exec_timeout:{timeout_s}s")
    last_message = last_message_path.read_text(encoding="utf-8", errors="replace") if last_message_path.exists() else ""
    return CodexExecResult(
        profile=profile.name,
        returncode=returncode,
        thread_id=thread_id,
        duration_s=duration,
        usage=usage,
        errors=errors,
        timed_out=timed_out,
        events_path=str(events_path),
        last_message=last_message,
    )


def find_rollout(profile: CodexProfile, thread_id: str) -> Path | None:
    """Locate the rollout JSONL whose filename carries ``thread_id``."""
    root = profile.sessions_root()
    if not thread_id or not root.exists():
        return None
    matches = [path for path in root.rglob(f"*{thread_id}*.jsonl")]
    return max(matches, key=lambda item: item.stat().st_mtime_ns) if matches else None
