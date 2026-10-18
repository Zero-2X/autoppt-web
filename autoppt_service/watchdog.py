"""Watchdog for phases that drive Office: time limits, hung Office processes, temp dirs, disk.

* :func:`guarded_phase` runs a phase with ``TEMP``/``TMP`` pointed at the job's
  own ``tmp`` folder (Office leaves large temp files), and afterwards kills any
  PowerPoint/Excel/Word process that was started during the phase and is still
  running (a crashed COM client leaves them behind, and the next job would hang
  on a half-open dialog).
* A background timer enforces the phase time limit by killing those processes,
  which makes the blocked COM call fail fast instead of hanging forever.
* :func:`disk_ok` lets the worker refuse new leases when the disk is low.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psutil

OFFICE_PROCESSES = {"powerpnt.exe", "excel.exe", "winword.exe"}


class PhaseTimeout(RuntimeError):
    pass


def office_processes(started_after: float | None = None) -> list[psutil.Process]:
    found = []
    for process in psutil.process_iter(["name", "create_time"]):
        try:
            name = (process.info["name"] or "").lower()
            if name in OFFICE_PROCESSES and (started_after is None or process.info["create_time"] >= started_after - 1):
                found.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def kill_processes(processes: list[psutil.Process]) -> list[str]:
    killed = []
    for process in processes:
        try:
            name = process.name()
            process.kill()
            killed.append(f"{name}:{process.pid}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    psutil.wait_procs(processes, timeout=10)
    return killed


def disk_ok(path: Path, min_free_gb: float) -> tuple[bool, float]:
    path.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(path).free / 1024**3
    return free >= min_free_gb, free


# TEMP/TMP point at the running phase's own folder so Office and Codex leave their scratch files in the job.
# They are process-wide: with several phases in one process (the soak test runs three workers), saving and
# restoring per phase raced and left TEMP on a deleted job folder, after which SQLite could not create temp
# files ("unable to open database file", 2026-10-18).  Active phases are tracked under a lock instead.
_temp_lock = threading.Lock()
_active_temps: list[Path] = []
_original_temp: dict[str, str | None] = {}


def _point_temp(path: Path | None) -> None:
    for key in ("TEMP", "TMP"):
        value = str(path) if path is not None else _original_temp.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _enter_temp(temp: Path) -> None:
    with _temp_lock:
        if not _active_temps:
            _original_temp.update({key: os.environ.get(key) for key in ("TEMP", "TMP")})
        _active_temps.append(temp)
        _point_temp(temp)


def _leave_temp(temp: Path) -> None:
    with _temp_lock:
        if temp in _active_temps:
            _active_temps.remove(temp)
        _point_temp(_active_temps[-1] if _active_temps else None)


@contextmanager
def guarded_phase(job_dir: Path, *, timeout_s: float, report: dict | None = None) -> Iterator[dict]:
    """Run a phase under the watchdog; ``report`` collects what the watchdog did."""
    report = report if report is not None else {}
    report.setdefault("killed", [])
    temp = Path(job_dir) / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    _enter_temp(temp)
    started = time.time()
    fired = threading.Event()

    def on_timeout() -> None:
        fired.set()
        report["killed"] += kill_processes(office_processes(started))

    timer = threading.Timer(timeout_s, on_timeout)
    timer.daemon = True
    timer.start()
    try:
        yield report
        if fired.is_set():
            raise PhaseTimeout(f"phase exceeded {timeout_s:.0f}s; Office processes were stopped")
    finally:
        timer.cancel()
        report["killed"] += kill_processes(office_processes(started))  # leftovers from this phase only
        _leave_temp(temp)
        shutil.rmtree(temp, ignore_errors=True)
