"""Model gateway for API-key mode (D2): Workers never hold a provider key.

A Worker asks the API for a model token bound to its lease (``/api/worker/jobs/{id}/model-token``) and runs
``codex exec`` against ``<public base>/gateway/v1`` with that token (``codex_exec`` adds the provider
overrides).  The gateway checks the token (job, expiry, input-token budget), forwards the request to the
upstream API with a pooled key, streams the answer back and adds the usage reported in the response
(``response.completed`` for streams, ``usage`` for JSON) to the token.

Keys rotate round-robin; a key that answers 429 or 5xx rests (``Retry-After`` or 60 s) and the request
moves to the next key before anything has been streamed.  Each key has a concurrency cap.

Not yet verified against the real provider: whether the Codex ``image_gen`` tool works under API-key
authentication (checklist P0); the forwarding itself is tested with a mock upstream and the real
``codex`` CLI.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ALLOWED_PATHS = ("responses", "models")  # Codex speaks the Responses API; nothing else is forwarded
RETRY_STATUSES = {429, 500, 502, 503, 504}
DEFAULT_REST_S = 60.0


@dataclass
class ProviderSecret:
    label: str
    key: str = field(repr=False)
    max_concurrency: int = 4
    in_flight: int = 0
    requests: int = 0
    errors: int = 0
    resting_until: float = 0.0


class KeyPool:
    def __init__(self, keys: list[ProviderSecret]) -> None:
        self._keys = keys
        self._next = 0
        self._lock = threading.Lock()

    @classmethod
    def from_file(cls, path: str) -> "KeyPool":
        entries = json.loads(Path(path).read_text(encoding="utf-8-sig")) if path else []
        return cls([ProviderSecret(label=str(e["label"]), key=str(e["key"]), max_concurrency=int(e.get("max_concurrency", 4)))
                    for e in entries])

    def __bool__(self) -> bool:
        return bool(self._keys)

    def acquire(self, exclude: set[str] = frozenset()) -> ProviderSecret | None:
        """The next key that is not resting, not excluded and below its concurrency cap."""
        now = time.monotonic()
        with self._lock:
            for offset in range(len(self._keys)):
                key = self._keys[(self._next + offset) % len(self._keys)]
                if key.label in exclude or key.resting_until > now or key.in_flight >= key.max_concurrency:
                    continue
                self._next = (self._next + offset + 1) % len(self._keys)
                key.in_flight += 1
                key.requests += 1
                return key
        return None

    def release(self, key: ProviderSecret, *, failed_status: int | None = None, retry_after: float | None = None) -> None:
        with self._lock:
            key.in_flight = max(0, key.in_flight - 1)
            if failed_status is not None:
                key.errors += 1
                if failed_status in RETRY_STATUSES:
                    key.resting_until = time.monotonic() + (retry_after if retry_after is not None else DEFAULT_REST_S)

    def stats(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            return [{"label": k.label, "requests": k.requests, "errors": k.errors, "in_flight": k.in_flight,
                     "max_concurrency": k.max_concurrency, "resting_s": max(0, round(k.resting_until - now))}
                    for k in self._keys]


def retry_after_seconds(headers: Any) -> float | None:
    value = headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class UsageMeter:
    """Reads token usage out of a streamed (SSE) or plain JSON Responses API answer."""

    def __init__(self) -> None:
        self._buffer = b""
        self._json = bytearray()
        self.usage: dict[str, int] = {}

    def feed(self, chunk: bytes) -> None:
        self._json += chunk if len(self._json) < 8_000_000 else b""
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.strip()
            if line.startswith(b"data:"):
                self._take(line[5:].strip())

    def finish(self) -> dict[str, int]:
        if not self.usage and self._json:
            self._take(bytes(self._json))
        return self.usage

    def _take(self, data: bytes) -> None:
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict):
            return
        usage = (payload.get("response") or {}).get("usage") if payload.get("type") == "response.completed" else payload.get("usage")
        if isinstance(usage, dict):
            self.usage = {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "cached_input_tokens": int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
            }
