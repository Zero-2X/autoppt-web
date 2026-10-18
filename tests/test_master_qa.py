"""Masters QA before CP2: data charts that ImageGen drew without source data."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from autoppt_service.jobfiles import read_json, write_json_atomic
from autoppt_service.master_qa import check_master_charts, check_master_numbers, find_value_axes, ocr_masters

TABLE = {"table_id": "f_01_table-001", "header": ["方法", "命中率"], "rows": [["本文方法", "0.82"]],
         "chart_candidate": True}


def line(text: str, x: float, y: float, w: float = 40, h: float = 15) -> dict:
    return {"text": text, "bbox": {"x": x, "y": y, "w": w, "h": h}}


def make_job(tmp_path: Path, slides: list[dict]) -> tuple[Path, dict[str, Path]]:
    job = tmp_path / "job"
    write_json_atomic(job / "workspace/presentation_handoff/deck-plan.json", {"slides": slides})
    write_json_atomic(job / "workspace/evidence_workspace/chart-data.json",
                      {"schema_version": "intake-chart-data-v1", "tables": [TABLE]})
    (job / "assets/slides").mkdir(parents=True)
    masters = {}
    for slide in slides:
        masters[slide["slide_id"]] = job / "assets/slides" / f"{slide['slide_id']}.png"
        Image.new("RGB", (400, 225)).save(masters[slide["slide_id"]])
    return job, masters


# What Windows OCR returned for J-plan-real-001 S08 (2026-10-18): the chart title and legend at master size,
# the tick labels only on the 3x copy (coordinates there are 3x; OCR read two decimal points as commas).
S08_MASTER = [line("本 人 负 责 消 融 实 验", 963, 135, 285, 54), line("历 史 内 涝", 837, 224, 66, 18),
              line("不 同 模 型 设 置 的 预 警 效 果 对 比", 1016, 221, 250, 20),
              line("0 准 确 率 到 FI 值", 1165, 267, 121, 15), line("完 整 模 型 去 除", 1005, 453, 116, 15)]
S08_UPSCALED = [line("0,8", 2903, 893, 61, 35), line("0.6", 2904, 994, 60, 36), line("0.4", 2903, 1095, 60, 36),
                line("0,2", 2903, 1196, 61, 37), line("0.0", 2903, 1298, 60, 36)]


def test_invented_chart_on_a_slide_without_table_is_reported(tmp_path):
    job, masters = make_job(tmp_path, [
        {"slide_id": "S07", "evidence_refs": ["ev_f_01_table-001", "ev_f_01_sec006"], "source_refs": []},
        {"slide_id": "S08", "evidence_refs": ["ev_f_01_sec007"], "source_refs": ["ev_f_01_sec007"]}])
    calls = []

    def ocr(image, out_json):
        calls.append(Path(image).name)
        if image.name == "S08-upscaled.png":
            with Image.open(image) as upscaled:
                assert upscaled.size == (1200, 675)
            return {"lines": S08_UPSCALED}
        return {"lines": S08_MASTER if image.stem == "S08" else [line("0.8", 10, 10), line("0.4", 10, 50),
                                                                   line("0.0", 10, 90)]}

    findings = check_master_charts(job, masters, ocr=ocr)
    assert findings == ["S08: master draws a data chart (axis 0–0.8, near “不同模型设置的预警效果对比 / "
                        "0 准确率到 FI 值”) but the slide's plan cites no table in chart-data.json; "
                        "its values have no source data"]
    # S07 draws a chart too, but its plan cites the table: not read at all.
    assert calls == ["S08.png", "S08-upscaled.png"]
    report = {s["slide_id"]: s for s in read_json(job / "workspace/master-qa/charts.json")["slides"]}
    assert report["S07"]["tables"] == ["f_01_table-001"] and report["S07"]["axes"] == []
    axis = report["S08"]["axes"][0]
    assert report["S08"]["upscaled"] and axis["orientation"] == "vertical"
    assert axis["ticks"] == ["0.0", "0,2", "0.4", "0.6", "0,8"]
    assert axis["bbox"]["x"] == 968  # back in master pixels


def test_table_reference_by_plain_table_id_counts(tmp_path):
    job, masters = make_job(tmp_path, [{"slide_id": "S03", "evidence_refs": [], "source_refs": ["f_01_table-001"]}])

    def ocr(image, out_json):
        raise AssertionError("a slide with a table is not read")

    assert check_master_charts(job, masters, ocr=ocr) == []


def test_shared_ocr_results_are_reused_and_upscale_only_when_no_axis(tmp_path):
    job, masters = make_job(tmp_path, [{"slide_id": "S02", "evidence_refs": ["ev_f_01_sec003"]},
                                       {"slide_id": "S05", "evidence_refs": ["ev_f_01_sec005"]}])
    pages = {"S02": [line("60", 100, 100, 20), line("40", 100, 150, 20), line("20", 100, 200, 20),
                     line("0", 110, 250, 10)],
             "S05": [line("用 光 流 法 外 推", 10, 10, 200, 30)]}
    calls = []

    def ocr(image, out_json):
        calls.append(Path(image).name)
        return {"lines": pages.get(Path(image).stem, [])}

    results = ocr_masters(job, masters, ocr=ocr)
    assert check_master_numbers(job, masters, ocr=ocr, results=results) == []
    findings = check_master_charts(job, masters, ocr=ocr, results=results)
    assert calls == ["S02.png", "S05.png", "S05-upscaled.png"]
    assert [f.split(" (")[0] for f in findings] == ["S02: master draws a data chart"]
    assert "axis 0–60" in findings[0]


def test_timelines_ranges_scale_bars_and_tables_are_not_axes():
    lines = [
        # S06: a downward list of seasons is a timeline, not a value axis
        line("2021", 59, 254, 73, 25), line("2022", 59, 385, 73, 26), line("2023", 59, 519, 73, 26),
        line("2024", 59, 655, 74, 26),
        # S09 r1: a map legend of ranges and a scale bar
        line("40 一 60", 952, 379, 60), line("20 一 40", 952, 416, 61), line("0 一 20", 953, 453, 51),
        line("0 5 10 km", 600, 541, 80),
        # labelled numbers and a horizontal row of years
        line("46 场", 691, 461, 100), line("30", 924, 355, 69), line("16", 927, 611, 66),
        line("2019 2020 2021 2022", 100, 900, 300),
        # two ticks are not enough (S04's small hydrograph sketch)
        line("2.0", 643, 506, 22, 14), line("1.0", 644, 568, 21, 14),
    ]
    assert find_value_axes(lines) == []


def test_uneven_column_of_numbers_is_not_an_axis():
    # A right-aligned table column (S07 hit rates) is not on one linear scale with its positions.
    lines = [line("0.82", 377, 391, 71, 31), line("0.79", 377, 520, 71, 31), line("0.61", 377, 655, 71, 31)]
    assert find_value_axes(lines) == []


def test_horizontal_axes_and_missing_ticks_are_found():
    vertical_with_gap = [line("100%", 50, 100, 40), line("50%", 60, 200, 30), line("0%", 70, 300, 20),
                         line("75%", 60, 150, 30)]
    row = [line("0", 100, 500, 10), line("20", 200, 500, 20), line("40", 300, 500, 20), line("80", 500, 500, 20)]
    merged = [line("0 0.2 0.4 0.6 0.8 1.0", 100, 700, 300)]
    found = find_value_axes(vertical_with_gap + row + merged)
    assert [(a["orientation"], a["min"], a["max"]) for a in found] == [
        ("vertical", 0, 100), ("horizontal", 0, 80), ("horizontal", 0, 1)]
