"""Number and chart checks on ImageGen masters before CP2.

ImageGen sometimes adds figures that are not in the plan (measured 2026-10-17:
S09 showed "提升 2.6 倍", derived from 47/18, despite the "no unsupported
number" rule).  Each master is read with OCR; every significant number that is
neither in that slide's exact text nor anywhere in the evidence is reported, so
the reviewer sees it in the CP2 summary instead of having to spot it.

ImageGen also draws whole data charts nobody asked for (measured 2026-10-18:
S08 showed an ablation bar chart, axis 0-1 without value labels, on a slide
whose plan cites no table).  Tick labels pass the number check, so a separate
check looks for a value axis (a run of tick labels) and reports it when the
slide's plan cites no table from chart-data.json.  Both only report; neither fixes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from autoppt_service.jobfiles import EVIDENCE_DIR, HANDOFF_DIR, read_json, write_json_atomic
from autoppt_service.planning import _numbers, _significant

OcrRunner = Callable[[Path, Path], dict[str, Any]]
QA_DIR = Path("workspace") / "master-qa"
# Tick labels are the smallest text on a slide: Windows OCR missed S08's 13 px "0.8" at master size and
# read the whole 0.0-0.8 run on a 3x copy.
UPSCALE = 3.0
UPSCALE_MAX_SIDE = 6000
_FULLWIDTH = str.maketrans("０１２３４５６７８９．，", "0123456789.,")
_AXIS_TOKENS = re.compile(r"km|m|%|分钟|分|秒|[\s\d.,·•一—–\-~()（）〈〉<>]")
_YEAR_RANGE = re.compile(r"((?:19|20)\d{2})\s*[—–\-~至到]+\s*((?:19|20)\d{2})")
_SPLIT_DECIMAL = re.compile(r"(\d)\s*[·•．。]\s*(\d)")
_SPLIT_GROUP = re.compile(r"(\d)\s*([.,])\s*(\d)")
_TICK = re.compile(r"^([-−–]?)\s*(\d+(?:[.,]\d+)?)\s*%?$")
_ALL_NUMBERS = re.compile(r"\d+(?:\.\d+)?")
_DISTANCE = re.compile(r"km|m\b|米")


def normalize_numbers(text: str) -> str:
    """Undo OCR splits inside numbers: '2 · 6' -> '2.6', '1, 286' -> '1,286', full-width digits."""
    text = text.translate(_FULLWIDTH)
    text = _SPLIT_DECIMAL.sub(lambda m: f"{m.group(1)}.{m.group(2)}", text)
    return _SPLIT_GROUP.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}", text)


def is_axis_line(text: str) -> bool:
    """Tick labels, legend ranges and scale bars: nothing but numbers, ranges and units."""
    return any(c.isdigit() for c in text) and not _AXIS_TOKENS.sub("", text)


def _with_year_ranges(text: str) -> set[str]:
    numbers = _numbers(text)
    for start, end in _YEAR_RANGE.findall(text):
        numbers |= {str(year) for year in range(int(start), int(end) + 1)}
    return numbers


def ocr_masters(job_dir: Path, masters: dict[str, Path], *, ocr: OcrRunner) -> dict[str, dict[str, Any]]:
    """One OCR pass per master, shared by the number and chart checks."""
    return {sid: ocr(Path(path), Path(job_dir) / QA_DIR / f"{sid}.json") for sid, path in sorted(masters.items())}


def check_master_numbers(job_dir: Path, masters: dict[str, Path], *, ocr: OcrRunner,
                         results: dict[str, dict[str, Any]] | None = None) -> list[str]:
    from autoppt_service.vectorize import join_cjk

    job_dir = Path(job_dir)
    results = results if results is not None else ocr_masters(job_dir, masters, ocr=ocr)
    plan = read_json(job_dir / HANDOFF_DIR / "deck-plan.json") or {}
    ledger = read_json(job_dir / EVIDENCE_DIR / "evidence-ledger.json") or {}
    brief = read_json(job_dir / HANDOFF_DIR / "planning-brief.json") or {}
    per_slide = {s["slide_id"]: _numbers(" ".join(s.get("exact_text", []))) for s in plan.get("slides", [])}
    evidence = _with_year_ranges(" ".join(str(i.get("fact_or_data", "")) for i in ledger.get("evidence_items", [])))
    cover = _numbers(" ".join(e["text"] for e in brief.get("exact_text_whitelist", [])))
    findings: list[str] = []
    report = []
    for slide_id in sorted(masters):
        lines = [normalize_numbers(join_cjk(line.get("text", ""))) for line in results[slide_id].get("lines", [])]
        text = " ".join(line for line in lines if not is_axis_line(line))
        seen = {n for n in _numbers(text) if _significant(n)}
        unexpected = sorted(seen - per_slide.get(slide_id, set()) - evidence - cover)
        report.append({"slide_id": slide_id, "numbers": sorted(seen), "unexpected": unexpected})
        if unexpected:
            findings.append(f"{slide_id}: master shows numbers not in the plan or evidence: {', '.join(unexpected)}")
    write_json_atomic(job_dir / QA_DIR / "numbers.json", {"slides": report, "findings": findings})
    return findings


# --- data charts without source data -------------------------------------------------------------

def _tick_value(text: str) -> float | None:
    """A lone tick label: '0.8', '0,8' (OCR comma), '60', '-20', '40%'; not ranges or labelled numbers."""
    match = _TICK.match(text.strip())
    if not match:
        return None
    digits = match.group(2)
    # '1,000' groups thousands; '0,8' is an OCR-read decimal point.
    digits = digits.replace(",", "") if re.fullmatch(r"\d{1,3},\d{3}", digits) else digits.replace(",", ".")
    return -float(digits) if match.group(1) else float(digits)


def _even_scale(points: list[tuple[float, float]]) -> bool:
    """``(position, value)`` pairs on one linear scale: values rise with position at a near-constant px per unit.

    OCR skips some ticks, so neighbours are compared by ratio rather than by a fixed step."""
    ratios = []
    for (p0, v0), (p1, v1) in zip(points, points[1:]):
        if v1 <= v0 or p1 <= p0:
            return False
        ratios.append((p1 - p0) / (v1 - v0))
    median = sorted(ratios)[len(ratios) // 2]
    return all(abs(r - median) <= 0.15 * median for r in ratios)


def _is_years(values: list[float]) -> bool:
    return all(v.is_integer() and 1900 <= v <= 2100 for v in values)


def find_value_axes(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Runs of tick labels that make a chart axis, from OCR lines ``{"text", "bbox"}`` (normalised text).

    A vertical axis is >= 3 right-aligned lone numbers whose value falls evenly from top to bottom (a
    downward list of years is a timeline).  A horizontal axis is >= 3 lone numbers on one baseline, or one
    OCR line of >= 4 numbers, rising evenly left to right and not only years.  Ranges ('40 一 60'),
    labelled numbers ('46 场') and scale bars ('0 5 10 km') are not ticks."""
    ticks = []
    for line in lines:
        box = line.get("bbox")
        value = _tick_value(line["text"])
        if box and value is not None:
            ticks.append({"value": value, "text": line["text"], "bbox": box, "h": box["h"],
                          "right": box["x"] + box["w"], "cx": box["x"] + box["w"] / 2, "cy": box["y"] + box["h"] / 2})

    def aligned(key: str) -> list[list[dict[str, Any]]]:
        found: list[list[dict[str, Any]]] = []
        for tick in sorted(ticks, key=lambda t: t[key]):
            if found and abs(found[-1][-1][key] - tick[key]) <= max(4.0, 0.6 * tick["h"]):
                found[-1].append(tick)
            else:
                found.append([tick])
        return found

    def axis(orientation: str, texts: list[str], values: list[float], boxes: list[dict[str, float]]) -> dict[str, Any]:
        x0, y0 = min(b["x"] for b in boxes), min(b["y"] for b in boxes)
        x1, y1 = max(b["x"] + b["w"] for b in boxes), max(b["y"] + b["h"] for b in boxes)
        return {"orientation": orientation, "ticks": texts, "min": min(values), "max": max(values),
                "bbox": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}}

    axes = []
    for group in aligned("right"):
        run = sorted(group, key=lambda t: -t["cy"])  # bottom to top
        values = [t["value"] for t in run]
        if len(set(values)) >= 3 and _even_scale([(-t["cy"], t["value"]) for t in run]):
            axes.append(axis("vertical", [t["text"] for t in run], values, [t["bbox"] for t in run]))
    for group in aligned("cy"):
        run = sorted(group, key=lambda t: t["cx"])
        values = [t["value"] for t in run]
        if len(set(values)) >= 3 and not _is_years(values) and _even_scale([(t["cx"], t["value"]) for t in run]):
            axes.append(axis("horizontal", [t["text"] for t in run], values, [t["bbox"] for t in run]))
    for line in lines:
        box, text = line.get("bbox"), line["text"]
        values = [float(n) for n in _ALL_NUMBERS.findall(text)]
        if box and len(values) >= 4 and is_axis_line(text) and not _DISTANCE.search(text) and not _is_years(values) \
                and _even_scale([(float(i), v) for i, v in enumerate(values)]):
            axes.append(axis("horizontal", [text], values, [box]))
    return axes


