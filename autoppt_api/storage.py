"""Blob storage and content sniffing.

Local, content-addressed storage (``blobs/ab/abcdef...``) stands in for an
object store (MinIO / OSS / COS / S3) in development; the interface is the
same.  Upload types are decided from the bytes, never the extension: an
OOXML container is told apart by its parts, and a text file must decode.
A malware scanner can be plugged in; without one files stay ``not_scanned``.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import zipfile
from pathlib import Path
from typing import BinaryIO, Callable

from autoppt_service.parsers.base import EXTENSION_KINDS

Scanner = Callable[[Path], str]  # returns "clean" | "infected"


class StorageError(ValueError):
    pass


def sniff_kind(head: bytes, path: Path | None = None) -> str:
    """Kind from content: pdf, docx, pptx, xlsx, zip, image, text or unknown."""
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff"):
        return "image"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heif"):
        return "image"
    if head.startswith(b"PK\x03\x04") and path is not None:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except zipfile.BadZipFile:
            return "unknown"
        if "[Content_Types].xml" in names:
            for prefix, kind in (("word/", "docx"), ("ppt/", "pptx"), ("xl/", "xlsx")):
                if any(name.startswith(prefix) for name in names):
                    return kind
        return "zip"
    if b"\x00" in head:
        return "unknown"
    for encoding in ("utf-8", "gb18030"):
        try:
            head.decode(encoding)
            return "text"
        except UnicodeDecodeError as exc:
            if exc.start > len(head) - 4:  # a multi-byte character cut at the sample boundary
                return "text"
    return "unknown"


def kind_matches_name(kind: str, filename: str) -> bool:
    declared = EXTENSION_KINDS.get(Path(filename).suffix.lower())
    if declared is None:
        return False
    if declared in ("text", "csv"):
        return kind == "text"
    return declared == kind


class LocalStorage:
    """Content-addressed by the sha256 of the plaintext; optionally encrypted at rest (Fernet)."""

    def __init__(self, root: Path, *, key: str | None = None) -> None:
        self.root = Path(root)
        self._fernet = None
        if key:
            from cryptography.fernet import Fernet

            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        (self.root / "blobs").mkdir(parents=True, exist_ok=True)
        (self.root / "partial").mkdir(parents=True, exist_ok=True)

    @property
    def encrypted(self) -> bool:
        return self._fernet is not None

    def _seal(self, data: bytes) -> bytes:
        return self._fernet.encrypt(data) if self._fernet else data

    def read_bytes(self, key: str) -> bytes:
        data = self.path(key).read_bytes()
        return self._fernet.decrypt(data) if self._fernet else data

    def key_for(self, sha256: str) -> str:
        return f"blobs/{sha256[:2]}/{sha256}"

    def path(self, key: str) -> Path:
        resolved = (self.root / key).resolve()
        if self.root.resolve() not in resolved.parents:
            raise StorageError("storage key escapes the storage root")
        return resolved

    def partial_path(self, session_id: str) -> Path:
        return self.root / "partial" / session_id

    def append(self, session_id: str, offset: int, stream: BinaryIO, limit: int) -> int:
        """Append a chunk at ``offset`` (must equal the current size); returns the new size."""
        target = self.partial_path(session_id)
        size = target.stat().st_size if target.exists() else 0
        if offset != size:
            raise StorageError(f"chunk offset {offset} does not match received bytes {size}")
        with target.open("ab") as handle:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise StorageError("upload exceeds the size limit")
                handle.write(chunk)
        return size

    def finish(self, session_id: str) -> tuple[str, int, Path]:
        """Hash the assembled upload and move it into content-addressed storage."""
        source = self.partial_path(session_id)
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        sha = digest.hexdigest()
        target = self.path(self.key_for(sha))
        target.parent.mkdir(parents=True, exist_ok=True)
        size = source.stat().st_size
        if target.exists():
            source.unlink()
        elif self._fernet:
            target.write_bytes(self._seal(source.read_bytes()))
            source.unlink()
        else:
            shutil.move(str(source), target)
        return sha, size, target

    def put_bytes(self, data: bytes) -> tuple[str, Path]:
        sha = hashlib.sha256(data).hexdigest()
        target = self.path(self.key_for(sha))
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self._seal(data))
        return sha, target

    def put_stream(self, stream: BinaryIO, limit: int) -> tuple[str, int, Path]:
        temp = self.partial_path(f"direct-{hashlib.sha256(str(id(stream)).encode()).hexdigest()[:16]}")
        temp.unlink(missing_ok=True)
        self.append(temp.name, 0, stream, limit)
        return self.finish(temp.name)

    def open(self, key: str) -> io.BytesIO:
        return io.BytesIO(self.read_bytes(key))

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)
