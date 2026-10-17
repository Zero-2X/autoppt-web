from __future__ import annotations

import json
from copy import deepcopy

import pytest

from autoppt_service.job_request import (
    SCHEMA_VERSION,
    job_request_schema,
    main,
    validate_job_request,
)
from autoppt_service.parsers.base import IntakeLimits

LONG_DESCRIPTION = "本项目研究城市内涝预警，目标是在暴雨前两小时给出街区级风险等级。" * 8


def base_request() -> dict:
    # The section 3.4 example from the development checklist.
    return {
        "schema_version": SCHEMA_VERSION,
        "service": "new_deck",
        "inputs": {
            "files": [
                {"file_id": "f_01", "role": "primary_source", "name": "论文终稿.docx", "sha256": "a" * 64},
                {"file_id": "f_02", "role": "chart_data", "name": "实验结果.xlsx", "sha256": "b" * 64},
                {"file_id": "f_03", "role": "keep_as_image", "name": "图3-2.png", "sha256": "c" * 64},
            ],
            "description": "……",
            "must_include": ["本人贡献单独一页"],
            "must_avoid": ["把伪标签一致性写成实测精度"],
        },
        "scenario": "thesis_defense",
        "audience": "答辩委员会",
        "language": "zh-CN",
        "slides": {"mode": "range", "min": 12, "max": 16, "talk_minutes": 12},
        "style": {"profile": "academic_light", "density": "medium_high", "reference_file_ids": []},
        "cover": {"title": "…", "author": "…", "advisor": "…", "institution": "…", "date": "2027-05"},
        "delivery": {
            "tier": "native_editable",
            "aspect_ratio": "16:9",
            "font_policy": "office_native_yahei",
            "charts": "native_with_workbook",
            "speaker_notes": True,
            "anticipated_questions": True,
        },
        "review": {"outline_checkpoint": True, "master_checkpoint": True, "native_preview_checkpoint": True},
        "privacy": {"retention_days": 7, "allow_case_study": False},
    }


def codes(result) -> set[str]:
    return {error["code"] for error in result.errors}


def warning_codes(result) -> set[str]:
    return {warning["code"] for warning in result.warnings}


def test_checklist_example_is_valid():
    result = validate_job_request(base_request())
    assert result.ok, result.errors
    assert result.derived["content_basis"] == "documents"
    assert result.derived["description_role"] == "focus_notes"
    assert result.derived["resolved_slides"]["min"] == 12
    assert result.derived["resolved_slides"]["max"] == 16
    assert result.derived["repo_tier"] == "native-priority-approved"
    assert result.derived["checkpoints"] == ["outline", "master", "native_preview"]
    assert result.derived["keep_as_image_file_ids"] == ["f_03"]
    assert result.derived["addons"] == ["speaker_notes", "anticipated_questions"]
    assert result.to_dict()["ok"] is True


def test_schema_is_valid_draft_2020_12():
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(job_request_schema())


def test_schema_errors_report_paths():
    request = base_request()
    request["delivery"]["tier"] = "gold"
    request["inputs"]["files"][0]["role"] = "secret"
    request["unexpected"] = 1
    result = validate_job_request(request)
    paths = {error["path"] for error in result.errors}
    assert {"delivery.tier", "inputs.files[0].role", "$"} <= paths
    assert result.normalized is None


def test_not_an_object():
    assert codes(validate_job_request([1, 2])) == {"not_an_object"}


def test_description_only_requires_long_description():
    request = base_request()
    request["inputs"]["files"] = []
    request["inputs"]["description"] = "太短的描述"
    request["delivery"]["charts"] = "in_image"
    assert "description_too_short" in codes(validate_job_request(request))

    request["inputs"]["description"] = "   "
    assert "description_required" in codes(validate_job_request(request))

    request["inputs"]["description"] = LONG_DESCRIPTION
    result = validate_job_request(request)
    assert result.ok, result.errors
    assert result.derived["content_basis"] == "description_only"
    assert "框架或示意" in result.derived["content_notice"]
    assert result.derived["description_role"] == "user_statement"


def test_images_alone_do_not_count_as_documents():
    request = base_request()
    request["inputs"]["files"] = [request["inputs"]["files"][2]]
    assert "description_too_short" in codes(validate_job_request(request))


def test_slide_limits():
    request = base_request()
    request["slides"] = {"mode": "fixed", "count": 31}
    assert "slide_limit_exceeded" in codes(validate_job_request(request))

    request["slides"] = {"mode": "range", "min": 16, "max": 12}
    assert "slide_range_inverted" in codes(validate_job_request(request))

    request["slides"] = {"mode": "duration", "talk_minutes": 45}  # 45 min at 60 s/slide = 45 slides
    assert "slide_limit_exceeded" in codes(validate_job_request(request))

    request["slides"] = {"mode": "fixed", "count": 0}
    assert "schema.minimum" in codes(validate_job_request(request))

    request["slides"] = {"mode": "range", "min": 12}
    assert "schema.required" in codes(validate_job_request(request))


def test_duration_mode_uses_scenario_seconds_per_slide():
    request = base_request()
    request["scenario"] = "academic_paper_report"  # 70 s per slide
    request["slides"] = {"mode": "duration", "talk_minutes": 14}
    result = validate_job_request(request)
    assert result.ok, result.errors
    slides = result.derived["resolved_slides"]
    assert slides["target"] == 12
    assert slides["seconds_per_slide"] == 70
    assert slides["min"] <= 12 <= slides["max"] <= 30


def test_talk_length_mismatch_is_a_warning():
    request = base_request()
    request["slides"] = {"mode": "fixed", "count": 30, "talk_minutes": 5}
    result = validate_job_request(request)
    assert result.ok
    assert "slides_talk_mismatch" in warning_codes(result)


