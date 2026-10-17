"""Schema and validation for ``autoppt-job-request-v1``.

The front end, the backend and the Worker share this one contract.  Validation
runs in two layers: a JSON Schema for shape and enums, then the form rules from
the development checklist (section 3.3):

* without uploaded documents the description is the only source, so it must be
  long enough, and everything built from it is labelled framework/illustrative;
* slide counts stay within the service limit (fixed, range or derived from the
  talk length and the scenario's seconds per slide);
* ``native_editable`` needs the native preview checkpoint (it can never be
  skipped) and an explicit acceptance of Office fonts instead of master glyphs;
* dark and intentionally minimal styles are only valid as an explicit choice;
* official-source school archetypes are not offered, and a Logo needs the
  user's confirmation that they may use it.

Errors block the order; warnings are shown to the user but do not.

    python -m autoppt_service.job_request request.json
    python -m autoppt_service.job_request --schema
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePath
from typing import Any, Sequence

from autoppt_service.repo_paths import ensure_autoppt_on_path

ensure_autoppt_on_path()
from jsonschema import Draft202012Validator  # noqa: E402

from autoppt_service.export_options import (  # noqa: E402
    ASPECT_RATIOS,
    CHART_MODES,
    DENSITIES,
    DELIVERY_TIERS,
    DOCUMENT_ROLES,
    FILE_ROLES,
    FONT_POLICIES,
    LANGUAGES,
    MAX_SLIDES,
    MIN_DESCRIPTION_CHARS,
    OFFICE_FONT_POLICIES,
    RETENTION_DAYS,
    SERVICES,
    SLIDE_MODES,
    offered_style_ids,
    scenario_default_style,
    style_requires_explicit_choice,
)
from autoppt_service.parsers.base import EXTENSION_KINDS, ROLE_FILE_KINDS, IntakeLimits  # noqa: E402
from autoppt_workflow.presentation.profiles import PRESENTATION_PROFILES  # noqa: E402  (read-only use)

SCHEMA_VERSION = "autoppt-job-request-v1"
VALIDATION_SCHEMA = "autoppt-job-request-validation-v1"
MAX_TALK_MINUTES = 180
# Pacing outside this band is shown as a warning, not an error.
MIN_SECONDS_PER_SLIDE = 20
MAX_SECONDS_PER_SLIDE = 180


_SHORT_TEXT = {"type": "string", "maxLength": 300}
_FLAG = {"type": "boolean"}


def _object(properties: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema


def job_request_schema() -> dict[str, Any]:
    file_entry = _object(
        {
            "file_id": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
            "role": {"enum": list(FILE_ROLES)},
            "name": {"type": "string", "minLength": 1, "maxLength": 255},
            "sha256": {"type": "string", "pattern": r"^[0-9a-fA-F]{64}$"},
            "size_bytes": {"type": "integer", "minimum": 0},
            "note": _SHORT_TEXT,
        },
        ["file_id", "role", "name"],
    )
    slides = _object(
        {
            "mode": {"enum": list(SLIDE_MODES)},
            "count": {"type": "integer", "minimum": 1},
            "min": {"type": "integer", "minimum": 1},
            "max": {"type": "integer", "minimum": 1},
            "talk_minutes": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_TALK_MINUTES},
        },
        ["mode"],
    )
    slides["allOf"] = [
        {"if": {"properties": {"mode": {"const": "fixed"}}}, "then": {"required": ["count"]}},
        {"if": {"properties": {"mode": {"const": "range"}}}, "then": {"required": ["min", "max"]}},
        {"if": {"properties": {"mode": {"const": "duration"}}}, "then": {"required": ["talk_minutes"]}},
    ]
    schema = _object(
        {
            "schema_version": {"const": SCHEMA_VERSION},
            "service": {"enum": list(SERVICES)},
            "inputs": _object(
                {
                    "files": {"type": "array", "items": file_entry},
                    "description": {"type": "string", "maxLength": 20000},
                    "must_include": {"type": "array", "maxItems": 30, "items": _SHORT_TEXT},
                    "must_avoid": {"type": "array", "maxItems": 30, "items": _SHORT_TEXT},
                    "logo_rights_confirmed": _FLAG,
                }
            ),
            "scenario": {"enum": list(PRESENTATION_PROFILES)},
            "audience": {"type": "string", "maxLength": 100},
            "language": {"enum": list(LANGUAGES)},
            "slides": slides,
            "style": _object(
                {
                    "profile": {"enum": list(offered_style_ids())},
                    "explicit_choice": _FLAG,
                    "density": {"enum": list(DENSITIES)},
                    "reference_file_ids": {"type": "array", "items": {"type": "string"}},
                }
            ),
            "cover": _object(
                {
                    key: {"type": "string", "maxLength": 200}
                    for key in ("title", "subtitle", "author", "advisor", "institution", "date")
                }
            ),
            "delivery": _object(
                {
                    "tier": {"enum": list(DELIVERY_TIERS)},
                    "aspect_ratio": {"enum": list(ASPECT_RATIOS)},
                    "font_policy": {"enum": list(FONT_POLICIES)},
                    "charts": {"enum": list(CHART_MODES)},
                    "speaker_notes": _FLAG,
                    "anticipated_questions": _FLAG,
                },
                ["tier"],
            ),
            "review": _object(
                {"outline_checkpoint": _FLAG, "master_checkpoint": _FLAG, "native_preview_checkpoint": _FLAG}
            ),
            "privacy": _object(
                {"retention_days": {"enum": list(RETENTION_DAYS)}, "allow_case_study": _FLAG}
            ),
            "deadline": {"type": "string", "maxLength": 40},
            "vectorize": _object(
                {
                    "pages": {"type": "string", "maxLength": 200, "pattern": r"^[0-9,，\- ]*$"},
                    "depth": {"enum": ["text", "text_shapes", "text_shapes_charts"]},
                    "keep_photos_as_images": _FLAG,
                }
            ),
        },
        ["schema_version", "service", "inputs", "delivery"],
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://autoppt.local/schemas/{SCHEMA_VERSION}.json"
    schema["title"] = "AutoPPT job request v1"
    schema["allOf"] = [
        {
            "if": {"properties": {"service": {"const": "new_deck"}}},
            "then": {"required": ["scenario", "slides"]},
        }
    ]
    return schema


_VALIDATOR = Draft202012Validator(job_request_schema())


@dataclass
class JobRequestValidation:
    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    normalized: dict[str, Any] | None = None
    derived: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, code: str, path: str, message: str) -> None:
        self.errors.append({"code": code, "path": path, "message": message})

    def warn(self, code: str, path: str, message: str) -> None:
        self.warnings.append({"code": code, "path": path, "message": message})

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": VALIDATION_SCHEMA,
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "normalized": self.normalized,
            "derived": self.derived,
        }


def _json_path(parts: Sequence[Any]) -> str:
    text = ""
    for part in parts:
        text += f"[{part}]" if isinstance(part, int) else (f".{part}" if text else str(part))
    return text or "$"


def _schema_message(error: Any) -> str:
    if error.validator == "enum":
        return f"取值无效：{error.instance!r}；可选：{', '.join(map(str, error.validator_value))}"
    if error.validator == "required":
        return f"缺少必填字段：{error.message.split(' is a required property')[0]}"
    if error.validator == "additionalProperties":
        return f"包含未定义字段：{error.message}"
    return error.message


def _apply_defaults(request: dict[str, Any]) -> dict[str, Any]:
    data = deepcopy(request)
    inputs = data["inputs"]
    inputs.setdefault("files", [])
    inputs.setdefault("description", "")
    inputs.setdefault("must_include", [])
    inputs.setdefault("must_avoid", [])
    inputs.setdefault("logo_rights_confirmed", False)
    data.setdefault("language", "zh-CN")
    data.setdefault("cover", {})
    style = data.setdefault("style", {})
    scenario = data.get("scenario")
    style.setdefault("profile", scenario_default_style(scenario) if scenario else "academic_light")
    style.setdefault("explicit_choice", False)
    style.setdefault("density", "medium_high")
    style.setdefault("reference_file_ids", [])
    delivery = data["delivery"]
    delivery.setdefault("aspect_ratio", "16:9")
    # native_editable must state its font policy itself; the others keep master glyphs.
    if delivery["tier"] != "native_editable":
        delivery.setdefault("font_policy", "master_glyphs")
    delivery.setdefault("charts", "native_with_workbook" if delivery["tier"] == "native_editable" else "in_image")
    delivery.setdefault("speaker_notes", False)
    delivery.setdefault("anticipated_questions", False)
    review = data.setdefault("review", {})
    review.setdefault("outline_checkpoint", True)
    review.setdefault("master_checkpoint", True)
    review.setdefault("native_preview_checkpoint", True)
    privacy = data.setdefault("privacy", {})
    privacy.setdefault("retention_days", 7)
    privacy.setdefault("allow_case_study", False)
    return data


def _check_files(data: dict[str, Any], result: JobRequestValidation, limits: IntakeLimits) -> list[dict[str, Any]]:
    files = data["inputs"]["files"]
    if len(files) > limits.max_files:
        result.error("too_many_files", "inputs.files", f"最多上传 {limits.max_files} 个文件，当前 {len(files)} 个")
    seen: set[str] = set()
    total = 0
    for index, entry in enumerate(files):
        path = f"inputs.files[{index}]"
        file_id = entry["file_id"]
        if file_id in seen:
            result.error("duplicate_file_id", f"{path}.file_id", f"file_id 重复：{file_id}")
        seen.add(file_id)
        suffix = PurePath(entry["name"]).suffix.lower()
        kind = EXTENSION_KINDS.get(suffix)
        if kind is None:
            result.error("unsupported_file_type", f"{path}.name", f"不支持的文件类型：{suffix or '（无扩展名）'}")
        elif kind not in ROLE_FILE_KINDS[entry["role"]]:
            result.error(
                "file_role_mismatch",
                f"{path}.role",
                f"{entry['name']} 不能作为“{FILE_ROLES[entry['role']]['label']}”上传",
            )
        size = entry.get("size_bytes")
        if size is not None:
            total += size
            if size > limits.max_file_bytes:
                result.error(
                    "file_too_large",
                    f"{path}.size_bytes",
                    f"{entry['name']} 超过单文件上限 {limits.max_file_bytes // (1024 * 1024)} MB",
                )
    if total > limits.max_total_bytes:
        result.error(
            "total_too_large", "inputs.files", f"文件总大小超过上限 {limits.max_total_bytes // (1024 * 1024)} MB"
        )
    return files


def _resolve_slides(data: dict[str, Any], result: JobRequestValidation) -> dict[str, Any] | None:
    slides = data.get("slides")
    if not slides:
        return None
    seconds = PRESENTATION_PROFILES[data["scenario"]]["default_duration_seconds"] if data.get("scenario") else 60
    mode = slides["mode"]
    talk = slides.get("talk_minutes")
    if mode == "fixed":
        low = high = slides["count"]
    elif mode == "range":
        low, high = slides["min"], slides["max"]
        if low > high:
            result.error("slide_range_inverted", "slides", f"页数范围无效：最少 {low} 页大于最多 {high} 页")
            return None
    else:
        target = max(1, round(talk * 60 / seconds))
        if target > MAX_SLIDES:
            result.error(
                "slide_limit_exceeded",
                "slides.talk_minutes",
                f"按每页约 {seconds} 秒推算需要 {target} 页，超过上限 {MAX_SLIDES} 页；请缩短时长或改用固定页数",
            )
            return None
        low = max(1, math.floor(target * 0.85))
        high = min(MAX_SLIDES, max(low, math.ceil(target * 1.15)))
        return {"min": low, "max": high, "target": target, "seconds_per_slide": seconds, "derived_from": "talk_minutes"}
    if high > MAX_SLIDES:
        result.error("slide_limit_exceeded", "slides", f"页数上限为 {MAX_SLIDES} 页，当前 {high} 页")
        return None
    if talk:
        fastest = talk * 60 / high
        slowest = talk * 60 / low
        if fastest < MIN_SECONDS_PER_SLIDE or slowest > MAX_SECONDS_PER_SLIDE:
            result.warn(
                "slides_talk_mismatch",
                "slides",
                f"{talk:g} 分钟讲 {low}–{high} 页，每页约 {fastest:.0f}–{slowest:.0f} 秒，节奏可能不合理",
            )
    return {"min": low, "max": high, "target": round((low + high) / 2), "seconds_per_slide": seconds, "derived_from": mode}


def _non_space_len(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def validate_job_request(request: Any, *, limits: IntakeLimits | None = None) -> JobRequestValidation:
    limits = limits or IntakeLimits()
    result = JobRequestValidation()
    if not isinstance(request, dict):
        result.error("not_an_object", "$", "需求必须是 JSON 对象")
        return result
    schema_errors = sorted(_VALIDATOR.iter_errors(request), key=lambda e: list(e.absolute_path))
    for error in schema_errors:
        result.error(f"schema.{error.validator}", _json_path(list(error.absolute_path)), _schema_message(error))
    if schema_errors:
        return result

    data = _apply_defaults(request)
    result.normalized = data
    service = data["service"]
    inputs = data["inputs"]
    delivery = data["delivery"]
    review = data["review"]
    style = data["style"]
    tier = delivery["tier"]

    files = _check_files(data, result, limits)
    roles = {entry["file_id"]: entry["role"] for entry in files}
    documents = [entry for entry in files if entry["role"] in DOCUMENT_ROLES]
    description = inputs["description"].strip()

    # Description: the only content source when no document is uploaded.
    content_basis = "documents" if documents else "description_only"
    if service == "new_deck" and not documents:
        if not description:
            result.error("description_required", "inputs.description", "未上传文档时必须填写描述文字")
        elif _non_space_len(description) < MIN_DESCRIPTION_CHARS:
            result.error(
                "description_too_short",
                "inputs.description",
                f"未上传文档时描述至少 {MIN_DESCRIPTION_CHARS} 字（当前 {_non_space_len(description)} 字）："
                "请补充主题、目的、核心结论和关键数据",
            )

    if service == "vectorize":
        content_basis = "vectorize_source"
        if "vectorize_source" not in roles.values():
            result.error("vectorize_source_required", "inputs.files", "矢量化服务需要上传至少一个待矢量化文件")
        if tier != "native_editable":
            result.error("vectorize_requires_native_tier", "delivery.tier", "矢量化服务只交付原生可编辑版")

    if "logo" in roles.values() and not inputs["logo_rights_confirmed"]:
        result.error("logo_rights_unconfirmed", "inputs.logo_rights_confirmed", "使用 Logo 前需要确认拥有使用权")

    for index, file_id in enumerate(style["reference_file_ids"]):
        path = f"style.reference_file_ids[{index}]"
        if file_id not in roles:
            result.error("unknown_reference_file", path, f"风格参考引用了不存在的文件：{file_id}")
        elif roles[file_id] != "style_reference":
            result.warn("reference_role_mismatch", path, f"{file_id} 未标注为风格参考，将按风格参考使用")

    resolved_slides = _resolve_slides(data, result)

    profile = style["profile"]
    explicit_required = style_requires_explicit_choice(profile)
    if explicit_required and not style["explicit_choice"]:
        result.error(
            "style_requires_explicit_choice",
            "style.explicit_choice",
            f"深色或极简风格（{profile}）需要用户主动选择，请确认后再提交",
        )

    if tier == "native_editable":
        if not review["native_preview_checkpoint"]:
            result.error(
                "native_preview_checkpoint_required",
                "review.native_preview_checkpoint",
                "原生可编辑版必须开启原生预览批准，不能关闭",
            )
        if delivery.get("font_policy") not in OFFICE_FONT_POLICIES:
            result.error(
                "office_font_policy_required",
                "delivery.font_policy",
                "原生可编辑版必须接受 Office 字体（微软雅黑或等线）替代母版字形",
            )
    elif delivery["charts"] == "native_with_workbook":
        result.warn("native_charts_need_native_tier", "delivery.charts", "原生图表只在原生可编辑版中交付，本档位按图像呈现")

    if (
        service == "new_deck"
        and delivery["charts"] == "native_with_workbook"
        and tier == "native_editable"
        and "chart_data" not in roles.values()
    ):
        result.warn("chart_data_missing", "inputs.files", "未上传图表数据（xlsx/csv）；原生图表只能使用文档中可核对的数据")

    deadline = data.get("deadline")
    if deadline:
        try:
            datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        except ValueError:
            result.error("invalid_deadline", "deadline", "截止时间需要 ISO 8601 格式，例如 2027-05-20T18:00:00+08:00")

    result.derived = {
        "content_basis": content_basis,
        "content_notice": (
            "只凭描述生成：内容必须标注为“框架或示意”，不能编造数据"
            if content_basis == "description_only"
            else ""
        ),
        "description_role": "user_statement" if content_basis == "description_only" else "focus_notes",
        "resolved_slides": resolved_slides,
        "style_profile": profile,
        "style_requires_explicit_choice": explicit_required,
        "repo_tier": DELIVERY_TIERS[tier]["repo_tier"],
        "checkpoints": [
            name
            for name, enabled in (
                ("outline", review["outline_checkpoint"]),
                ("master", review["master_checkpoint"] and tier != "planning_package"),
                ("native_preview", tier == "native_editable"),
            )
            if enabled
        ],
        "keep_as_image_file_ids": [fid for fid, role in roles.items() if role == "keep_as_image"],
        "addons": [name for name in ("speaker_notes", "anticipated_questions") if delivery[name]],
    }
    return result


def load_job_request(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an autoppt-job-request-v1 JSON file.")
    parser.add_argument("request", nargs="?", type=Path)
    parser.add_argument("--schema", action="store_true", help="print the JSON Schema and exit")
    args = parser.parse_args(argv)
    if args.schema:
        payload: Any = job_request_schema()
        code = 0
    elif args.request:
        result = validate_job_request(load_job_request(args.request))
        payload, code = result.to_dict(), (0 if result.ok else 1)
    else:
        parser.error("give a request file or --schema")
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
