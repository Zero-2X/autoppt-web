"""Make the service package and the read-only AutoPPT workflow importable."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autoppt_service.repo_paths import ensure_autoppt_on_path  # noqa: E402

ensure_autoppt_on_path()
