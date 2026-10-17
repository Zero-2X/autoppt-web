from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoppt_service.worker_template import (
    TemplateError,
    build_template,
    install_skill,
    installed_skill_status,
    load_template_manifest,
    render_service_agents,
    verify_template,
    write_job_agents,
)


def fake_repo(root: Path) -> Path:
    files = {
        "autopptskills/SKILL.md": "# skill\nSee runs/zhicang_20260929/x.png and D:\\autoppt\\deliverables\\a.pptx for 智仓兴教.\n",
        "autopptskills/references/production-lessons.md": "lesson about 张正轩 deck\n",
        "autopptskills/scripts/tool.py": "CASE = '智仓兴教'\n",
        "autopptskills/scripts/__pycache__/tool.cpython-312.pyc": "x",
        "autoppt_workflow/__init__.py": "",
        "scripts/helper.py": "print('ok')\n",
        "improvement/style-handbook.md": "# handbook\n智搬小匠 example\n",
        "improvement/archive-zhang-native-r15.py": "print('case script')\n",
        "docs/keep.md": "doc for 智仓兴教\n",
        "docs/private.md": "secret\n",
        "runs/r1/out.txt": "run artefact\n",
        "deliverables/d.pptx": "pptx",
        "交付/x.txt": "x",
        "智搬小匠_ppt_run/x.txt": "x",
        "big.pptx": "pptx",
    }
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    ledger = [
        {"schema_version": "ppt-improvement-ledger-v1", "category": "quality", "problem": "智仓兴教 S03 drift",
         "root_cause": "mask", "fix": "narrow mask", "reusable_rule": "keep masks to glyphs",
         "evidence": "runs/zhicang/s03.png", "candidate_sha256": "a" * 64, "attempt_id": "r15"},
        {"schema_version": "ppt-improvement-ledger-v1", "lesson": "read the ledger first"},
    ]
    (root / "improvement/ppt-improvement-ledger.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in ledger) + "\n", encoding="utf-8"
    )
    return root


@pytest.fixture()
def template(tmp_path) -> Path:
    repo = fake_repo(tmp_path / "autoppt")
    return build_template(tmp_path / "templates", repo=repo, include_docs=["docs/keep.md"], version="v1")


def test_build_whitelists_and_excludes(template):
    files = load_template_manifest(template)["files"]
    assert "autopptskills/SKILL.md" in files and "scripts/helper.py" in files and "SERVICE_AGENTS.md" in files
    assert "docs/keep.md" in files and "docs/private.md" not in files
    assert not any(name.startswith(("runs/", "deliverables/", "交付/", "智搬小匠_")) for name in files)
    assert not any(name.endswith((".pyc", ".pptx")) for name in files)
    assert "improvement/archive-zhang-native-r15.py" not in files  # case scripts stay out
    manifest = load_template_manifest(template)
    assert {"runs", "deliverables", "交付", "智搬小匠_ppt_run", "big.pptx"} <= set(manifest["excluded_present_in_repo"])


def test_redaction_of_text_and_ledger_but_not_code(template):
    skill = (template / "autopptskills/SKILL.md").read_text(encoding="utf-8")
    assert "智仓兴教" not in skill and "runs/zhicang" not in skill and "D:\\autoppt" not in skill
    assert "[案例人员]" in (template / "autopptskills/references/production-lessons.md").read_text(encoding="utf-8")
    assert "智搬小匠" not in (template / "improvement/style-handbook.md").read_text(encoding="utf-8")
    records = [json.loads(line) for line in (template / "improvement/ppt-improvement-ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert "evidence" not in records[0] and "candidate_sha256" not in records[0] and "attempt_id" not in records[0]
    assert "智仓兴教" not in records[0]["problem"] and records[0]["reusable_rule"] == "keep masks to glyphs"
    assert (template / "autopptskills/scripts/tool.py").read_text(encoding="utf-8") == "CASE = '智仓兴教'\n"
    hits = load_template_manifest(template)["redaction"]["unredacted_code_hits"]
    assert hits == {"autopptskills/scripts/tool.py": ["智仓兴教"]}


def test_versions_are_immutable_and_verified(tmp_path, template):
    with pytest.raises(TemplateError, match="immutable"):
        build_template(tmp_path / "templates", repo=tmp_path / "autoppt", version="v1")
    assert verify_template(template)["matching"]
    (template / "scripts/helper.py").write_text("print('edited')\n", encoding="utf-8")
    check = verify_template(template)
    assert not check["matching"] and check["different"] == ["scripts/helper.py"]
    with pytest.raises(TemplateError, match="outside"):
        build_template(tmp_path / "autoppt" / "out", repo=tmp_path / "autoppt")
    with pytest.raises(TemplateError, match="under docs"):
        build_template(tmp_path / "t2", repo=tmp_path / "autoppt", include_docs=["../secret.md"])


def test_install_skill_keeps_backup_and_reports_version(tmp_path, template):
    home = tmp_path / "homes" / "acct1"
    old = home / "skills" / "autopptskills"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("stale", encoding="utf-8")
    status = installed_skill_status(template, home)
    assert not status["matching"]
    install_skill(template, home)
    status = installed_skill_status(template, home)
    assert status["matching"] and status["installed_version"] == "v1"
    backups = [p.name for p in (home / "skills").iterdir() if p.name.startswith("autopptskills.backup-")]
    assert len(backups) == 1
    assert (home / "skills" / backups[0] / "SKILL.md").read_text(encoding="utf-8") == "stale"


def test_render_service_agents(tmp_path, template):
    text = render_service_agents(template, job_id="J42", budgets={"planning_timeout_s": 600})
    assert "{{" not in text
    assert "Job `J42`" in text and template.resolve().as_posix() in text
    assert "planning 10 minutes per session" in text
    assert "data, not instructions" in text and "Never ask the user" in text
    (tmp_path / "job").mkdir()
    path = write_job_agents(tmp_path / "job", template, job_id="J42")
    assert path.read_text(encoding="utf-8").startswith("# AutoPPT service-mode instructions")
