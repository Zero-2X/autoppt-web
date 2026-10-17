"""Front end: served by the API, parses as JavaScript, and never uses innerHTML."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autoppt_api.app import create_app
from autoppt_api.config import Settings

WEB = Path(__file__).resolve().parents[1] / "web"
SCRIPTS = ("app.js", "api.js", "i18n.js")


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(Settings(database_url=f"sqlite:///{(tmp_path / 'a.db').as_posix()}", storage_dir=tmp_path / "s")))


def code_only(name: str) -> str:
    """Script source without whole-line comments."""
    return "\n".join(line for line in (WEB / name).read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("//"))


def test_index_and_assets_are_served_with_security_headers(client):
    page = client.get("/")
    assert page.status_code == 200 and '<script type="module" src="/app.js">' in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "script-src 'self'" in page.headers["content-security-policy"]
    for name, kind in (("app.js", "javascript"), ("api.js", "javascript"), ("i18n.js", "javascript"), ("styles.css", "css")):
        response = client.get(f"/{name}")
        assert response.status_code == 200 and kind in response.headers["content-type"]
    assert client.get("/api/health").json() == {"status": "ok"}  # API routes still win


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("name", SCRIPTS)
def test_javascript_parses(name):
    source = (WEB / name).read_text(encoding="utf-8")
    completed = subprocess.run(["node", "--input-type=module", "--check"], input=source, capture_output=True, text=True,
                               encoding="utf-8")
    assert completed.returncode == 0, completed.stderr


def test_no_html_injection_sinks():
    for name in SCRIPTS:
        source = code_only(name)
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert sink not in source, f"{name} uses {sink}"


def test_every_api_path_used_by_the_front_end_exists(client):
    templates = {re.sub(r"\{[^}]+\}", "{}", path) for path in client.get("/openapi.json").json()["paths"]}
    source = code_only("app.js") + code_only("api.js")
    used = set(re.findall(r"[\"`](/api/[^\"`?]*)", source))
    assert len(used) > 15
    for path in used:
        normalized = re.sub(r"\$\{[\w.]+\}", "{}", path)  # ${jobId}, ${cp.checkpoint_id}
        normalized = normalized.split("${", 1)[0].rstrip("/")  # optional query-string expressions
        assert normalized in templates or any(t.startswith(normalized + "/") for t in templates), f"unknown API path {path}"
