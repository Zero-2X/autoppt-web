"""One ImageGen sample slide per offered style, for the style picker in the order wizard.

Each style gets a one-slide job under ``--jobs-root``: the same fictional method slide (the test thesis
used for J-plan-real-001; content and data invented, and the slide says it is a sample) goes through the
service's own prompt building and Codex ``image_gen`` step, so the picture shows what that style really
produces.  The masters are copied to ``web/styles/<style>.jpg``.

    python scripts/build_style_samples.py --jobs-root D:/autoppt-jobs/_style-samples [--style deep_academic]

Uses the default Codex home unless ``--profiles`` is given; ``AUTOPPT_CODEX_BIN`` and
``AUTOPPT_CODEX_MODEL`` apply as for every run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autoppt_service.repo_paths import ensure_autoppt_on_path  # noqa: E402

ensure_autoppt_on_path()

SAMPLE_SLIDE = {
    "slide_id": "S01",
    "role": "method",
    "headline": "方法分三步：雷达外推、管网溢流、街区风险",
    "message": "三步融合把雷达外推、管网溢流与街区地形连成一条预测链。",
    "exact_text": [
        "方法分三步：雷达外推、管网溢流、街区风险",
        "① 用光流法外推 0–90 分钟雷达回波",
        "② 用简化管网水力模型计算节点溢流",
        "③ 图神经网络融合溢流与街区地形，输出风险等级（低、中、高）",
        "管网节点从 18,400 个简化到 2,310 个",
        "风格示例 · 虚构内容",
    ],
    "visual_brief": "横向三段机制图：雷达回波 → 节点溢流 → 街区风险等级；下方一条注释说明管网简化前后的节点数；"
                    "右下角用小字标注“风格示例 · 虚构内容”。",
    "evidence_summary": "虚构的测试论文第 5 节：三步流程、输出等级与管网简化规则及节点数变化。",
    "source_refs": ["ev_sample_sec005"],
}


def build_job(job_dir: Path, style: str) -> Path:
    from autoppt_service.planning import build_workflow_inputs

    job_dir.mkdir(parents=True, exist_ok=True)
    plan = {"deck": {"title": "基于多源数据融合的城市内涝短临预警方法研究（风格示例）", "use_case": "thesis defense"},
            "slides": [SAMPLE_SLIDE]}
    request = {"style": {"profile": style}, "delivery": {"font_policy": "office_native_yahei", "aspect_ratio": "16:9"}}
    build_workflow_inputs(job_dir, plan, request)
    return job_dir / "image-prompts.json"


def main(argv: list[str] | None = None) -> int:
    from PIL import Image

    from autoppt_service.codex_exec import load_profiles
    from autoppt_service.codex_imagegen_step import generate_masters
    from autoppt_service.export_options import offered_style_ids

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--jobs-root", type=Path, required=True)
    parser.add_argument("--style", action="append", help="only these styles (default: all offered)")
    parser.add_argument("--profiles", type=Path)
    parser.add_argument("--out", type=Path, default=ROOT / "web" / "styles")
    args = parser.parse_args(argv)
    styles = args.style or list(offered_style_ids())
    args.out.mkdir(parents=True, exist_ok=True)
    results = {}
    for style in styles:
        job = args.jobs_root / style
        prompts = build_job(job, style) if not (job / "image-prompts.json").exists() else job / "image-prompts.json"
        report = generate_masters(prompts, profiles=load_profiles(args.profiles))
        master = job / "assets" / "slides" / "S01.png"
        results[style] = {"verdict": report["handoff_verdict"], "issues": report.get("handoff_issues", [])}
        if report["handoff_verdict"] == "pass" and master.exists():
            with Image.open(master) as image:
                image = image.convert("RGB")
                image.thumbnail((1280, 1280))
                image.save(args.out / f"{style}.jpg", quality=82, optimize=True, progressive=True)
            results[style]["sample"] = f"web/styles/{style}.jpg"
            kinds_path = args.out / "samples.json"
            kinds = json.loads(kinds_path.read_text(encoding="utf-8")) if kinds_path.exists() else {}
            kinds[style] = {"kind": "imagegen", "source": f"scripts/build_style_samples.py ({job.name})"}
            kinds_path.write_text(json.dumps(kinds, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({style: results[style]}, ensure_ascii=False), flush=True)
    (args.jobs_root / "style-samples-report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                                               encoding="utf-8")
    return 0 if all(r["verdict"] == "pass" for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
