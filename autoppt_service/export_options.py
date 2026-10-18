"""Form choices for the web front end, exported from the repository catalogs.

Scenarios come from ``PRESENTATION_PROFILES`` and styles from ``STYLE_PROFILES``.
Official-source-inspired school archetypes (``OFFICIAL_SOURCE_ARCHETYPE_IDS``)
are never offered: the service does not provide institution-flavoured styles,
users upload their own school template as a style reference instead.  When a
scenario's repository default is one of those archetypes, the form default
falls back to Academic Light (the v66 reference style) and the repository
default is still reported so the planner can see it.

The other form enums (service, tier, file roles, ...) live here too, so the
job-request schema and the front end share one list of choices.

    python -m autoppt_service.export_options --output options.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Sequence

from autoppt_service.repo_paths import ensure_autoppt_on_path

ensure_autoppt_on_path()
from autoppt_workflow.presentation.profiles import PRESENTATION_PROFILES  # noqa: E402  (read-only use)
from autopptskills.scripts.style_contracts import (  # noqa: E402  (read-only use)
    OFFICIAL_SOURCE_ARCHETYPE_IDS,
    STYLE_PROFILES,
)

OPTIONS_SCHEMA = "autoppt-export-options-v1"
DEFAULT_STYLE = "academic_light"
MAX_SLIDES = 30
MIN_SLIDES = 1
MIN_DESCRIPTION_CHARS = 200

SCENARIO_USE_WHEN: dict[str, str] = {
    "innovation_competition_defense": "大学生科创竞赛、创新创业类比赛答辩：讲清现实问题、技术创新、原型或实验证据与应用价值",
    "thesis_defense": "本科、硕士、博士学位论文答辩：研究问题、方法、主要结果、本人贡献与局限",
    "nsfc_application_defense": "国家自然科学基金等科研项目申请答辩：科学问题、核心假说、技术路线、可行性与风险",
    "nsfc_conclusion_defense": "国自然或其他科研项目结题答辩：计划与完成情况、代表性成果、未完成内容及原因",
    "academic_paper_report": "论文解读、文献汇报与学术报告：围绕原文图表区分直接观察与个人解释",
    "research_progress_report": "组会与阶段进展汇报：本期完成边界、当前证据、困难与下一步计划",
}

SCENARIO_LABELS_EN: dict[str, str] = {
    "innovation_competition_defense": "Innovation competition pitch",
    "thesis_defense": "Thesis defense",
    "nsfc_application_defense": "NSFC grant application defense",
    "nsfc_conclusion_defense": "NSFC / research project final review",
    "academic_paper_report": "Paper reading / academic talk",
    "research_progress_report": "Research progress / group meeting",
}

SCENARIO_USE_WHEN_EN: dict[str, str] = {
    "innovation_competition_defense": "Student innovation and entrepreneurship contests: the real problem, the technical "
                                      "innovation, prototype or experimental evidence, and the value of the application",
    "thesis_defense": "Bachelor's, master's and doctoral defenses: research question, method, main results, "
                      "your own contribution and the limitations",
    "nsfc_application_defense": "NSFC and other grant application defenses: scientific question, core hypothesis, "
                                "technical route, feasibility and risks",
    "nsfc_conclusion_defense": "Final reviews of NSFC or other projects: plan versus completion, representative results, "
                               "unfinished work and why",
    "academic_paper_report": "Paper readings, literature reports and academic talks, separating what the figures show "
                             "from your own interpretation",
    "research_progress_report": "Group meetings and progress reports: what was finished, current evidence, "
                                "difficulties and next steps",
}

STYLE_LABELS_ZH: dict[str, str] = {
    "academic_light": "学术浅色",
    "academic_minimal": "学术极简",
    "deep_academic": "深色学术",
    "editorial_warm": "暖调编辑",
    "engineering_blueprint": "工程蓝图",
    "clinical_clean": "医学清爽",
}

STYLE_USE_WHEN_ZH: dict[str, str] = {
    "academic_light": "通用学术报告、项目答辩、研究申请，以及专业背景不一的听众（默认，参考 v66 基准）",
    "academic_minimal": "短时论文答辩、简要研究汇报，以及证据较少、需要刻意留白突出重点的页面",
    "deep_academic": "研究答辩、海洋或航空航天类课题，以及光线较暗、深色背景更利于聚焦的会场",
    "editorial_warm": "创新项目、社会研究、设计评审，以及需要人文气质的学术汇报",
    "engineering_blueprint": "系统架构、机械、控制、机器人、制造与工程设计评审",
    "clinical_clean": "生物医学、健康、化学、生命科学，以及实验证据密集的汇报",
}

SERVICES = {"new_deck": {"label": "新建 PPT", "label_en": "New deck"},
            "vectorize": {"label": "PPT 矢量化", "label_en": "Make a deck editable"}}

DELIVERY_TIERS: dict[str, dict[str, str]] = {
    "planning_package": {"label": "规划包", "label_en": "Planning package", "repo_tier": "Planning package"},
    "image_deck": {"label": "图片版", "label_en": "Image deck", "repo_tier": "Image-only deck"},
    "native_editable": {"label": "原生可编辑版", "label_en": "Native editable deck", "repo_tier": "native-priority-approved"},
}

FILE_ROLES: dict[str, dict[str, Any]] = {
    "primary_source": {"label": "主资料（论文/报告/申请书）", "label_en": "Main source (thesis, report, proposal)", "document": True},
    "supplementary": {"label": "补充资料", "label_en": "Supplementary material", "document": True},
    "chart_data": {"label": "图表数据", "label_en": "Chart data", "document": True},
    "image_asset": {"label": "图片素材（实验图、照片、系统截图）", "label_en": "Images (figures, photos, screenshots)", "document": False},
    "keep_as_image": {"label": "原图保留（不让 ImageGen 重绘）", "label_en": "Keep as is (never redrawn by ImageGen)", "document": False},
    "logo": {"label": "Logo", "label_en": "Logo", "document": False},
    "style_reference": {"label": "风格参考（参考 PPT/截图/学校模板）", "label_en": "Style reference (deck, screenshot, school template)", "document": False},
    "vectorize_source": {"label": "待矢量化文件", "label_en": "Deck or pages to make editable", "document": False},
}

SLIDE_MODES = {"fixed": {"label": "固定页数", "label_en": "Fixed number of slides"},
               "range": {"label": "页数范围", "label_en": "Range of slides"},
               "duration": {"label": "按演讲时长推算", "label_en": "From the talk length"}}
LANGUAGES = {"zh-CN": {"label": "中文", "label_en": "Chinese"}, "en": {"label": "英文", "label_en": "English"},
             "zh-CN+en": {"label": "中英双语", "label_en": "Chinese and English"}}
AUDIENCES = ["答辩委员会", "评审专家", "导师与组员", "比赛评委", "投资人"]
AUDIENCES_EN = ["Defense committee", "Review panel", "Supervisor and group", "Competition judges", "Investors"]
DENSITIES = {"medium_high": {"label": "中高（默认）", "label_en": "Medium-high (default)"},
             "medium": {"label": "中", "label_en": "Medium"}, "low": {"label": "低", "label_en": "Low"}}
ASPECT_RATIOS = {"16:9": {"label": "16:9（默认）", "label_en": "16:9 (default)"}, "4:3": {"label": "4:3", "label_en": "4:3"}}
FONT_POLICIES: dict[str, dict[str, Any]] = {
    "office_native_yahei": {"label": "接受 Office 字体：微软雅黑", "label_en": "Accept an Office font: Microsoft YaHei",
                            "office_native": True},
    "office_native_dengxian": {"label": "接受 Office 字体：等线", "label_en": "Accept an Office font: DengXian",
                               "office_native": True},
    "master_glyphs": {"label": "保留母版字形（仅规划包/图片版）", "label_en": "Keep the master's lettering (planning package / image deck only)",
                      "office_native": False},
}
CHART_MODES = {
    "native_with_workbook": {"label": "原生图表（嵌入可编辑工作簿）", "label_en": "Native charts (with an editable workbook)"},
    "in_image": {"label": "图表作为页面图像的一部分", "label_en": "Charts as part of the slide image"},
    "none": {"label": "不需要图表", "label_en": "No charts"},
}
RETENTION_DAYS = (3, 7, 30)

DOCUMENT_ROLES = tuple(role for role, meta in FILE_ROLES.items() if meta["document"])
OFFICE_FONT_POLICIES = tuple(key for key, meta in FONT_POLICIES.items() if meta["office_native"])

_HEX_RE = re.compile(r"#([0-9A-Fa-f]{6})")


def _relative_luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def palette_swatches(palette: str) -> dict[str, list[str]]:
    """Split ``"background #F7F9FC/#FFFFFF; ink #172033"`` into named hex lists."""
    swatches: dict[str, list[str]] = {}
    for part in str(palette or "").split(";"):
        part = part.strip()
        if not part:
            continue
        name = part.split("#", 1)[0].strip() or f"colour_{len(swatches) + 1}"
        colours = [f"#{value.upper()}" for value in _HEX_RE.findall(part)]
        if colours:
            swatches[name] = colours
    return swatches


def is_dark_style(profile: dict[str, Any]) -> bool:
    background = palette_swatches(profile.get("palette", "")).get("background") or []
    return bool(background) and _relative_luminance(background[0][1:]) < 0.2


def offered_style_ids() -> tuple[str, ...]:
    return tuple(key for key in STYLE_PROFILES if key not in OFFICIAL_SOURCE_ARCHETYPE_IDS)


def style_requires_explicit_choice(style_id: str) -> bool:
    profile = STYLE_PROFILES[style_id]
    return bool(profile.get("intentional_minimal")) or is_dark_style(profile)


def style_options() -> list[dict[str, Any]]:
    options = []
    for key in offered_style_ids():
        profile = STYLE_PROFILES[key]
        dark = is_dark_style(profile)
        minimal = bool(profile.get("intentional_minimal"))
        options.append(
            {
                "id": key,
                "label": STYLE_LABELS_ZH.get(key, profile["label"]),
                "label_en": profile["label"],
                "use_when": STYLE_USE_WHEN_ZH.get(key, profile["use_when"]),
                "use_when_en": profile["use_when"],
                "is_default": key == DEFAULT_STYLE,
                "dark": dark,
                "intentional_minimal": minimal,
                "requires_explicit_choice": dark or minimal,
                "design_density": profile.get("design_density", ""),
                "palette": palette_swatches(profile.get("palette", "")),
            }
        )
    return options


def scenario_default_style(scenario_id: str) -> str:
    repo_style = PRESENTATION_PROFILES[scenario_id].get("style_profile") or DEFAULT_STYLE
    if repo_style in offered_style_ids() and not style_requires_explicit_choice(repo_style):
        return repo_style
    return DEFAULT_STYLE


def scenario_options() -> list[dict[str, Any]]:
    options = []
    for key, profile in PRESENTATION_PROFILES.items():
        low, high = profile["default_slide_range"]
        seconds = profile["default_duration_seconds"]
        repo_style = profile.get("style_profile") or DEFAULT_STYLE
        default_style = scenario_default_style(key)
        option = {
            "id": key,
            "label": profile["label"],
            "label_en": SCENARIO_LABELS_EN.get(key, key.replace("_", " ")),
            "use_when": SCENARIO_USE_WHEN.get(key, ""),
            "use_when_en": SCENARIO_USE_WHEN_EN.get(key, ""),
            "default_slide_range": [low, high],
            "default_duration_seconds": seconds,
            "default_talk_minutes_range": [round(low * seconds / 60, 1), round(high * seconds / 60, 1)],
            "default_style": default_style,
            "repo_style_profile": repo_style,
            "narrative": list(profile.get("narrative", [])),
            "forbidden": list(profile.get("forbidden", [])),
        }
        if default_style != repo_style:
            option["style_fallback_reason"] = "repository default is an official-source archetype, which the service does not offer"
        options.append(option)
    return options


def _labelled(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, value in mapping.items():
        row = {"id": key}
        row.update(value if isinstance(value, dict) else {"label": value})
        rows.append(row)
    return rows


def export_options() -> dict[str, Any]:
    return {
        "schema_version": OPTIONS_SCHEMA,
        "job_request_schema": "autoppt-job-request-v1",
        "scenarios": scenario_options(),
        "styles": style_options(),
        "default_style": DEFAULT_STYLE,
        "services": _labelled(SERVICES),
        "delivery_tiers": _labelled(DELIVERY_TIERS),
        "file_roles": _labelled(FILE_ROLES),
        "slide_modes": _labelled(SLIDE_MODES),
        "languages": _labelled(LANGUAGES),
        "audiences": list(AUDIENCES),
        "audiences_en": list(AUDIENCES_EN),
        "densities": _labelled(DENSITIES),
        "aspect_ratios": _labelled(ASPECT_RATIOS),
        "font_policies": _labelled(FONT_POLICIES),
        "chart_modes": _labelled(CHART_MODES),
        "retention_days": list(RETENTION_DAYS),
        "limits": {
            "min_slides": MIN_SLIDES,
            "max_slides": MAX_SLIDES,
            "min_description_chars_without_documents": MIN_DESCRIPTION_CHARS,
        },
        "notices": [
            "原生可编辑版与母版之间存在字体和局部图形差异，需经用户确认后接受；不承诺逐像素一致。",
            "原生可编辑版必须开启原生预览批准，并接受 Office 字体替代母版字形。",
            "不提供学校品牌风格；如需学校模板，请作为风格参考上传，并确认拥有 Logo/VI 使用权。",
        ],
        "notices_en": [
            "The native editable deck differs from the masters in fonts and some graphics; you accept the differences "
            "after reviewing a preview. Pixel-identical output is not promised.",
            "The native editable deck always needs your approval of the native preview and Office fonts in place of "
            "the masters' lettering.",
            "No school-branded styles are offered; upload your school template as a style reference and confirm you "
            "may use its logo and visual identity.",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export scenario and style options as front-end JSON.")
    parser.add_argument("--output", type=Path, help="write JSON here instead of stdout")
    args = parser.parse_args(argv)
    payload = json.dumps(export_options(), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.buffer.write(payload.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
