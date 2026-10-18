"""Codex CLI resolution."""

from __future__ import annotations

import pytest


def test_an_old_codex_cli_is_refused_before_any_session(tmp_path, monkeypatch):
    # An unpinned run on 2026-10-18 picked up 0.155.1 from PATH.
    import autoppt_service.codex_exec as codex_exec

    monkeypatch.setenv("AUTOPPT_CODEX_BIN", str(tmp_path / "codex-old"))
    monkeypatch.setitem(codex_exec._checked_versions, str(tmp_path / "codex-old"), (0, 155, 1))
    with pytest.raises(FileNotFoundError, match="older than 0.163.0"):
        codex_exec.codex_command()
    monkeypatch.setitem(codex_exec._checked_versions, str(tmp_path / "codex-old"), (0, 163, 0))
    assert codex_exec.codex_command() == [str(tmp_path / "codex-old")]


def test_model_can_be_pinned_per_run(tmp_path, monkeypatch):
    import json
    import sys

    from autoppt_service.codex_exec import CodexProfile, run_codex_exec

    script = tmp_path / "fake.py"
    script.write_text("import sys, json; open(sys.argv[0] + '.argv', 'w').write(json.dumps(sys.argv[1:]))", encoding="utf-8")
    monkeypatch.setenv("AUTOPPT_CODEX_MODEL", "gpt-6-luna")
    run_codex_exec("hi", cwd=tmp_path / "job", profile=CodexProfile(name="p", codex_home=None), run_dir=tmp_path / "run",
                   command=[sys.executable, str(script)])
    argv = json.loads((tmp_path / "fake.py.argv").read_text(encoding="utf-8"))
    assert argv[argv.index("-m") + 1] == "gpt-6-luna"
