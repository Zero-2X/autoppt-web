"""Vectorization intake: user files -> frozen masters -> OCR -> CP-V text review.

The masters of a vectorization job are the user's own pages, not ImageGen
output, so they are recorded as ``user_supplied_master`` with the upload hash
and page number (decision D3).  The workflow repository's ImageGen-first
gates do not accept them and are not changed from here; the service keeps its
own provenance manifest and the tier stays a disclosed, user-supplied-master
reconstruction.

* PPTX pages are rendered by Microsoft PowerPoint through the workflow's
  ``render_pptx_powerpoint.ps1`` (read-only use); one Office job per machine.
* PDF pages are rasterised with pypdfium2 when installed, else PyMuPDF
  (AGPL: check the licence before commercial use; the report records which).
* Images are normalised to RGB PNG.
* Windows OCR (``windows_ocr_layout.ps1``) proposes lines with boxes.  At CP-V
  the user corrects them; the reviewed text becomes the exact-text manifest,
  and OCR never overrides reviewed text.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from PIL import Image, ImageOps

from autoppt_service.jobfiles import EVIDENCE_DIR, read_json, relative_to_job, sha256_file, utc_now, write_json_atomic
from autoppt_service.repo_paths import ensure_autoppt_on_path

MASTERS_DIR = "assets/slides"
WORK_DIR = "workspace/vectorize"
PROVENANCE = "user_supplied_master"
MASTER_WIDTH = 1920
DEFAULT_OFFICE_LOCK = Path(os.getenv("AUTOPPT_OFFICE_LOCK", str(Path.home() / ".autoppt-office.lock")))

OfficeRenderer = Callable[[Path, Path], list[Path]]  # (pptx, out_dir) -> slide PNGs in order
OcrRunner = Callable[[Path, Path], dict[str, Any]]   # (image, out_json) -> OCR payload


class VectorizeError(RuntimeError):
    pass


# ---------------------------------------------------------------- Office lock


@contextmanager
def office_lock(path: Path = DEFAULT_OFFICE_LOCK, *, timeout_s: float = 3600.0, poll_s: float = 2.0) -> Iterator[None]:
    """One Office automation job per machine (PowerPoint is single-instance per session)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {utc_now()}".encode())
            os.close(fd)
            break
        except FileExistsError:
            if _stale_lock(path):
                path.unlink(missing_ok=True)
                continue
            if time.monotonic() > deadline:
                raise VectorizeError(f"Office is busy (lock {path})")
            time.sleep(poll_s)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def _stale_lock(path: Path) -> bool:
    """A lock whose owner process is gone (crash) may be taken over."""
    try:
        pid = int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return True
    if sys.platform == "win32":
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False)
        return str(pid) not in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


# ---------------------------------------------------------------- real runners (Windows)


def _script(name: str) -> Path:
    return ensure_autoppt_on_path() / "autopptskills" / "scripts" / name


def powerpoint_renderer(pptx: Path, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    with office_lock():
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(_script("render_pptx_powerpoint.ps1")),
             "-InputPptx", str(pptx), "-OutputDirectory", str(out_dir), "-Width", str(MASTER_WIDTH), "-Height", "1080"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=1800,
        )
    if completed.returncode != 0:
        raise VectorizeError(f"PowerPoint render failed: {completed.stderr.strip()[-800:]}")
    return sorted(out_dir.glob("slide-*.png"), key=lambda p: int(re.findall(r"\d+", p.stem)[-1]))


def windows_ocr(image: Path, out_json: Path, *, language: str = "zh-Hans-CN") -> dict[str, Any]:
    out_json.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(_script("windows_ocr_layout.ps1")),
         "-InputPath", str(image), "-OutputJson", str(out_json), "-LanguageTag", language, "-Quiet"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=600,
    )
    if completed.returncode != 0:
        raise VectorizeError(f"Windows OCR failed: {completed.stderr.strip()[-800:]}")
    return json.loads(out_json.read_text(encoding="utf-8-sig"))


