"""Shared limits, records and helpers for the upload parsers.

Every parser returns a :class:`ParsedSource`: evidence items (dicts that
``autoppt_workflow.presentation.schemas.normalize_evidence_item`` accepts),
tables, image assets and OCR requests.  Uploaded text is data for the planner,
never instructions, and nothing extracted here is marked as confirmed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

MB = 1024 * 1024

EXTENSION_KINDS: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".csv": "csv",
    ".md": "text",
    ".markdown": "text",
    ".txt": "text",
    ".tex": "text",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".heic": "image",
    ".heif": "image",
    ".zip": "zip",
}


# Which file kinds each upload role accepts (shared by the request validator and ZIP unpacking).
ROLE_FILE_KINDS: dict[str, frozenset[str]] = {
    "primary_source": frozenset({"pdf", "docx", "pptx", "xlsx", "csv", "text", "zip"}),
    "supplementary": frozenset({"pdf", "docx", "pptx", "xlsx", "csv", "text", "zip"}),
    "chart_data": frozenset({"xlsx", "csv"}),
    "image_asset": frozenset({"image", "zip"}),
    "keep_as_image": frozenset({"image"}),
    "logo": frozenset({"image"}),
    "style_reference": frozenset({"pptx", "pdf", "image", "zip"}),
    "vectorize_source": frozenset({"pptx", "pdf", "image", "zip"}),
}


@dataclass(frozen=True)
class IntakeLimits:
    max_files: int = 30
    max_file_bytes: int = 100 * MB
    max_total_bytes: int = 300 * MB
    max_pdf_pages: int = 300
    max_pptx_slides: int = 200
    max_image_pixels: int = 80_000_000
    max_embedded_images: int = 200
    max_sheet_rows: int = 5000
    max_sheet_cols: int = 100
    max_text_chars: int = 2_000_000
    max_item_chars: int = 2000
    # ZIP uploads and the OOXML containers (docx/pptx/xlsx) are both checked.
    max_zip_entries: int = 500
    max_zip_entry_bytes: int = 100 * MB
    max_zip_total_bytes: int = 500 * MB
    max_zip_ratio: float = 100.0
    zip_ratio_min_bytes: int = 1 * MB
    max_ooxml_total_bytes: int = 1024 * MB
    # Repetitive spreadsheet/document XML compresses harder than ordinary uploads.
    max_ooxml_ratio: float = 200.0
    # A PDF page with less text than this is queued for OCR.
    ocr_min_chars: int = 20


class SourceRejected(Exception):
    """The upload cannot be used (unsafe, over a limit, or unreadable)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class SourceFile:
    file_id: str
    role: str
    name: str
    path: Path
    origin: str = ""  # parent archive member path for files unpacked from a ZIP


