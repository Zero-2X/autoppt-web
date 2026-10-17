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

SNAPSHOT_EXCLUDE = ("service/codex-scratch/", "service/.snapshot-sha256")
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
            if relative.startswith(SNAPSHOT_EXCLUDE) or "__pycache__" in relative:
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
    ) -> None:
        self.http = http  # httpx.Client or a FastAPI TestClient
        self.headers = {"Authorization": f"Bearer {token}"}
        self.jobs_root = Path(jobs_root)
        self.mock, self.template, self.profiles = mock, template, profiles
        self.template_version = template_version
        self.heartbeat_s = heartbeat_s
        self.command = command

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
            result = run_phase(
                job_dir, phase, job_id=job_id, mock=self.mock, request=lease["request"], uploads=uploads,
                profiles_path=self.profiles, template=self.template, command=self.command,
            )
            report = self._report(job_dir, job_id, phase, lease_token, result)
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
        lessons = self._new_records(service_path(job_dir, "lesson-candidates.jsonl"), service_path(job_dir, ".reported-lessons"))
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
        }
        reply = self._post(f"/api/worker/jobs/{job_id}/complete", report, **{"Idempotency-Key": f"complete:{lease_token}"})
        return {"job_state": target, "backend": reply}

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
