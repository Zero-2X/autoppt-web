"""MD / TXT / TEX: plain-text sources split into sections."""

from __future__ import annotations

import re
from pathlib import Path

from autoppt_service.parsers.base import (
    IntakeLimits,
    ParsedSource,
    SourceFile,
    SourceRejected,
    decode_text,
    text_items,
)

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_TEX_SECTION = re.compile(r"^\s*\\(chapter|section|subsection|subsubsection)\*?\{(.+?)\}")
_TEX_COMMENT = re.compile(r"(?<!\\)%.*$")
_TEX_GRAPHIC = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}")
_TEX_DROP_ENV = re.compile(r"\\(?:begin|end)\{[^}]+\}")
_TEX_INLINE = re.compile(r"\\(?:textbf|textit|emph|underline|text)\{([^{}]*)\}")
_TEX_REF = re.compile(r"\\(?:cite|ref|label|eqref)\{[^}]*\}")


def _sections(lines: list[str], heading) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in lines:
        title = heading(line)
        if title is not None:
            sections.append((title, []))
        else:
            sections[-1][1].append(line)
    return [(title, "\n".join(body).strip()) for title, body in sections if title or "\n".join(body).strip()]


def _markdown_heading(line: str) -> str | None:
    match = _MD_HEADING.match(line)
    return match.group(2).strip() if match else None


def _tex_heading(line: str) -> str | None:
    match = _TEX_SECTION.match(line)
    return match.group(2).strip() if match else None


def _clean_tex(line: str) -> str:
    line = _TEX_COMMENT.sub("", line)
    line = _TEX_REF.sub("", line)
    line = _TEX_INLINE.sub(r"\1", line)
    return _TEX_DROP_ENV.sub("", line).rstrip()


def parse_text(source: SourceFile, work_dir: Path, limits: IntakeLimits) -> ParsedSource:
    text, encoding = decode_text(source.path.read_bytes())
    if len(text) > limits.max_text_chars:
        raise SourceRejected("text_too_long", f"文本超过上限 {limits.max_text_chars} 字符")
    suffix = source.path.suffix.lower()
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result = ParsedSource(parser={".md": "markdown", ".markdown": "markdown", ".tex": "tex"}.get(suffix, "text"))
    if suffix == ".tex":
        graphics = [match.group(1) for line in lines for match in _TEX_GRAPHIC.finditer(line)]
        if graphics:
            result.stats["tex_graphics"] = graphics
            result.warnings.append(f"TeX 引用了 {len(graphics)} 个图片文件；请把图片一起上传（可打包为 ZIP）")
        sections = _sections([_clean_tex(line) for line in lines if not _TEX_GRAPHIC.search(line)], _tex_heading)
    elif suffix in {".md", ".markdown"}:
        sections = _sections(lines, _markdown_heading)
    else:
        sections = [("", text.strip())] if text.strip() else []

    for index, (title, body) in enumerate(sections, 1):
        content = f"{title}\n{body}".strip() if title else body
        if not content:
            continue
        result.items.extend(
            text_items(
                source,
                f"sec{index:03d}",
                content,
                limits,
                title=title or f"{source.name} 第 {index} 段",
                location=f"section {index}" + (f": {title}" if title else ""),
                chapter=title or None,
            )
        )
    result.stats.update({"encoding": encoding, "chars": len(text), "sections": len(sections)})
    if encoding == "latin-1":
        result.warnings.append("无法识别文本编码，按 latin-1 读取，可能有乱码")
    return result
