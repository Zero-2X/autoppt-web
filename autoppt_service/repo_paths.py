"""Locate the AutoPPT workflow repository, which this service uses read-only.

The workflow and skills live in their own repository and are never modified
from here.  ``AUTOPPT_REPO`` points at it; the default is a sibling checkout
named ``autoppt`` next to this repository.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def autoppt_repo() -> Path:
    configured = os.getenv("AUTOPPT_REPO", "").strip()
    repo = Path(configured) if configured else SERVICE_ROOT.parent / "autoppt"
    if not (repo / "autoppt_workflow").is_dir() or not (repo / "autopptskills").is_dir():
        raise FileNotFoundError(f"AutoPPT repository not found at {repo}; set AUTOPPT_REPO")
    return repo.resolve()


def ensure_autoppt_on_path() -> Path:
    repo = autoppt_repo()
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    return repo
