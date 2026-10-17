"""Worker loop against the backend API: lease a phase, run it, upload, report.

    python -m autoppt_service.worker_client --api http://127.0.0.1:8000 --token <worker token> \\
        --jobs-root D:\\autoppt-jobs\\worker1 [--mock] [--template <dir> --profiles profiles.json] [--once]

For each lease the worker:

1. restores the job directory from the last snapshot if this machine does not
   have it (another worker may have run the earlier phases);
2. downloads the uploads for intake, and writes the user's checkpoint answer
   to ``service/checkpoint-response.json`` when one is waiting;
3. runs the phase with :func:`autoppt_service.run_job.run_phase`, sending
   heartbeats from a background thread;
4. uploads checkpoint previews, deliverables and a snapshot of the job
   directory, then reports the reached state, new usage records and lesson
   candidates (idempotent: the same report can be resent safely).

A phase that raises is reported as failed; configuration problems
(``PhaseNotReady``/``PhaseError``) are not retried.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from autoppt_service.jobfiles import read_json, read_jsonl, service_path
from autoppt_service.parsers.archive import safe_member_path
from autoppt_service.run_job import PhaseError, PhaseNotReady, run_phase
from autoppt_service.status import CHECKPOINT_STATES, load_job_state

# Raw agent event logs contain whatever the agent read (possibly other files on the machine): never leave the worker.
SNAPSHOT_EXCLUDE = ("service/codex-scratch/", "service/.snapshot-sha256", "tmp/")
SNAPSHOT_EXCLUDE_NAMES = ("events.jsonl", "stderr.txt")
PHASE_TIMEOUTS_S = {"intake": 1800, "planning": 3 * 3600, "masters": 6 * 3600, "reconstruct": 12 * 3600, "package": 3600}
FINISHED_STATES = ("delivered", "failed", "cancelled")
SNAPSHOT_MAX_BYTES = 2 * 1024 * 1024 * 1024


class WorkerClientError(RuntimeError):
    pass


def snapshot_bytes(job_dir: Path) -> bytes:
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(job_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(job_dir).as_posix()
            if relative.startswith(SNAPSHOT_EXCLUDE) or "__pycache__" in relative or path.name in SNAPSHOT_EXCLUDE_NAMES:
                continue
            total += path.stat().st_size
            if total > SNAPSHOT_MAX_BYTES:
                raise WorkerClientError("job directory too large to snapshot")
            archive.write(path, relative)
    return buffer.getvalue()


def restore_snapshot(data: bytes, job_dir: Path) -> None:
    """Unpack a snapshot made by :func:`snapshot_bytes`, refusing paths outside the job directory."""
    staging = job_dir.with_name(job_dir.name + ".restoring")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            relative = safe_member_path(info.filename)
            target = staging.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    shutil.rmtree(job_dir, ignore_errors=True)
    staging.rename(job_dir)


class ApiWorker:
    def __init__(
        self,
        http: Any,
        token: str,
        jobs_root: Path,
        *,
        mock: bool = False,
        template: Path | None = None,
        profiles: Path | None = None,
        template_version: str = "",
        heartbeat_s: float = 60.0,
        command: Sequence[str] | None = None,
        min_free_gb: float = 10.0,
        phase_timeouts: dict[str, float] | None = None,
        cleanup_local: bool = True,
    ) -> None:
        self.http = http  # httpx.Client or a FastAPI TestClient
        self.headers = {"Authorization": f"Bearer {token}"}
        self.jobs_root = Path(jobs_root)
        self.mock, self.template, self.profiles = mock, template, profiles
        self.template_version = template_version
        self.heartbeat_s = heartbeat_s
        self.command = command
        self.min_free_gb = min_free_gb
        self.phase_timeouts = {**PHASE_TIMEOUTS_S, **(phase_timeouts or {})}
        self.cleanup_local = cleanup_local
        self._secrets: set[str] | None = None

    def secrets(self) -> set[str]:
        """This machine's Codex credentials, loaded once and kept only in memory for the leak guard."""
        if self._secrets is None:
            from autoppt_service.codex_exec import load_profiles
            from autoppt_service.leakguard import profile_secrets

            homes = [profile.home() for profile in load_profiles(self.profiles)]
            self._secrets = profile_secrets(homes)
        return self._secrets

    # ------------------------------------------------------------ http helpers

    def _post(self, url: str, payload: dict[str, Any], **headers: str) -> Any:
        response = self.http.post(url, json=payload, headers={**self.headers, **headers})
        if response.status_code == 204:
            return None
        if response.status_code >= 400:
            raise WorkerClientError(f"{url}: {response.status_code} {response.text[:500]}")
        return response.json()

    def _download(self, url: str) -> bytes:
        response = self.http.get(url)
        if response.status_code != 200:
            raise WorkerClientError(f"download failed: {response.status_code}")
        return response.content

    def _upload(self, job_id: str, lease_token: str, kind: str, path: str, data: bytes) -> dict[str, Any]:
        response = self.http.put(
            f"/api/worker/jobs/{job_id}/artifacts",
            params={"kind": kind, "path": path, "lease_token": lease_token},
            content=data,
            headers=self.headers,
        )
        if response.status_code >= 400:
            raise WorkerClientError(f"artifact upload failed: {response.status_code} {response.text[:300]}")
        return response.json()

    # ------------------------------------------------------------ one lease

    def run_once(self) -> dict[str, Any] | None:
        from autoppt_service.watchdog import disk_ok

        enough, free = disk_ok(self.jobs_root, self.min_free_gb)
        if not enough:
            # Never start a phase that could fill the disk (Office renders and snapshots are large).
            return {"skipped": "disk_low", "free_gb": round(free, 1)}
        lease = self._post("/api/worker/lease", {"template_version": self.template_version})
        if lease is None:
            return None
        job_id, phase, lease_token = lease["job_id"], lease["phase"], lease["lease_token"]
        job_dir = self.jobs_root / job_id
        stop = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(job_id, lease_token, stop), daemon=True)
        beat.start()
        try:
            self._prepare(job_dir, lease)
            uploads = self._fetch_uploads(job_dir, lease) if phase == "intake" else {}
            from autoppt_service.watchdog import guarded_phase

            with guarded_phase(job_dir, timeout_s=self.phase_timeouts.get(phase, 3600)) as watchdog:
                result = run_phase(
                    job_dir, phase, job_id=job_id, mock=self.mock, request=lease["request"], uploads=uploads,
                    profiles_path=self.profiles, template=self.template, command=self.command,
                )
            if watchdog["killed"]:
                result = {**result, "reason": f"watchdog stopped {', '.join(watchdog['killed'])}"}
            from autoppt_service.leakguard import scan_outputs

            findings = scan_outputs(job_dir, self.secrets())
            if findings:
                # Upload nothing: the snapshot and deliverables would carry the leaked text.
                stop.set()
                self._post(f"/api/worker/jobs/{job_id}/fail",
                           {"lease_token": lease_token, "retryable": False,
                            "error": "security: agent output contains credential-like data in "
                                     + ", ".join(f"{f['file']} ({'/'.join(f['kinds'])})" for f in findings)[:900]},
                           **{"Idempotency-Key": f"fail:{lease_token}"})
                self._discard_local(job_dir)
                return {"job_id": job_id, "phase": phase, "failed": "leak_guard", "findings": findings}
            report = self._report(job_dir, job_id, phase, lease_token, result)
            self._purge_codex_traces(job_dir)
            if self.cleanup_local:
                # The backend holds the snapshot; keep at most one customer's data on this machine at a time.
                self._discard_local(job_dir)
        except (PhaseNotReady, PhaseError) as exc:
            stop.set()
            self._post(f"/api/worker/jobs/{job_id}/fail", {"lease_token": lease_token, "error": str(exc), "retryable": False},
                       **{"Idempotency-Key": f"fail:{lease_token}"})
            return {"job_id": job_id, "phase": phase, "failed": str(exc)}
        except Exception as exc:  # noqa: BLE001 - anything else is a retryable system failure
            stop.set()
            self._post(f"/api/worker/jobs/{job_id}/fail", {"lease_token": lease_token, "error": f"{type(exc).__name__}: {exc}",
                                                           "retryable": True}, **{"Idempotency-Key": f"fail:{lease_token}"})
            return {"job_id": job_id, "phase": phase, "failed": str(exc)}
        finally:
            stop.set()
        return {"job_id": job_id, "phase": phase, **report}

    @staticmethod
    def _discard_local(job_dir: Path) -> None:
        shutil.rmtree(job_dir, ignore_errors=True)
        shutil.rmtree(job_dir.parent / f"{job_dir.name}._uploads", ignore_errors=True)

    def _purge_codex_traces(self, job_dir: Path) -> None:
        """Delete this job's Codex rollouts and generated images after the phase is reported.

        Provenance was already verified from them and recorded (hashes) in the job directory.
        """
        from autoppt_service.codex_exec import find_rollout, load_profiles

        profiles = {profile.name: profile for profile in load_profiles(self.profiles)}
        for record in read_jsonl(service_path(job_dir, "usage.jsonl")):
            profile, thread = profiles.get(record.get("profile", "")), record.get("thread_id", "")
            if profile is None or not thread or thread == "mock":
                continue
            rollout = find_rollout(profile, thread)
            if rollout is not None:
                rollout.unlink(missing_ok=True)
            shutil.rmtree(profile.home() / "generated_images" / thread, ignore_errors=True)

    def _heartbeat(self, job_id: str, lease_token: str, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_s):
            status = None
            path = self.jobs_root / job_id / "service" / "status.json"
            if path.exists():
                try:
                    status = read_json(path)
                except ValueError:
                    status = None
            try:
                self._post(f"/api/worker/jobs/{job_id}/heartbeat", {"lease_token": lease_token, "progress": status})
            except WorkerClientError:
                return

    def _prepare(self, job_dir: Path, lease: dict[str, Any]) -> None:
        snapshot = lease.get("snapshot")
        marker = service_path(job_dir, ".snapshot-sha256")
        local_sha = marker.read_text().strip() if marker.exists() else ""
        if snapshot and snapshot["sha256"] != local_sha:
            # This machine is missing the job or has an older copy: start from the backend's latest snapshot.
            restore_snapshot(self._download(snapshot["url"]), job_dir)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(snapshot["sha256"])
        response = lease.get("checkpoint_response")
        checkpoint = read_json(service_path(job_dir, "checkpoint.json")) if job_dir.exists() else None
        response_path = service_path(job_dir, "checkpoint-response.json")
        if response and checkpoint and checkpoint.get("checkpoint_id") == response.get("checkpoint_id") and not response_path.exists():
            response_path.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")

    def _fetch_uploads(self, job_dir: Path, lease: dict[str, Any]) -> dict[str, str]:
        folder = job_dir.parent / f"{job_dir.name}._uploads"
        folder.mkdir(parents=True, exist_ok=True)
        uploads = {}
        for entry in lease["files"]:
            target = folder / f"{entry['file_id']}{PurePosixPath(entry['name']).suffix.lower()}"
            target.write_bytes(self._download(entry["url"]))
            uploads[entry["file_id"]] = str(target)
        return uploads

    def _new_records(self, path: Path, marker: Path) -> list[dict[str, Any]]:
        records = read_jsonl(path)
        seen = int(marker.read_text()) if marker.exists() else 0
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(len(records)))
        return records[seen:]

    def _report(self, job_dir: Path, job_id: str, phase: str, lease_token: str, result: dict[str, Any]) -> dict[str, Any]:
        state = load_job_state(job_dir)
        target = state["state"] if state else "blocked"
        checkpoint = None
        if target in CHECKPOINT_STATES:
            checkpoint = read_json(service_path(job_dir, "checkpoint.json"))
            for preview in checkpoint.get("previews", []):
                self._upload(job_id, lease_token, "preview", preview["path"], (job_dir / preview["path"]).read_bytes())
        if target == "delivered":
            manifest = read_json(job_dir / "output" / "delivery-manifest.json")
            for item in manifest["files"]:
                self._upload(job_id, lease_token, "deliverable", item["path"], (job_dir / item["path"]).read_bytes())
            for extra in ("output/delivery-manifest.json", *[str(p.relative_to(job_dir).as_posix()) for p in (job_dir / "output").glob("*.zip")]):
                self._upload(job_id, lease_token, "deliverable", extra, (job_dir / extra).read_bytes())
        uploaded = self._upload(job_id, lease_token, "snapshot", "snapshot.zip", snapshot_bytes(job_dir))
        service_path(job_dir, ".snapshot-sha256").write_text(uploaded["sha256"])
        snapshot_id = uploaded["artifact_id"]
        usage = self._new_records(service_path(job_dir, "usage.jsonl"), service_path(job_dir, ".reported-usage"))
        from autoppt_service.leakguard import sanitize_lesson

        request = read_json(service_path(job_dir, "job-request.json")) or {}
        terms = [*((request.get("cover") or {}).values()), *(f.get("name", "") for f in (request.get("inputs") or {}).get("files", []))]
        lessons = [sanitize_lesson(lesson, terms) for lesson in
                   self._new_records(service_path(job_dir, "lesson-candidates.jsonl"), service_path(job_dir, ".reported-lessons"))]
        report = {
            "lease_token": lease_token,
            "phase": phase,
            "job_state": target,
            "reason": result.get("reason", ""),
            "blocked_reason": (state or {}).get("blocked_reason", "") if target == "blocked" else "",
            "deferred": bool(result.get("deferred")),
            "retry_after": result.get("retry_after"),
            "checkpoint": checkpoint,
            "snapshot_artifact_id": snapshot_id,
            "template_version": self.template_version,
            "usage": usage,
            "lesson_candidates": lessons,
            "slides": self._slide_count(job_dir),
        }
        reply = self._post(f"/api/worker/jobs/{job_id}/complete", report, **{"Idempotency-Key": f"complete:{lease_token}"})
        return {"job_state": target, "backend": reply}

    @staticmethod
    def _slide_count(job_dir: Path) -> int | None:
        """Slides actually planned; the backend settles per-slide phases on this, not on the estimate's maximum."""
        brief = read_json(job_dir / "workspace" / "presentation_handoff" / "slide_brief.json")
        return len(brief.get("slides", [])) if isinstance(brief, dict) and brief.get("slides") else None

    def run_forever(self, *, idle_sleep_s: float = 10.0) -> None:
        while True:
            if self.run_once() is None:
                time.sleep(idle_sleep_s)


def main(argv: Sequence[str] | None = None) -> int:
    import httpx

    parser = argparse.ArgumentParser(description="Run the AutoPPT worker loop against the backend API.")
    parser.add_argument("--api", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--jobs-root", type=Path, required=True)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--template", type=Path)
    parser.add_argument("--profiles", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    version = ""
    if args.template:
        from autoppt_service.worker_template import load_template_manifest

        version = load_template_manifest(args.template)["version"]
    with httpx.Client(base_url=args.api, timeout=120) as http:
        worker = ApiWorker(http, args.token, args.jobs_root, mock=args.mock, template=args.template,
                           profiles=args.profiles, template_version=version)
        if args.once:
            result = worker.run_once()
            sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        else:
            worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
