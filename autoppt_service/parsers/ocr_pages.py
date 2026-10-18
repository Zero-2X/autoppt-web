"""OCR for PDF pages without a usable text layer (scanned theses, photographed pages).

The parsers only queue such pages.  This step renders each queued page and reads
it with the Worker's OCR (Windows OCR on the Office machines); the text becomes an
evidence item marked ``extraction: ocr`` with a higher risk level, because OCR
misreads characters and digits and nobody has checked it against the page yet.
Pages that cannot be read stay pending and are reported in the brief.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from autoppt_service.parsers.base import IntakeLimits, SourceFile, text_items

OcrRunner = Callable[[Path, Path], dict[str, Any]]
RENDER_WIDTH = 1800  # about 150 dpi for A4: Windows OCR reads 5-pt captions at this size
OCR_RISK_NOTES = "OCR 识别的扫描页文字，未与原件核对；数字和专有名词使用前须人工确认"
_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_SPLIT_DECIMAL = re.compile(r"(\d)\s*[·•．.]\s*(\d)")
_DECIMAL_TAIL = re.compile(r"(\d\.\d+) (?=\d)")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+(?=[，。；：、！？）》”])")


def _ocr_line(text: str) -> str:
    """Windows OCR splits numbers (measured: '0 · 1 7' for 0.17); join them so they match the evidence check.

    Chinese punctuation is kept as it is (``master_qa.normalize_numbers`` maps '，' to ',')."""
    from autoppt_service.vectorize import join_cjk

    text = _SPLIT_DECIMAL.sub(r"\1.\2", join_cjk(text).translate(_DIGITS)).strip()
    while _DECIMAL_TAIL.search(text):
        text = _DECIMAL_TAIL.sub(r"\1", text)
    return _SPACE_BEFORE_PUNCT.sub("", text)


def ocr_queued_pages(manifest: dict[str, Any], work_dir: Path, *, ocr: OcrRunner,
                     limits: IntakeLimits | None = None, max_pages: int = 100) -> dict[str, int]:
    """Read every queued page; add OCR text items to ``manifest`` and mark each queue entry.

    Entry ``status``: ``ocr_done`` (text added), ``ocr_empty`` (nothing read), ``ocr_failed`` (with
    ``error``) or ``skipped`` (over ``max_pages``).  Returns the counts."""
    from autoppt_service.vectorize import rasterize_pdf

    limits = limits or IntakeLimits()
    files = {record["file_id"]: record for record in manifest["files"]}
    counts = {"ocr_done": 0, "ocr_empty": 0, "ocr_failed": 0, "skipped": 0}
    items: list[dict[str, Any]] = []
    for index, entry in enumerate(manifest["ocr_queue"]):
        if index >= max_pages:
            entry["status"] = "skipped"
            counts["skipped"] += 1
            continue
        record = files[entry["file_id"]]
        source = SourceFile(file_id=record["file_id"], role=record["role"], name=record["name"],
                            path=Path(record["path"]), origin=record.get("archive_member") or "")
        page_dir = Path(work_dir) / record["file_id"]
        try:
            images, _ = rasterize_pdf(source.path, page_dir, width=RENDER_WIDTH, pages=[entry["page"]])
            result = ocr(images[0], page_dir / f"page-{entry['page']:03d}.json")
        except Exception as exc:  # noqa: BLE001 - an unreadable page stays pending, intake goes on
            entry.update(status="ocr_failed", error=str(exc)[:500])
            counts["ocr_failed"] += 1
            continue
        text = "\n".join(line for line in (_ocr_line(raw.get("text", "")) for raw in result.get("lines", [])) if line)
        if not text:
            entry["status"] = "ocr_empty"
            counts["ocr_empty"] += 1
            continue
        entry.update(status="ocr_done", ocr_chars=len(text))
        counts["ocr_done"] += 1
        items += text_items(
            source, f"p{entry['page']:03d}-ocr", text, limits,
            title=f"{source.name} 第 {entry['page']} 页（OCR）", location=f"page {entry['page']} (OCR)",
            page=entry["page"],
            extra={"extraction": "ocr", "reliability": "ocr_uploaded_source", "risk_level": "medium",
                   "risk_notes": OCR_RISK_NOTES},
        )
    manifest["evidence_items"].extend(items)
    manifest["summary"]["evidence_items"] = len(manifest["evidence_items"])
    manifest["summary"]["ocr_done_pages"] = counts["ocr_done"]
    return counts


def pending_ocr_pages(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in manifest["ocr_queue"] if entry.get("status") != "ocr_done"]