@dataclass
class ParsedSource:
    parser: str
    status: str = "parsed"  # parsed | needs_ocr | needs_transcode
    items: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    assets: list[dict[str, Any]] = field(default_factory=list)
    ocr_queue: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    children: list[dict[str, Any]] = field(default_factory=list)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(MB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_text(text: str) -> str:
    # str.split() already treats NBSP and full-width spaces as whitespace.
    return " ".join(str(text or "").split())


def id_token(value: str) -> str:
    """Evidence-ID-safe token (matches ``schemas.EVIDENCE_ID_RE``'s ``ev_`` form)."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "x"


def evidence_id(source: SourceFile, suffix: str) -> str:
    return f"ev_{id_token(source.file_id)}_{id_token(suffix)}"


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split on paragraph, then sentence, then hard boundaries to stay under ``max_chars``."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    pieces: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        while len(paragraph) > max_chars:
            cut = max(paragraph.rfind(mark, 0, max_chars) for mark in ("。", "！", "？", ". ", "\n", "；", "; "))
            cut = cut + 1 if cut > max_chars // 3 else max_chars
            pieces.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if paragraph:
            pieces.append(paragraph)
    chunks: list[str] = []
    for piece in pieces:
        if chunks and len(chunks[-1]) + len(piece) + 2 <= max_chars:
            chunks[-1] = f"{chunks[-1]}\n\n{piece}"
        else:
            chunks.append(piece)
    return chunks


def make_item(
    source: SourceFile,
    suffix: str,
    *,
    item_type: str,
    text: str,
    title: str = "",
    location: str | Iterable[str] = (),
    page: int | None = None,
    table: str | None = None,
    figure: str | None = None,
    chapter: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    locations = [location] if isinstance(location, str) else list(location)
    item: dict[str, Any] = {
        "evidence_id": evidence_id(source, suffix),
        "item_type": item_type,
        "file_id": source.file_id,
        "role": source.role,
        "title": title or f"{source.name} {suffix}",
        "fact_or_data": text,
        "source_file": source.name,
        "source_locations": [value for value in locations if value],
        "page": page,
        "table": table,
        "figure": figure,
        "chapter": chapter,
        "confirmed": False,
        "ppt_allowed": True,
        "reliability": "uploaded_source",
        "risk_level": "unknown",
        "risk_notes": "extracted from an upload; not yet confirmed against the source",
    }
    if source.origin:
        item["archive_member"] = source.origin
    if extra:
        item.update(extra)
    return item


def text_items(
    source: SourceFile,
    prefix: str,
    text: str,
    limits: IntakeLimits,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    chunks = chunk_text(text, limits.max_item_chars)
    items = []
    for index, chunk in enumerate(chunks, 1):
        suffix = prefix if len(chunks) == 1 else f"{prefix}.{index}"
        items.append(make_item(source, suffix, item_type="text", text=chunk, **kwargs))
    return items


def table_record(
    source: SourceFile,
    table_id: str,
    rows: list[list[Any]],
    *,
    location: str,
    truncated: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Table entry plus a numeric-column hint for native chart generation."""
    width = max((len(row) for row in rows), default=0)
    rows = [list(row) + [None] * (width - len(row)) for row in rows]
    header = [str(value) if value is not None else "" for value in rows[0]] if rows else []
    body = rows[1:]
    numeric_columns = [
        index
        for index in range(width)
        if body and sum(_is_number(row[index]) for row in body) >= max(1, (len(body) + 1) // 2)
    ]
    record = {
        "table_id": f"{id_token(source.file_id)}_{id_token(table_id)}",
        "local_id": table_id,
        "file_id": source.file_id,
        "role": source.role,
        "source_file": source.name,
        "location": location,
        "header": header,
        "rows": [[_jsonable(value) for value in row] for row in body],
        "row_count": len(body),
        "column_count": width,
        "numeric_columns": numeric_columns,
        "chart_candidate": bool(numeric_columns) and len(body) >= 2,
        "truncated": truncated,
    }
    if extra:
        record.update(extra)
    return record


def table_item(source: SourceFile, record: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    header = " | ".join(record["header"])
    preview = [" | ".join("" if value is None else str(value) for value in row) for row in record["rows"][:5]]
    text = "\n".join([header, *preview])
    if record["row_count"] > 5:
        text += f"\n…（共 {record['row_count']} 行）"
    return make_item(
        source,
        record["local_id"],
        item_type="table",
        text=text,
        title=kwargs.pop("title", f"{source.name} {record['location']}"),
        location=record["location"],
        table=record["table_id"],
        extra={"table_id": record["table_id"], "chart_candidate": record["chart_candidate"]},
        **kwargs,
    )


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value.replace(",", "").replace("%", "").strip())
            return bool(value.strip())
        except ValueError:
            return False
    return False


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def image_asset(
    source: SourceFile,
    asset_suffix: str,
    path: Path,
    *,
    origin: str,
    width: int | None,
    height: int | None,
    image_format: str | None,
) -> dict[str, Any]:
    return {
        "asset_id": f"{id_token(source.file_id)}_{id_token(asset_suffix)}",
        "file_id": source.file_id,
        "role": source.role,
        "source_file": source.name,
        "origin": origin,
        "path": str(Path(path).resolve()),
        "sha256": sha256_file(path),
        "bytes": Path(path).stat().st_size,
        "width": width,
        "height": height,
        "format": image_format,
        # Profile rule: never decoratively redraw original figures; kept images stay bounded raster.
        "keep_as_image": source.role in {"keep_as_image", "logo"},
    }


def decode_text(data: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"
