"""Shared helpers for tests that build jobs directly through the service layer."""

from __future__ import annotations

import hashlib

from autoppt_api.models import StoredFile, UploadSession

DESCRIPTION = "本研究面向城市内涝预警，目标是在暴雨前两小时给出街区级风险等级，并说明本人负责的模型部分。" * 6


def job_request_for(session, user, *, tier: str = "native_editable") -> dict:
    """A valid request whose only file is a completed upload owned by ``user`` (no bytes needed)."""
    sha = hashlib.sha256(f"{user.id}-thesis".encode()).hexdigest()
    if session.get(StoredFile, sha) is None:
        session.add(StoredFile(sha256=sha, size=1234, storage_key=f"blobs/{sha[:2]}/{sha}", sniffed_kind="docx"))
        session.add(UploadSession(user_id=user.id, filename="论文.docx", expected_size=1234, received=1234,
                                  status="complete", sha256=sha))
        session.flush()
    delivery = {"tier": tier, "font_policy": "office_native_yahei"} if tier == "native_editable" else {"tier": tier}
    return {
        "schema_version": "autoppt-job-request-v1",
        "service": "new_deck",
        "inputs": {"files": [{"file_id": "f_01", "role": "primary_source", "name": "论文.docx", "sha256": sha}],
                   "description": DESCRIPTION},
        "scenario": "thesis_defense",
        "slides": {"mode": "fixed", "count": 12},
        "cover": {"title": "测试"},
        "delivery": delivery,
    }
