"""DOCX: reuse ``autopptskills/scripts/extract_docx_evidence.extract`` and map its output.

Paragraphs are grouped under their nearest heading into section items;
tables and embedded media become table records and assets.
"""

from __future__ import annotations

from pathlib import Path

from autoppt_service.parsers.archive import inspect_ooxml
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
from autoppt_service.repo_paths import ensure_autoppt_on_path

ensure_autoppt_on_path()
from autopptskills.scripts.extract_docx_evidence import extract  # noqa: E402  (read-only use)


def parse_docx(source: SourceFile, work_dir: Path, limits: IntakeLimits) -> ParsedSource:
    inspect_ooxml(source.path, limits)
    media_dir = work_dir / "media" / source.file_id
    try:
        data = extract(source.path, media_dir)
    except Exception as exc:
        raise SourceRejected("docx_unreadable", f"无法读取 DOCX：{exc}") from exc

    result = ParsedSource(parser="docx")
    total_chars = sum(len(p["text"]) for p in data["paragraphs"])
    if total_chars > limits.max_text_chars:
        raise SourceRejected("text_too_long", f"DOCX 文字量超过上限 {limits.max_text_chars} 字符")

    sections: list[dict] = [{"heading": "", "paragraphs": []}]
    for paragraph in data["paragraphs"]:
        if paragraph.get("heading_level") is not None:
            sections.append({"heading": paragraph["text"], "start": paragraph["id"], "paragraphs": []})
        else:
            sections[-1]["paragraphs"].append(paragraph)
    for index, section in enumerate(sections, 1):
        body = "\n".join(p["text"] for p in section["paragraphs"])
        heading = section["heading"]
        content = f"{heading}\n{body}".strip() if heading else body
        if not content:
            continue
        refs = [p["id"] for p in section["paragraphs"]]
        location = section.get("start") or (refs[0] if refs else f"section {index}")
        if refs:
            location = f"{location}..{refs[-1]}" if refs[-1] != location else location
        result.items.extend(
            text_items(
                source,
                f"sec{index:03d}",
                content,
                limits,
                title=heading or f"{source.name} 第 {index} 节",
                location=location,
                chapter=heading or None,
            )
        )

    for table in data["tables"]:
        record = table_record(source, table["id"], table["rows"], location=table["id"])
        result.tables.append(record)
        result.items.append(table_item(source, record))

    for media in data["media"][: limits.max_embedded_images]:
        result.assets.append(
            image_asset(
                source,
                media["id"],
                Path(media["path"]),
                origin=f"word/media/{media['original_name']}",
                width=media.get("width"),
                height=media.get("height"),
                image_format=media.get("format"),
            )
        )
    if len(data["media"]) > limits.max_embedded_images:
        result.warnings.append(f"内嵌图片超过 {limits.max_embedded_images} 张，其余未登记")

    result.stats = dict(data["statistics"])
    return result