def _chart_context(lines: list[dict[str, Any]], axis: dict[str, Any], limit: int = 2) -> list[str]:
    """Text just above the axis (a chart title, a legend), so the reviewer can find the chart."""
    box = axis["bbox"]
    span = max(box["h"], box["w"])
    # A title sits over the plot, which starts at the axis: S08's left neighbour panel ("历史内涝") is not context.
    left, right = box["x"] - 2 * box["w"], box["x"] + box["w"] + span * 2.0
    near = []
    for line in lines:
        other, text = line.get("bbox"), line["text"]
        if not other or not text or is_axis_line(text):
            continue
        bottom = other["y"] + other["h"]
        if box["y"] - span <= bottom <= box["y"] + other["h"] and other["x"] < right and other["x"] + other["w"] > left:
            near.append((abs(box["y"] - bottom), other["y"], text))
    nearest: dict[str, tuple[float, float]] = {}
    for distance, top, text in sorted(near):
        nearest.setdefault(text, (distance, top))  # both OCR passes may read the same title
    return sorted(list(nearest)[:limit], key=lambda text: nearest[text][1])  # top to bottom


def _lines(result: dict[str, Any], scale: float = 1.0) -> list[dict[str, Any]]:
    from autoppt_service.vectorize import join_cjk

    lines = []
    for line in result.get("lines", []):
        box = line.get("bbox")
        lines.append({"text": normalize_numbers(join_cjk(line.get("text", ""))).strip(),
                      "bbox": {k: box[k] / scale for k in ("x", "y", "w", "h")} if box else None})
    return lines


