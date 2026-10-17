"""Number check on ImageGen masters before CP2.

ImageGen sometimes adds figures that are not in the plan (measured 2026-10-17:
S09 showed "提升 2.6 倍", derived from 47/18, despite the "no unsupported
number" rule).  Each master is read with OCR; every significant number that is
neither in that slide's exact text nor anywhere in the evidence is reported, so
the reviewer sees it in the CP2 summary instead of having to spot it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from autoppt_service.jobfiles import EVIDENCE_DIR, HANDOFF_DIR, read_json, write_json_atomic
from autoppt_service.planning import _numbers, _significant

OcrRunner = Callable[[Path, Path], dict[str, Any]]
_FULLWIDTH = str.maketrans("０１２３４５６７８９．，", "0123456789.,")
_AXIS_TOKENS = re.compile(r"km|m|%|分钟|分|秒|[\s\d.,·•一—–\-~()（）〈〉<>]")
_YEAR_RANGE = re.compile(r"((?:19|20)\d{2})\s*[—–\-~至到]+\s*((?:19|20)\d{2})")
_SPLIT_DECIMAL = re.compile(r"(\d)\s*[·•．。]\s*(\d)")
_SPLIT_GROUP = re.compile(r"(\d)\s*([.,])\s*(\d)")


def normalize_numbers(text: str) -> str:
    """Undo OCR splits inside numbers: '2 · 6' -> '2.6', '1, 286' -> '1,286', full-width digits."""
    text = text.translate(_FULLWIDTH)
    text = _SPLIT_DECIMAL.sub(lambda m: f"{m.group(1)}.{m.group(2)}", text)
    return _SPLIT_GROUP.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}", text)


def is_axis_line(text: str) -> bool:
    """Tick labels, legend ranges and scale bars: nothing but numbers, ranges and units."""
    return any(c.isdigit() for c in text) and not _AXIS_TOKENS.sub("", text)


def _with_year_ranges(text: str) -> set[str]:
    numbers = _numbers(text)
    for start, end in _YEAR_RANGE.findall(text):
        numbers |= {str(year) for year in range(int(start), int(end) + 1)}
    return numbers


def check_master_numbers(job_dir: Path, masters: dict[str, Path], *, ocr: OcrRunner) -> list[str]:
    from autoppt_service.vectorize import join_cjk

    job_dir = Path(job_dir)
    plan = read_json(job_dir / HANDOFF_DIR / "deck-plan.json") or {}
    ledger = read_json(job_dir / EVIDENCE_DIR / "evidence-ledger.json") or {}
    brief = read_json(job_dir / HANDOFF_DIR / "planning-brief.json") or {}
    per_slide = {s["slide_id"]: _numbers(" ".join(s.get("exact_text", []))) for s in plan.get("slides", [])}
    evidence = _with_year_ranges(" ".join(str(i.get("fact_or_data", "")) for i in ledger.get("evidence_items", [])))
    cover = _numbers(" ".join(e["text"] for e in brief.get("exact_text_whitelist", [])))
    findings: list[str] = []
    report = []
    for slide_id, path in sorted(masters.items()):
        result = ocr(Path(path), job_dir / "workspace" / "master-qa" / f"{slide_id}.json")
        lines = [normalize_numbers(join_cjk(line.get("text", ""))) for line in result.get("lines", [])]
        text = " ".join(line for line in lines if not is_axis_line(line))
        seen = {n for n in _numbers(text) if _significant(n)}
        unexpected = sorted(seen - per_slide.get(slide_id, set()) - evidence - cover)
        report.append({"slide_id": slide_id, "numbers": sorted(seen), "unexpected": unexpected})
        if unexpected:
            findings.append(f"{slide_id}: master shows numbers not in the plan or evidence: {', '.join(unexpected)}")
    write_json_atomic(job_dir / "workspace" / "master-qa" / "numbers.json", {"slides": report, "findings": findings})
    return findings
