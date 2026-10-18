"""Intake gaps closed 2026-10-18: OCR for scanned PDF pages, ruled PDF tables, HEIC photos."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from autoppt_service.intake import run_intake
from autoppt_service.jobfiles import read_json
from autoppt_service.parsers import build_source_manifest
from autoppt_service.parsers.ocr_pages import ocr_queued_pages, pending_ocr_pages
from test_intake_and_run_job import make_request
from test_intake_parsers import by_id, entry, make_scanned_pdf, make_text_pdf


def write_pdf(path: Path, streams: list[bytes]) -> Path:
    """Minimal PDF, one content stream per page (Helvetica as /F1)."""
    objects: dict[int, bytes] = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
                                 3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids = []
    for index, stream in enumerate(streams):
        page_id, content_id = 4 + 2 * index, 5 + 2 * index
        kids.append(f"{page_id} 0 R")
        objects[page_id] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>").encode()
        objects[content_id] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(streams)} >>".encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {max(objects) + 1}\n0000000000 65535 f \n".encode()
    for number in range(1, max(objects) + 1):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {max(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path


def ruled_table(rows: list[list[str]], *, x0=72, y0=700, cell_w=120, cell_h=24) -> bytes:
    """Grid lines plus one text run per cell, like a table exported from Word."""
    ops = ["0.5 w"]
    for r in range(len(rows) + 1):
        y = y0 - r * cell_h
        ops.append(f"{x0} {y} m {x0 + cell_w * len(rows[0])} {y} l S")
    for c in range(len(rows[0]) + 1):
        x = x0 + c * cell_w
        ops.append(f"{x} {y0} m {x} {y0 - cell_h * len(rows)} l S")
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            ops.append(f"BT /F1 10 Tf {x0 + c * cell_w + 6} {y0 - (r + 1) * cell_h + 8} Td ({text}) Tj ET")
    return "\n".join(ops).encode("latin-1")


def test_ruled_pdf_table_becomes_chart_data(tmp_path):
    rows = [["Method", "Hit rate", "Lead (min)"], ["District alert", "0.61", "18"], ["Hydraulic", "0.79", "12"],
            ["Ours", "0.82", "47"]]
    prose = b"BT /F1 12 Tf 72 400 Td (Results of the replay experiment are listed in the table above.) Tj ET"
    pdf = write_pdf(tmp_path / "paper.pdf", [ruled_table(rows) + b"\n" + prose])
    manifest = build_source_manifest([entry("f_01", "primary_source", pdf)], tmp_path / "work")
    assert by_id(manifest, "f_01")["stats"]["tables"] == 1
    table = manifest["tables"][0]
    assert table["table_id"] == "f_01_p001-t01" and table["location"] == "page 1 table 1"
    assert table["header"] == ["Method", "Hit rate", "Lead (min)"]
    assert table["rows"][2] == ["Ours", "0.82", "47"]
    assert table["numeric_columns"] == [1, 2] and table["chart_candidate"] is True
    item = next(i for i in manifest["evidence_items"] if i["item_type"] == "table")
    assert item["evidence_id"] == "ev_f_01_p001-t01" and item["page"] == 1


def test_prose_pdf_has_no_table(tmp_path):
    pdf = make_text_pdf(tmp_path / "paper.pdf", ["Research question and method overview for flood alerts"])
    manifest = build_source_manifest([entry("f_01", "primary_source", pdf)], tmp_path / "work")
    assert manifest["tables"] == [] and by_id(manifest, "f_01")["stats"]["tables"] == 0


def test_scanned_pages_are_read_and_marked_as_ocr_evidence(tmp_path):
    scan = make_scanned_pdf(tmp_path / "scan.pdf")
    mixed = make_text_pdf(tmp_path / "paper.pdf", ["Research question and method overview for flood alerts", ""])
    manifest = build_source_manifest([entry("f_01", "primary_source", mixed), entry("f_02", "supplementary", scan)],
                                     tmp_path / "work")
    seen = []

    def ocr(image, out_json):
        seen.append(Path(image))
        with Image.open(image) as page:
            assert abs(page.width - 1800) <= 2  # pdfium rounds the scale
        if "f_02" in str(image):
            return {"lines": [{"text": "第 三 章 实 验 结 果"}, {"text": "命 中 率 为 0 · 82 ， 误 报 率 为 0 · 1 7"}]}
        return {"lines": []}

    counts = ocr_queued_pages(manifest, tmp_path / "work" / "ocr", ocr=ocr)
    assert counts == {"ocr_done": 1, "ocr_empty": 1, "ocr_failed": 0, "skipped": 0}
    assert len(seen) == 2
    item = next(i for i in manifest["evidence_items"] if i.get("extraction") == "ocr")
    assert item["evidence_id"] == "ev_f_02_p001-ocr"
    assert item["fact_or_data"] == "第三章实验结果\n命中率为 0.82，误报率为 0.17"
    assert item["risk_level"] == "medium" and "未与原件核对" in item["risk_notes"]
    assert [(e["file_id"], e["page"]) for e in pending_ocr_pages(manifest)] == [("f_01", 2)]


def test_ocr_failure_leaves_the_page_pending(tmp_path):
    manifest = build_source_manifest([entry("f_01", "primary_source", make_scanned_pdf(tmp_path / "scan.pdf"))],
                                     tmp_path / "work")

    def ocr(image, out_json):
        raise RuntimeError("Windows OCR language pack missing")

    assert ocr_queued_pages(manifest, tmp_path / "ocr", ocr=ocr)["ocr_failed"] == 1
    assert manifest["ocr_queue"][0]["status"] == "ocr_failed"
    assert "language pack" in manifest["ocr_queue"][0]["error"]


def test_intake_runs_ocr_and_tells_planning_which_pages_came_from_ocr(tmp_path):
    scan = make_scanned_pdf(tmp_path / "scan.pdf")
    files = [{"file_id": "f_01", "role": "primary_source", "name": "扫描论文.pdf"}]

    def ocr(image, out_json):
        return {"lines": [{"text": "摘 要 ： 平 均 提 前 47 分 钟"}]}

    job = tmp_path / "job"
    report = run_intake(make_request(files=files), {"f_01": str(scan)}, job, job_id="J1", ocr=ocr)
    assert report["job_state"] == "planning"
    assert any("文字来自 OCR" in w for w in report["warnings"])
    assert not any("没有文字层" in w for w in report["warnings"])
    ledger = read_json(job / "workspace/evidence_workspace/evidence-ledger.json")
    assert any(i.get("extraction") == "ocr" and "47" in i["fact_or_data"] for i in ledger["evidence_items"])
    brief = read_json(job / "workspace/presentation_handoff/planning-brief.json")
    assert brief["evidence"]["ocr_pending_pages"] == []
    assert brief["evidence"]["ocr_read_pages"] == [{"file_id": "f_01", "page": 1}]

    # Without an OCR runner (mock runs, non-Windows), the page stays pending as before.
    job2 = tmp_path / "job2"
    report = run_intake(make_request(files=files), {"f_01": str(scan)}, job2, job_id="J2")
    assert any("需要 OCR 后才能作为证据" in w for w in report["warnings"])


def test_heic_photo_is_transcoded_to_png(tmp_path):
    pillow_heif = pytest.importorskip("pillow_heif")
    heic = tmp_path / "photo.heic"
    pillow_heif.from_pillow(Image.new("RGB", (64, 48), "teal")).save(heic, quality=80)
    manifest = build_source_manifest([entry("f_01", "keep_as_image", heic)], tmp_path / "work")
    assert by_id(manifest, "f_01")["status"] == "parsed"
    asset = manifest["assets"][0]
    assert asset["format"] == "PNG" and asset["path"].endswith("image.png")
    assert "transcoded to PNG" in asset["origin"]
    with Image.open(asset["path"]) as image:
        assert image.format == "PNG" and image.size == (64, 48)