def _upscaled(master: Path, target: Path) -> tuple[Path, float]:
    from PIL import Image

    with Image.open(master) as image:
        scale = min(UPSCALE, UPSCALE_MAX_SIDE / max(image.size))
        if scale <= 1.2:
            return master, 1.0
        target.parent.mkdir(parents=True, exist_ok=True)
        image.convert("RGB").resize((round(image.width * scale), round(image.height * scale)),
                                    Image.LANCZOS).save(target)
    return target, scale


def slide_tables(slide: dict[str, Any], tables: list[dict[str, Any]]) -> list[str]:
    """chart-data.json tables the slide's plan cites, as ``ev_<table_id>`` or ``<table_id>``."""
    refs = {*slide.get("evidence_refs", []), *slide.get("source_refs", [])}
    refs |= {ref[3:] for ref in refs if ref.startswith("ev_")}
    return [t["table_id"] for t in tables if t.get("table_id") in refs]


def check_master_charts(job_dir: Path, masters: dict[str, Path], *, ocr: OcrRunner,
                        results: dict[str, dict[str, Any]] | None = None) -> list[str]:
    """Report masters that draw a data chart (a value axis) on a slide whose plan cites no chart-data table.

    Slides that cite a table are skipped.  The others get a second OCR pass on an upscaled copy when the
    first pass found no axis."""
    job_dir = Path(job_dir)
    plan = read_json(job_dir / HANDOFF_DIR / "deck-plan.json") or {}
    tables = (read_json(job_dir / EVIDENCE_DIR / "chart-data.json") or {}).get("tables", [])
    slides = {s["slide_id"]: s for s in plan.get("slides", [])}
    findings: list[str] = []
    report = []
    for slide_id, path in sorted(masters.items()):
        entry: dict[str, Any] = {"slide_id": slide_id, "tables": slide_tables(slides.get(slide_id, {}), tables),
                                 "axes": [], "upscaled": False}
        report.append(entry)
        if entry["tables"]:
            continue
        result = results[slide_id] if results and slide_id in results else \
            ocr(Path(path), job_dir / QA_DIR / f"{slide_id}.json")
        lines = _lines(result)
        axes = find_value_axes(lines)
        if not axes:
            image, scale = _upscaled(Path(path), job_dir / QA_DIR / f"{slide_id}-upscaled.png")
            if scale > 1.0:
                upscaled = _lines(ocr(image, job_dir / QA_DIR / f"{slide_id}-upscaled.json"), scale)
                axes = find_value_axes(upscaled)
                lines += upscaled  # either pass may have read the chart title
                entry["upscaled"] = True
        for found in axes:
            found["context"] = _chart_context(lines, found)
            found["bbox"] = {k: round(v) for k, v in found["bbox"].items()}
        entry["axes"] = axes
        if axes:
            near = f", near “{' / '.join(axes[0]['context'])}”" if axes[0]["context"] else ""
            findings.append(f"{slide_id}: master draws a data chart (axis {axes[0]['min']:g}–{axes[0]['max']:g}{near}) "
                            "but the slide's plan cites no table in chart-data.json; its values have no source data")
    write_json_atomic(job_dir / QA_DIR / "charts.json", {"slides": report, "findings": findings})
    return findings
