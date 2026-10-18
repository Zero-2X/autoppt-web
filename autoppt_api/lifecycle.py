"""Data lifecycle: one-click deletion and retention expiry.

Deleting a job removes its file links, artifacts and checkpoint payloads,
deletes blobs nobody else references, scrubs the stored request, and keeps
only what accounting and audit need (job id, owner, state ``deleted``, ledger
postings).  Every deletion writes an audit record.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from autoppt_api.models import Artifact, Checkpoint, Job, JobFile, StoredFile, UploadSession, now
from autoppt_api.services import audit, aware, event
from autoppt_api.storage import LocalStorage


def _blob_in_use(session: Session, sha256: str) -> bool:
    return any(
        session.scalars(query.limit(1)).first() is not None
        for query in (
            select(JobFile.id).where(JobFile.sha256 == sha256),
            select(Artifact.id).where(Artifact.sha256 == sha256),
            select(UploadSession.id).where(UploadSession.sha256 == sha256, UploadSession.status == "complete"),
        )
    )


def delete_job_data(session: Session, storage: LocalStorage, job: Job, *, actor_type: str, actor_id: str) -> int:
    hashes = {row.sha256 for row in session.scalars(select(JobFile).where(JobFile.job_id == job.id))}
    hashes |= {row.sha256 for row in session.scalars(select(Artifact).where(Artifact.job_id == job.id))}
    session.execute(delete(JobFile).where(JobFile.job_id == job.id))
    session.execute(delete(Artifact).where(Artifact.job_id == job.id))
    for checkpoint in session.scalars(select(Checkpoint).where(Checkpoint.job_id == job.id)):
        checkpoint.payload, checkpoint.response = {"deleted": True}, None
    # The user's own uploads for this job go too, unless another of their jobs still uses them.
    # Drafts and estimated orders name their files only in the request (JobFile rows appear at submit): found
    # 2026-10-19 by the soak test, deleting one job removed the upload under the same user's open draft.
    drafted = {str(entry.get("sha256") or "").lower()
               for other in session.scalars(select(Job).where(Job.user_id == job.user_id, Job.id != job.id,
                                                              Job.state.in_(("draft", "estimated"))))
               for entry in ((other.request or {}).get("inputs") or {}).get("files") or []}
    for upload in session.scalars(select(UploadSession).where(UploadSession.user_id == job.user_id, UploadSession.sha256.in_(hashes))):
        still_used = session.scalars(
            select(JobFile.id).join(Job, Job.id == JobFile.job_id).where(JobFile.sha256 == upload.sha256, Job.user_id == job.user_id,
                                                                         Job.id != job.id)
        ).first()
        if still_used is None and upload.sha256 not in drafted:
            upload.status = "deleted"
    session.flush()
    removed = 0
    for sha in hashes:
        if not _blob_in_use(session, sha):
            stored = session.get(StoredFile, sha)
            if stored is not None:
                storage.delete(stored.storage_key)
                session.delete(stored)
                removed += 1
    job.request, job.normalized, job.estimate = {}, None, None
    job.title, job.blocked_reason, job.snapshot_artifact_id = "", "", None
    event(session, job, "deleted", "job data deleted")
    job.state, job.pending_phase = "deleted", None
    audit(session, actor_type, actor_id, "job.delete", job.id, removed_blobs=removed)
    return removed


def purge_intermediate(session: Session, storage: LocalStorage, *, days: int = 7) -> int:
    """Drop snapshots and previews of finished jobs after ``days``; deliverables follow the retention period."""
    cutoff = now() - timedelta(days=days)
    removed = 0
    finished = select(Job.id).where(Job.finished_at.is_not(None), Job.finished_at <= cutoff)
    for artifact in session.scalars(select(Artifact).where(Artifact.job_id.in_(finished), Artifact.kind.in_(("snapshot", "preview")))).all():
        sha = artifact.sha256
        session.delete(artifact)
        session.flush()
        if not _blob_in_use(session, sha):
            stored = session.get(StoredFile, sha)
            if stored is not None:
                storage.delete(stored.storage_key)
                session.delete(stored)
        removed += 1
    for job in session.scalars(select(Job).where(Job.id.in_(finished), Job.snapshot_artifact_id.is_not(None))):
        job.snapshot_artifact_id = None
    return removed


def purge_expired(session: Session, storage: LocalStorage) -> list[str]:
    """Delete finished jobs whose retention period (3/7/30 days after finishing) has passed."""
    current = now()
    purged = []
    for job in session.scalars(select(Job).where(Job.finished_at.is_not(None), Job.state.in_(("delivered", "failed", "cancelled")))):
        if aware(job.finished_at) + timedelta(days=job.retention_days) <= current:
            delete_job_data(session, storage, job, actor_type="system", actor_id="retention")
            purged.append(job.id)
    return purged