def test_native_editable_requires_preview_checkpoint_and_office_fonts():
    request = base_request()
    request["review"]["native_preview_checkpoint"] = False
    assert "native_preview_checkpoint_required" in codes(validate_job_request(request))

    request = base_request()
    request["delivery"]["font_policy"] = "master_glyphs"
    assert "office_font_policy_required" in codes(validate_job_request(request))

    request = base_request()
    del request["delivery"]["font_policy"]  # must be accepted explicitly, never defaulted
    assert "office_font_policy_required" in codes(validate_job_request(request))


def test_image_deck_defaults_and_no_native_checkpoint():
    request = base_request()
    request["delivery"] = {"tier": "image_deck"}
    request["review"] = {"native_preview_checkpoint": False}
    result = validate_job_request(request)
    assert result.ok, result.errors
    assert result.normalized["delivery"]["font_policy"] == "master_glyphs"
    assert result.normalized["delivery"]["charts"] == "in_image"
    assert result.derived["checkpoints"] == ["outline", "master"]


@pytest.mark.parametrize("profile", ["deep_academic", "academic_minimal"])
def test_dark_and_minimal_styles_need_explicit_choice(profile):
    request = base_request()
    request["style"]["profile"] = profile
    result = validate_job_request(request)
    assert "style_requires_explicit_choice" in codes(result)

    request["style"]["explicit_choice"] = True
    result = validate_job_request(request)
    assert result.ok, result.errors
    assert result.derived["style_requires_explicit_choice"] is True


@pytest.mark.parametrize("profile", ["graduate_defense_navy", "nsfc_review_light", "science_dark_contrast"])
def test_official_source_archetypes_are_not_offered(profile):
    request = base_request()
    request["style"]["profile"] = profile
    result = validate_job_request(request)
    assert "schema.enum" in codes(result)


def test_style_defaults_to_academic_light_even_when_scenario_default_is_an_archetype():
    request = base_request()
    del request["style"]
    result = validate_job_request(request)
    assert result.ok, result.errors
    assert result.normalized["style"]["profile"] == "academic_light"


def test_file_rules():
    request = base_request()
    files = request["inputs"]["files"]
    files.append(dict(files[0]))  # duplicate id
    files.append({"file_id": "f_09", "role": "chart_data", "name": "数据.docx"})
    files.append({"file_id": "f_10", "role": "supplementary", "name": "程序.exe"})
    files.append({"file_id": "f_11", "role": "logo", "name": "logo.png"})
    request["style"]["reference_file_ids"] = ["missing", "f_01"]
    result = validate_job_request(request)
    assert {
        "duplicate_file_id",
        "file_role_mismatch",
        "unsupported_file_type",
        "logo_rights_unconfirmed",
        "unknown_reference_file",
    } <= codes(result)
    assert "reference_role_mismatch" in warning_codes(result)

    request = base_request()
    request["inputs"]["files"].append({"file_id": "f_11", "role": "logo", "name": "logo.png"})
    request["inputs"]["logo_rights_confirmed"] = True
    assert validate_job_request(request).ok


def test_size_limits():
    limits = IntakeLimits(max_files=2, max_file_bytes=100, max_total_bytes=150)
    request = base_request()
    for entry in request["inputs"]["files"]:
        entry["size_bytes"] = 101
    result = validate_job_request(request, limits=limits)
    assert {"too_many_files", "file_too_large", "total_too_large"} <= codes(result)


def test_vectorize_rules():
    request = {
        "schema_version": SCHEMA_VERSION,
        "service": "vectorize",
        "inputs": {"files": [{"file_id": "f_01", "role": "supplementary", "name": "旧稿.pdf"}]},
        "delivery": {"tier": "image_deck"},
    }
    result = validate_job_request(request)
    assert {"vectorize_source_required", "vectorize_requires_native_tier"} <= codes(result)
    assert "description_required" not in codes(result)

    request["inputs"]["files"][0]["role"] = "vectorize_source"
    request["delivery"] = {"tier": "native_editable", "font_policy": "office_native_dengxian"}
    result = validate_job_request(request)
    assert result.ok, result.errors
    assert result.derived["content_basis"] == "vectorize_source"
    assert result.derived["resolved_slides"] is None


def test_new_deck_requires_scenario_and_slides():
    request = base_request()
    del request["scenario"]
    del request["slides"]
    assert "schema.required" in codes(validate_job_request(request))


def test_deadline_must_be_iso():
    request = base_request()
    request["deadline"] = "下周五"
    assert "invalid_deadline" in codes(validate_job_request(request))
    request["deadline"] = "2027-05-20T18:00:00+08:00"
    assert validate_job_request(request).ok


def test_input_is_not_mutated():
    request = base_request()
    del request["review"]
    snapshot = deepcopy(request)
    result = validate_job_request(request)
    assert request == snapshot
    assert result.normalized["review"]["native_preview_checkpoint"] is True


def test_cli(tmp_path, capsysbinary):
    path = tmp_path / "request.json"
    path.write_text(json.dumps(base_request(), ensure_ascii=False), encoding="utf-8")
    assert main([str(path)]) == 0
    payload = json.loads(capsysbinary.readouterr().out.decode("utf-8"))
    assert payload["ok"] is True

    bad = base_request()
    bad["delivery"]["font_policy"] = "master_glyphs"
    path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    assert main([str(path)]) == 1

    assert main(["--schema"]) == 0
    schema = json.loads(capsysbinary.readouterr().out.decode("utf-8").split("\n}\n")[-2] + "\n}")
    assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION
