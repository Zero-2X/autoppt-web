"""A 20-second soak: concurrent users, three mock workers, a live server; the ledger must balance.

Regression guard for the 2026-10-18 finding: async endpoints waited for SQLite's write lock on the
event loop, which froze the server for 30 s and failed requests with "database is locked".
The long run is ``python scripts/stress_test.py --minutes 60``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_short_soak_keeps_the_ledger_and_queue_healthy(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import stress_test
    finally:
        sys.path.pop(0)
    out = tmp_path / "report.json"
    code = stress_test.main(["--minutes", "0.35", "--users", "4", "--workers", "3", "--check-every", "7",
                             "--drain-minutes", "3", "--work-dir", str(tmp_path / "work"), "--out", str(out)])
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["errors"] == [] and code == 0, report["errors"]
    assert report["final_ledger_ok"] and report["ledger_ok_every_check"]
    assert report["counts"].get("delivered", 0) >= 5 and report["jobs_not_final"] == []