def rasterize_pdf(pdf: Path, out_dir: Path, *, width: int = MASTER_WIDTH, pages: Sequence[int] | None = None) -> tuple[list[Path], str]:
    """Pages as PNG; returns (paths, renderer name)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []
    try:
        import pypdfium2 as pdfium  # Apache-2.0 / BSD

        document = pdfium.PdfDocument(str(pdf))
        numbers = pages or range(1, len(document) + 1)
        for number in numbers:
            page = document[number - 1]
            scale = width / page.get_width()
            target = out_dir / f"page-{number:03d}.png"
            page.render(scale=scale).to_pil().convert("RGB").save(target)
            results.append(target)
        return results, "pypdfium2"
    except ImportError:
        pass
    try:
        import fitz  # PyMuPDF, AGPL-3.0
    except ImportError as exc:
        raise VectorizeError("no PDF renderer: install pypdfium2") from exc
    document = fitz.open(str(pdf))
    numbers = pages or range(1, document.page_count + 1)
    for number in numbers:
        page = document[number - 1]
        zoom = width / page.rect.width
        target = out_dir / f"page-{number:03d}.png"
        page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).save(str(target))
        results.append(target)
    return results, "pymupdf (AGPL-3.0)"


# ---------------------------------------------------------------- normalisation


def parse_pages(spec: str | None, total: int) -> list[int]:
    """'1-3,5' -> [1, 2, 3, 5] within 1..total; empty means all pages."""
    if not spec or not spec.strip():
        return list(range(1, total + 1))
    pages: list[int] = []
    for part in spec.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = (int(x) for x in part.split("-", 1))
            pages.extend(range(start, end + 1))
        else:
            pages.append(int(part))
    selected = sorted({p for p in pages if 1 <= p <= total})
    if not selected:
        raise VectorizeError(f"page selection {spec!r} selects nothing from {total} pages")
    return selected


def _normalize_image(source: Path, target: Path) -> tuple[int, int]:
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        if image.width > MASTER_WIDTH:
            image = image.resize((MASTER_WIDTH, round(image.height * MASTER_WIDTH / image.width)), Image.Resampling.LANCZOS)
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target, "PNG")
        return image.size


def prepare_masters(
    job_dir: Path,
    *,
    renderer: OfficeRenderer = powerpoint_renderer,
    pdf_rasterizer: Callable[..., tuple[list[Path], str]] = rasterize_pdf,
    max_slides: int | None = None,
    page_spec: str | None = None,
) -> dict[str, Any]:
    """Turn every vectorize_source upload into frozen masters ``assets/slides/Sxx.png``."""
    job_dir = Path(job_dir)
    manifest = read_json(job_dir / EVIDENCE_DIR / "source-manifest.json") or {}
    sources = [r for r in manifest.get("files", []) if r["role"] == "vectorize_source" and r["status"] != "rejected"
               and r.get("kind") in ("pptx", "pdf", "image")]
    if not sources:
        raise VectorizeError("no usable vectorize_source file")
    pages: list[dict[str, Any]] = []
    renderers: set[str] = set()
    work = job_dir / WORK_DIR / "pages"
    for record in sources:
        path = job_dir / record["path"]
        kind = record["kind"]
        if kind == "pptx":
            rendered = renderer(path, work / record["file_id"])
            renderers.add("powerpoint")
            items = [(index, image) for index, image in enumerate(rendered, 1)]
        elif kind == "pdf":
            rendered, name = pdf_rasterizer(path, work / record["file_id"])
            renderers.add(name)
            items = [(int(re.findall(r"\d+", image.stem)[-1]), image) for image in rendered]
        else:
            items = [(1, path)]
            renderers.add("pillow")
        for page_number, image in items:
            pages.append({"file_id": record["file_id"], "source_file": record["name"], "source_sha256": record.get("sha256"),
                          "page": page_number, "image": image})
    selected = parse_pages(page_spec, len(pages))
    if max_slides is not None and len(selected) > max_slides:
        raise VectorizeError(f"{len(selected)} pages selected but the order covers {max_slides}; adjust the page selection")
    slides = []
    for index, position in enumerate(selected, 1):
        page = pages[position - 1]
        slide_id = f"S{index:02d}"
        target = job_dir / MASTERS_DIR / f"{slide_id}.png"
        width, height = _normalize_image(page["image"], target)
        slides.append({
            "slide_id": slide_id, "path": relative_to_job(job_dir, target), "sha256": sha256_file(target),
            "width": width, "height": height, "provenance": PROVENANCE,
            "source": {k: page[k] for k in ("file_id", "source_file", "source_sha256", "page")},
        })
    payload = {
        "schema_version": "autoppt-user-supplied-masters-v1",
        "generated_at": utc_now(),
        "provenance": PROVENANCE,
        "note": "User-supplied pages, not ImageGen output; the workflow's ImageGen-first gates do not apply to them.",
        "renderers": sorted(renderers),
        "slides": slides,
    }
    write_json_atomic(job_dir / MASTERS_DIR / "user-supplied-masters.json", payload)
    return payload


# ---------------------------------------------------------------- OCR and CP-V


_CJK = "　-〿㐀-䶿一-鿿豈-﫿＀-￯"  # CJK, CJK punctuation, full-width forms
_CJK_GAP = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")


def join_cjk(text: str) -> str:
    """Windows OCR returns one word per CJK character joined by spaces; drop spaces between CJK characters only."""
    return _CJK_GAP.sub("", " ".join(str(text).split()))


# Windows OCR sometimes reads one character as its two components (measured: 涝 -> 氵劳, 外 -> 夕卜).
# Known pairs become suggestions at CP-V; a lone radical marks the line for attention.  Nothing is changed
# automatically: the reviewed text is the user's.
OCR_SPLITS = {"氵劳": "涝", "夕卜": "外", "亻尔": "你", "讠吾": "语", "扌是": "提", "钅失": "铁", "纟充": "统"}
_LONE_RADICALS = set("氵亻扌讠钅纟彳忄阝冫冖宀犭礻衤饣")


def ocr_suggestion(text: str) -> tuple[str | None, bool]:
    """``(suggested_text, suspect)`` for a recognised line."""
    suggested = text
    for split, joined in OCR_SPLITS.items():
        suggested = suggested.replace(split, joined)
    suspect = suggested != text or any(ch in _LONE_RADICALS for ch in suggested)
    return (suggested if suggested != text else None), suspect


def run_ocr(job_dir: Path, masters: dict[str, Any], *, ocr: OcrRunner = windows_ocr) -> dict[str, Any]:
    """OCR every master; write the editable text-review document for CP-V."""
    job_dir = Path(job_dir)
    review = {"schema_version": "autoppt-text-review-v1", "generated_at": utc_now(), "slides": []}
    for slide in masters["slides"]:
        result = ocr(job_dir / slide["path"], job_dir / WORK_DIR / "ocr" / f"{slide['slide_id']}.json")
        width, height = result.get("width") or slide["width"], result.get("height") or slide["height"]
        lines = [
            {
                "line_id": line.get("id") or f"line-{index:03d}",
                "ocr_text": join_cjk(line["text"]),
                "text": join_cjk(line["text"]),
                "bbox": [round(line["bbox"]["x"] / width, 5), round(line["bbox"]["y"] / height, 5),
                         round(line["bbox"]["w"] / width, 5), round(line["bbox"]["h"] / height, 5)],
            }
            for index, line in enumerate(result.get("lines", []))
            if str(line.get("text", "")).strip()
        ]
        for line in lines:
            suggestion, suspect = ocr_suggestion(line["text"])
            if suspect:
                line["suspect"] = True
                if suggestion:
                    line["suggestion"] = suggestion
        review["slides"].append({"slide_id": slide["slide_id"], "master": slide["path"], "lines": lines})
    write_json_atomic(job_dir / WORK_DIR / "text-review.json", review)
    return review


def checkpoint_payload(job_dir: Path, masters: dict[str, Any], *, round_no: int) -> dict[str, Any]:
    review_path = Path(job_dir) / WORK_DIR / "text-review.json"
    return {
        "checkpoint_id": f"cpv-text-r{round_no}",
        "type": "vectorize_text",
        "round": round_no,
        "summary": f"{len(masters['slides'])} pages; check the recognised text before reconstruction",
        "previews": [{"path": s["path"], "kind": "slide", "slide_id": s["slide_id"]} for s in masters["slides"]]
        + [{"path": relative_to_job(job_dir, review_path), "kind": "document"}],
        "questions": [{"question_id": "text_corrections", "text": "请逐页核对识别出的文字，改正错误、删除不应保留的行。", "kind": "text"}],
        "master_sha256": {s["slide_id"]: s["sha256"] for s in masters["slides"]},
    }


def apply_text_review(job_dir: Path, response: dict[str, Any]) -> dict[str, Any]:
    """Merge the user's corrections into the reviewed exact-text manifest (reviewed text wins over OCR).

    ``response["answers"]["text_corrections"]`` = ``{slide_id: {line_id: new_text}}``;
    an empty string removes the line.  ``answers["added_lines"]`` = ``{slide_id: [text, ...]}``.
    """
    job_dir = Path(job_dir)
    review = read_json(job_dir / WORK_DIR / "text-review.json")
    answers = response.get("answers") or {}
    corrections = answers.get("text_corrections") or {}
    added = answers.get("added_lines") or {}
    known = {slide["slide_id"]: {line["line_id"] for line in slide["lines"]} for slide in review["slides"]}
    for slide_id, changes in corrections.items():
        if slide_id not in known or set(changes) - known[slide_id]:
            raise VectorizeError(f"corrections refer to unknown slides or lines: {slide_id}")
    slides = []
    for slide in review["slides"]:
        changes = corrections.get(slide["slide_id"], {})
        lines = []
        for line in slide["lines"]:
            text = changes.get(line["line_id"], line["text"])
            if str(text).strip():
                lines.append({**line, "text": str(text), "reviewed": True, "edited": text != line["ocr_text"]})
        for text in added.get(slide["slide_id"], []):
            if str(text).strip():
                lines.append({"line_id": f"user-{len(lines):03d}", "ocr_text": "", "text": str(text), "bbox": None,
                              "reviewed": True, "edited": True})
        slides.append({"slide_id": slide["slide_id"], "master": slide["master"], "lines": lines})
    manifest = {
        "schema_version": "autoppt-reviewed-text-v1",
        "generated_at": utc_now(),
        "source": "CP-V user review; OCR may propose geometry but never overrides this text",
        "user_reply": response.get("user_reply", ""),
        "slides": slides,
        "exact_text": [line["text"] for slide in slides for line in slide["lines"]],
    }
    write_json_atomic(job_dir / WORK_DIR / "reviewed-text.json", manifest)
    return manifest
