from __future__ import annotations

import json

from autoppt_service.export_options import (
    export_options,
    is_dark_style,
    main,
    palette_swatches,
    scenario_options,
    style_options,
)
from autoppt_workflow.presentation.profiles import PRESENTATION_PROFILES
from autopptskills.scripts.style_contracts import OFFICIAL_SOURCE_ARCHETYPE_IDS, STYLE_PROFILES


def test_scenarios_cover_all_six_profiles_with_defaults():
    options = scenario_options()
    assert [option["id"] for option in options] == list(PRESENTATION_PROFILES)
    assert len(options) == 6
    offered = {style["id"] for style in style_options()}
    for option in options:
        profile = PRESENTATION_PROFILES[option["id"]]
        assert option["label"] == profile["label"]
        assert option["use_when"]
        assert option["default_slide_range"] == profile["default_slide_range"]
        assert option["default_duration_seconds"] == profile["default_duration_seconds"]
        assert option["default_style"] in offered
        assert option["repo_style_profile"] == profile["style_profile"]


def test_archetype_scenario_defaults_fall_back_to_academic_light():
    thesis = next(option for option in scenario_options() if option["id"] == "thesis_defense")
    assert thesis["repo_style_profile"] == "graduate_defense_navy"
    assert thesis["default_style"] == "academic_light"
    assert "style_fallback_reason" in thesis


def test_styles_exclude_official_source_archetypes():
    ids = [style["id"] for style in style_options()]
    assert ids
    assert not set(ids) & set(OFFICIAL_SOURCE_ARCHETYPE_IDS)
    assert set(ids) == set(STYLE_PROFILES) - set(OFFICIAL_SOURCE_ARCHETYPE_IDS)


def test_style_flags():
    styles = {style["id"]: style for style in style_options()}
    assert styles["academic_light"]["is_default"] is True
    assert styles["academic_light"]["requires_explicit_choice"] is False
    assert styles["deep_academic"]["dark"] is True
    assert styles["deep_academic"]["requires_explicit_choice"] is True
    assert styles["academic_minimal"]["intentional_minimal"] is True
    assert styles["academic_minimal"]["requires_explicit_choice"] is True
    for style in styles.values():
        assert style["label"] and style["use_when"]
        assert style["label"] != style["label_en"]  # Chinese label present
        assert style["palette"]["background"][0].startswith("#")


def test_palette_parsing_and_darkness():
    swatches = palette_swatches("background #101827/#172235; ink #F3F6FA")
    assert swatches == {"background": ["#101827", "#172235"], "ink": ["#F3F6FA"]}
    assert is_dark_style({"palette": "background #101827"})
    assert not is_dark_style({"palette": "background #F7F9FC"})


def test_export_is_json_and_cli_writes_file(tmp_path):
    payload = export_options()
    assert payload["schema_version"] == "autoppt-export-options-v1"
    assert payload["limits"]["max_slides"] == 30
    assert {tier["id"] for tier in payload["delivery_tiers"]} == {"planning_package", "image_deck", "native_editable"}
    json.dumps(payload, ensure_ascii=False)

    target = tmp_path / "options.json"
    assert main(["--output", str(target)]) == 0
    assert json.loads(target.read_text(encoding="utf-8"))["styles"] == payload["styles"]
