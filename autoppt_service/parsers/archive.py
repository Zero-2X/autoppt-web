"""Safe ZIP handling: inspect before trusting, stream-extract with byte caps.

Used for ZIP uploads and, in check-only mode, for the OOXML containers
(docx/pptx/xlsx), which are ZIP files too and can carry the same bombs.

Rejected outright: absolute paths, drive letters, ``..`` components, symlinks,
encrypted members, nested archives beyond depth 1, too many members, members
or totals over the byte limits, and members whose compression ratio marks a
zip bomb.  Header sizes are not trusted: extraction counts the bytes it
actually writes and aborts at the declared size or the limit.
"""

from __future__ import annotations

import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from autoppt_service.parsers.base import EXTENSION_KINDS, IntakeLimits, SourceRejected

SKIPPED_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
SKIPPED_DIRS = {"__macosx"}
_UTF8_FLAG = 0x800
_ENCRYPTED_FLAG = 0x1


def member_name(info: zipfile.ZipInfo) -> str:
    """Decode legacy (non-UTF-8) names the way Chinese Windows archivers write them."""
    name = info.filename
    if not info.flag_bits & _UTF8_FLAG:
        try:
            name = name.encode("cp437").decode("gbk")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name


def safe_member_path(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        raise SourceRejected("zip_path_traversal", f"压缩包含有绝对路径：{name}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise SourceRejected("zip_path_traversal", f"压缩包含有越界路径：{name}")
    if any(":" in part for part in parts):
        raise SourceRejected("zip_path_traversal", f"压缩包路径含有非法字符：{name}")
    if not parts:
        raise SourceRejected("zip_path_traversal", f"压缩包含有空路径：{name!r}")
    return PurePosixPath(*parts)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def inspect_zip(
    path: Path,
    limits: IntakeLimits,
    *,
    total_limit: int | None = None,
    max_ratio: float | None = None,
) -> list[zipfile.ZipInfo]:
    """Check every member header; raise :class:`SourceRejected` on anything unsafe."""
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise SourceRejected("bad_zip", f"无法读取压缩文件：{exc}") from exc
    with archive:
        infos = archive.infolist()
    if len(infos) > limits.max_zip_entries:
        raise SourceRejected("zip_too_many_entries", f"压缩包成员过多：{len(infos)} > {limits.max_zip_entries}")
    total = 0
    cap = total_limit if total_limit is not None else limits.max_zip_total_bytes
    for info in infos:
        name = member_name(info)
        safe_member_path(name)
        if _is_symlink(info):
            raise SourceRejected("zip_symlink", f"压缩包含有符号链接：{name}")
        if info.flag_bits & _ENCRYPTED_FLAG:
            raise SourceRejected("zip_encrypted", f"压缩包含有加密成员：{name}")
        if info.file_size > limits.max_zip_entry_bytes:
            raise SourceRejected("zip_entry_too_large", f"压缩包成员解压后过大：{name}")
        if info.file_size >= limits.zip_ratio_min_bytes:
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > (max_ratio if max_ratio is not None else limits.max_zip_ratio):
                raise SourceRejected("zip_bomb", f"压缩比异常（{ratio:.0f}:1），疑似压缩炸弹：{name}")
        total += info.file_size
        if total > cap:
            raise SourceRejected("zip_total_too_large", f"压缩包解压后总大小超过上限 {cap // (1024 * 1024)} MB")
    return infos


def _unique(target_dir: Path, relative: PurePosixPath, taken: set[str]) -> Path:
    candidate = relative
    counter = 2
    # Windows paths are case-insensitive: treat case-only collisions as clashes.
    while str(candidate).lower() in taken:
        candidate = relative.with_name(f"{relative.stem}-{counter}{relative.suffix}")
        counter += 1
    taken.add(str(candidate).lower())
    return target_dir.joinpath(*candidate.parts)


def extract_zip(path: Path, target_dir: Path, limits: IntakeLimits) -> dict[str, Any]:
    """Extract supported members; return ``{"files": [...], "skipped": [...]}``.

    On any rejection or read error the whole ``target_dir`` is removed, so a
    rejected archive never leaves partial files behind.
    """
    infos = inspect_zip(path, limits)
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        return _extract_members(path, infos, target_dir, limits)
    except SourceRejected:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError) as exc:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise SourceRejected("bad_zip", f"压缩包成员损坏或与声明不符：{exc}") from exc


def _extract_members(
    path: Path, infos: list[zipfile.ZipInfo], target_dir: Path, limits: IntakeLimits
) -> dict[str, Any]:
    root = target_dir.resolve()
    files: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    taken: set[str] = set()
    written_total = 0
    with zipfile.ZipFile(path) as archive:
        for info in infos:
            if info.is_dir():
                continue
            name = member_name(info)
            relative = safe_member_path(name)
            lowered = [part.lower() for part in relative.parts]
            if lowered[-1] in SKIPPED_NAMES or any(part in SKIPPED_DIRS for part in lowered[:-1]):
                skipped.append({"member": name, "reason": "system_file"})
                continue
            kind = EXTENSION_KINDS.get(relative.suffix.lower())
            if kind is None:
                skipped.append({"member": name, "reason": "unsupported_type"})
                continue
            if kind == "zip":
                skipped.append({"member": name, "reason": "nested_archive"})
                continue
            destination = _unique(target_dir, relative, taken)
            if root not in destination.resolve().parents:
                raise SourceRejected("zip_path_traversal", f"压缩包成员越界：{name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            cap = min(info.file_size, limits.max_zip_entry_bytes)
            written = 0
            with archive.open(info) as src, destination.open("wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    written_total += len(chunk)
                    if written > cap or written_total > limits.max_zip_total_bytes:
                        raise SourceRejected("zip_bomb", f"成员实际解压大小超过声明或上限：{name}")
                    dst.write(chunk)
            files.append({"member": name, "path": destination, "bytes": written, "kind": kind})
    return {"files": files, "skipped": skipped}


def inspect_ooxml(path: Path, limits: IntakeLimits) -> None:
    """Bomb/traversal check for docx/pptx/xlsx containers before a library opens them."""
    inspect_zip(path, limits, total_limit=limits.max_ooxml_total_bytes, max_ratio=limits.max_ooxml_ratio)
