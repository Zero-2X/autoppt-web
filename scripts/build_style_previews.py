"""Style previews for the order wizard until real ImageGen samples exist.

``academic_light`` uses a real ImageGen master of the fictional test thesis (J-plan-real-001, made in that
style).  Every other style gets a schematic drawn from its palette (title band, cards, emphasis colour) and
labelled on the image as a schematic, not AI output.  ``scripts/build_style_samples.py`` replaces them with
real samples once the Codex account offers image_gen again; ``web/styles/samples.json`` records which kind
each file is, and the wizard shows the label.

    python scripts/build_style_previews.py [--real-master D:/autoppt-jobs/J-plan-real-001/assets/slides/S05.png]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "web" / "styles"
W, H = 1280, 720
FONT = "C:/Windows/Fonts/msyh.ttc"


def font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT, size)
    except OSError:
        return ImageFont.load_default()


def schematic(style: dict) -> Image.Image:
    palette = style["palette"]
    colours = [c[0] for name, c in palette.items() if name not in ("background", "ink", "accent", "gray", "muted", "steel")]
    background, ink, accent = palette["background"][0], palette["ink"][0], palette["accent"][0]
    soft = palette["background"][-1]
    main = colours[0] if colours else ink
    second = colours[1] if len(colours) > 1 else main
    muted = (palette.get("gray") or palette.get("muted") or palette.get("steel") or [ink])[0]
    image = Image.new("RGB", (W, H), background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, W, 8), fill=main)
    draw.text((64, 48), "方法分三步：雷达外推、管网溢流、街区风险", fill=ink, font=font(40))
    draw.line((64, 112, 400, 112), fill=accent, width=5)
    labels = [("① 雷达回波外推", "0–90 分钟"), ("② 节点溢流计算", "简化管网"), ("③ 街区风险等级", "低 · 中 · 高")]
    for index, (title, note) in enumerate(labels):
        x = 64 + index * 392
        fill = main if index != 1 else second
        draw.rounded_rectangle((x, 160, x + 360, 520), radius=14, fill=soft, outline=fill, width=3)
        draw.rectangle((x, 160, x + 360, 216), fill=fill)
        draw.text((x + 20, 170), title, fill="#FFFFFF", font=font(28))
        for row in range(4):
            draw.rounded_rectangle((x + 24, 248 + row * 56, x + 336 - row * 40, 276 + row * 56), radius=6, fill=fill if row == 0 else muted)
        draw.text((x + 24, 470), note, fill=accent if index == 2 else ink, font=font(24))
        if index < 2:
            draw.polygon([(x + 368, 330), (x + 384, 340), (x + 368, 350)], fill=accent)
    draw.rounded_rectangle((64, 560, W - 64, 640), radius=10, fill=soft, outline=muted, width=2)
    draw.text((88, 582), "管网节点从 18,400 个简化到 2,310 个", fill=ink, font=font(28))
    draw.text((W - 360, H - 44), "风格示意 · 非 AI 生成 · 虚构内容", fill=muted, font=font(20))
    return image


def main(argv: list[str] | None = None) -> int:
    from autoppt_service.export_options import style_options

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--real-master", type=Path, default=Path("D:/autoppt-jobs/J-plan-real-001/assets/slides/S05.png"))
    parser.add_argument("--force", action="store_true", help="redraw schematics even where a real sample exists")
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    record_path = OUT / "samples.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
    for style in style_options():
        target = OUT / f"{style['id']}.jpg"
        if record.get(style["id"], {}).get("kind") == "imagegen" and target.exists() and not args.force:
            continue
        if style["id"] == "academic_light" and args.real_master.exists():
            with Image.open(args.real_master) as master:
                image = master.convert("RGB")
                image.thumbnail((W, W))
            record[style["id"]] = {"kind": "imagegen", "source": "J-plan-real-001 S05 master (fictional test thesis)"}
        else:
            image = schematic(style)
            record[style["id"]] = {"kind": "schematic", "source": "drawn from the style palette by scripts/build_style_previews.py"}
        image.save(target, quality=84, optimize=True, progressive=True)
        print(style["id"], record[style["id"]]["kind"])
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
