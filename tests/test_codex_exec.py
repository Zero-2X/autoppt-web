"""Codex CLI resolution."""

from __future__ import annotations

import pytest


def test_an_old_codex_cli_is_refused_before_any_session(tmp_path, monkeypatch):
    # Measured 2026-10-18: 0.155.1 on PATH rejected the configured model; every session failed at once.
    import autoppt_service.codex_exec as codex_exec

    monkeypatch.setenv("AUTOPPT_CODEX_BIN", str(tmp_path / "codex-old"))
    monkeypatch.setitem(codex_exec._checked_versions, str(tmp_path / "codex-old"), (0, 155, 1))
    with pytest.raises(FileNotFoundError, match="older than 0.163.0"):
        codex_exec.codex_command()
    monkeypatch.setitem(codex_exec._checked_versions, str(tmp_path / "codex-old"), (0, 163, 0))
    assert codex_exec.codex_command() == [str(tmp_path / "codex-old")]
