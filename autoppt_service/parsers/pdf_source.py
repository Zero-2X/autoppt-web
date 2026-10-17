"""PDF: text layer per page, embedded images, and OCR flags for scanned pages.

OCR itself is not run here; pages whose text layer is (nearly) empty are put
on the manifest's ``ocr_queue`` for a later OCR step.
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

    if result.ocr_queue:
        result.status = "needs_ocr"
        result.warnings.append(f"{len(result.ocr_queue)} 页没有可用文字层，需要 OCR")
    result.stats = {
        "pages": len(pages),
        "text_chars": text_chars,
        "images": image_count,
        "ocr_pages": [entry["page"] for entry in result.ocr_queue],
    }
    return result
