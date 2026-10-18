"""PDF: text layer per page, ruled tables, embedded images, and OCR flags for scanned pages.

OCR itself is not run here; pages whose text layer is (nearly) empty are put
on the manifest's ``ocr_queue`` for the OCR step (``parsers.ocr_pages``).
Tables are read with pdfplumber's ruling-line detection only, so body text laid
out in columns is not mistaken for a table.
"""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from autoppt_service.parsers.base import (
    IntakeLimits,
    ParsedSource,
    SourceFile,
    SourceRejected,
    image_asset,
    table_item,
    table_record,
    text_items,
)


def _page_has_images(page) -> bool:
    try:
        resources = page.get("/Resources") or {}
        xobjects = resources.get("/XObject") or {}
        return any(xobjects[name].get_object().get("/Subtype") == "/Image" for name in xobjects)
    except Exception:
        return False


def parse_pdf(source: SourceFile, work_dir: Path, limits: IntakeLimits) -> ParsedSource:
    result = ParsedSource(parser="pdf")
    try:
        reader = PdfReader(str(source.path))
        if reader.is_encrypted and not reader.decrypt(""):
            raise SourceRejected("pdf_encrypted", "PDF 已加密，请上传未加密版本")
        pages = list(reader.pages)
    except SourceRejected:
        raise
    except (PdfReadError, OSError, ValueError) as exc:
        raise SourceRejected("pdf_unreadable", f"无法读取 PDF：{exc}") from exc
    if len(pages) > limits.max_pdf_pages:
        raise SourceRejected("pdf_too_many_pages", f"PDF 页数 {len(pages)} 超过上限 {limits.max_pdf_pages}")

    media_dir = work_dir / "media" / source.file_id
    text_chars = 0
    image_count = 0
    for number, page in enumerate(pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # pypdf raises assorted errors on damaged content streams
            result.warnings.append(f"第 {number} 页文字抽取失败：{exc}")
            text = ""
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        text_chars += len(text)
        if text_chars > limits.max_text_chars:
            raise SourceRejected("text_too_long", f"PDF 文字量超过上限 {limits.max_text_chars} 字符")
        has_images = _page_has_images(page)
        if len(text) < limits.ocr_min_chars:
            result.ocr_queue.append(
                {
                    "file_id": source.file_id,
                    "page": number,
                    "reason": "scanned_page" if has_images else "no_text_layer",
                    "text_chars": len(text),
                }
            )
        if text:
            result.items.extend(
                text_items(
                    source,
                    f"p{number:03d}",
                    text,
                    limits,
                    title=f"{source.name} 第 {number} 页",
                    location=f"page {number}",
                    page=number,
                    extra={"needs_ocr": len(text) < limits.ocr_min_chars},
                )
            )
        if not has_images or image_count >= limits.max_embedded_images:
            continue
        try:
            page_images = list(page.images)
        except Exception as exc:
            result.warnings.append(f"第 {number} 页图片抽取失败：{exc}")
            continue
        for index, embedded in enumerate(page_images, 1):
            if image_count >= limits.max_embedded_images:
                result.warnings.append(f"内嵌图片超过 {limits.max_embedded_images} 张，其余未抽取")
                break
            try:
                image = embedded.image
            except Exception:
                image = None
            if image is None:
                continue
            if image.width * image.height > limits.max_image_pixels:
                result.warnings.append(f"第 {number} 页图片 {index} 像素过多，未抽取")
                continue
            media_dir.mkdir(parents=True, exist_ok=True)
            target = media_dir / f"p{number:03d}-img{index:02d}.png"
            image.save(target, format="PNG")
            image_count += 1
            result.assets.append(
                image_asset(
                    source,
                    f"p{number:03d}-img{index:02d}",
                    target,
                    origin=f"page {number} image {index}",
                    width=image.width,
                    height=image.height,
                    image_format="PNG",
                )
            )

    _add_tables(source, result, len(pages))
    if result.ocr_queue:
        result.status = "needs_ocr"
        result.warnings.append(f"{len(result.ocr_queue)} 页没有可用文字层，需要 OCR")
    result.stats = {
        "pages": len(pages),
        "text_chars": text_chars,
        "images": image_count,
        "ocr_pages": [entry["page"] for entry in result.ocr_queue],
        "tables": len(result.tables),
    }
    return result


def _clean_cell(value) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _add_tables(source: SourceFile, result: ParsedSource, page_count: int) -> None:
    """Ruled tables per page (header = first row), as table records and evidence items."""
    try:
        import pdfplumber
    except ImportError:
        result.warnings.append("未安装 pdfplumber，PDF 表格未抽取")
        return
    try:
        document = pdfplumber.open(str(source.path))
    except Exception as exc:  # noqa: BLE001 - pypdf already read the file; tables are best effort
        result.warnings.append(f"PDF 表格抽取失败：{exc}")
        return
    with document:
        for number, page in enumerate(document.pages[:page_count], 1):
            try:
                tables = page.extract_tables({"vertical_strategy": "lines", "horizontal_strategy": "lines"})
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(f"第 {number} 页表格抽取失败：{exc}")
                continue
            index = 0
            for raw in tables:
                rows = [[_clean_cell(cell) for cell in row] for row in raw]
                rows = [row for row in rows if any(cell is not None for cell in row)]
                if len(rows) < 2 or max(len(row) for row in rows) < 2:
                    continue
                index += 1
                location = f"page {number} table {index}"
                record = table_record(source, f"p{number:03d}-t{index:02d}", rows, location=location,
                                      extra={"page": number, "extraction": "pdfplumber_lines"})
                result.tables.append(record)
                result.items.append(table_item(source, record, page=number))
