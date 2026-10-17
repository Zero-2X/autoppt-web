"""Web previews from images that already exist: masters and Office renders.

Nothing here renders or generates a slide.  Masters come from the ImageGen
step, native renders from the Office QA step; this module only resizes,
tiles and compares them:

* thumbnails and a contact sheet (CP2 master review),
* master | native side-by-side images and a difference heatmap per slide
  (CP3 native preview).

Difference metrics are informational: the product never promises pixel
identity, so they are shown to the user, not used as a release gate.

Every preview entry has the checkpoint shape ``{path, kind, slide_id, sha256}``
plus size; paths are relative to the job directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from autoppt_service.jobfiles import relative_to_job, service_path, sha256_file, utc_now, write_json_atomic

PREVIEWS_SCHEMA = "autoppt-previews-v1"
THUMB_WIDTH = 320
SIDE_BY_SIDE_HEIGHT = 540
CHANGED_THRESHOLD = 24  # per-pixel mean channel difference (0-255) counted as "changed"
_BACKGROUND = (244, 246, 249)
_INK = (23, 32, 51)


_CJK_FONTS = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


def _label_font(size: int) -> tuple[ImageFont.ImageFont, bool]:
    """A CJK-capable font if the machine has one; otherwise Pillow's default (ASCII labels only)."""
    for candidate in _CJK_FONTS:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size), True
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size), False
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default(), False


def _label(chinese: str, english: str, cjk: bool) -> str:
    return f"{chinese} {english}" if cjk else english


def discover_slides(folder: Path, pattern: str = "*.png") -> dict[str, Path]:
    """``{stem: path}`` for slide images such as ``S01.png``, sorted by name."""
    return {path.stem: path for path in sorted(Path(folder).glob(pattern)) if path.is_file()}


def _rgb(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).convert("RGB")


def _entry(job_dir: Path, path: Path, kind: str, slide_id: str | None = None, **extra: Any) -> dict[str, Any]:
    with Image.open(path) as image:
        width, height = image.size
    entry = {"path": relative_to_job(job_dir, path), "kind": kind, "sha256": sha256_file(path), "width": width, "height": height}
    if slide_id:
        entry["slide_id"] = slide_id
    entry.update(extra)
    return entry


def _fit_width(image: Image.Image, width: int) -> Image.Image:
    if image.width <= width:
        return image.copy()
    height = max(1, round(image.height * width / image.width))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _fit_height(image: Image.Image, height: int) -> Image.Image:
    width = max(1, round(image.width * height / image.height))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def make_thumbnails(job_dir: Path, slides: Mapping[str, Path], out_dir: Path, *, width: int = THUMB_WIDTH) -> list[dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for slide_id, path in slides.items():
        target = out_dir / f"{slide_id}-thumb.png"
        _fit_width(_rgb(path), width).save(target, "PNG", optimize=True)
        entries.append(_entry(job_dir, target, "thumbnail", slide_id))
    return entries


def make_contact_sheet(
    job_dir: Path,
    slides: Mapping[str, Path],
    target: Path,
    *,
    columns: int = 4,
    cell_width: int = THUMB_WIDTH,
    label: str = "",
) -> dict[str, Any]:
    if not slides:
        raise ValueError("contact sheet needs at least one slide")
    thumbs = [(slide_id, _fit_width(_rgb(path), cell_width)) for slide_id, path in slides.items()]
    cell_height = max(thumb.height for _, thumb in thumbs)
    font, cjk = _label_font(16)
    caption, gap = 22, 12
    columns = max(1, min(columns, len(thumbs)))
    rows = (len(thumbs) + columns - 1) // columns
    header = 32 if label else 0
    sheet = Image.new(
        "RGB",
        (gap + columns * (cell_width + gap), header + gap + rows * (cell_height + caption + gap)),
        _BACKGROUND,
    )
    draw = ImageDraw.Draw(sheet)
    if label:
        draw.text((gap, 6), label if cjk else label.split(" / ")[-1], fill=_INK, font=font)
    for index, (slide_id, thumb) in enumerate(thumbs):
        row, column = divmod(index, columns)
        x = gap + column * (cell_width + gap)
        y = header + gap + row * (cell_height + caption + gap)
        sheet.paste(thumb, (x, y))
        draw.rectangle((x - 1, y - 1, x + thumb.width, y + thumb.height), outline=(200, 205, 214))
        draw.text((x, y + thumb.height + 3), slide_id, fill=_INK, font=font)
    target.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(target, "PNG", optimize=True)
    return _entry(job_dir, target, "contact_sheet", slides_included=list(slides))


def make_side_by_side(
    job_dir: Path, slide_id: str, master: Path, native: Path, target: Path, *, height: int = SIDE_BY_SIDE_HEIGHT
) -> dict[str, Any]:
    left, right = _fit_height(_rgb(master), height), _fit_height(_rgb(native), height)
    font, cjk = _label_font(20)
    gap, caption = 16, 30
    canvas = Image.new("RGB", (left.width + right.width + 3 * gap, height + caption + 2 * gap), _BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    canvas.paste(left, (gap, gap + caption))
    canvas.paste(right, (2 * gap + left.width, gap + caption))
    draw.text((gap, gap), f"{slide_id}  {_label('母版', 'master', cjk)}", fill=_INK, font=font)
    draw.text((2 * gap + left.width, gap), f"{slide_id}  {_label('原生', 'native', cjk)}", fill=_INK, font=font)
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, "PNG", optimize=True)
    return _entry(job_dir, target, "side_by_side", slide_id)


def diff_metrics(master: Image.Image, native: Image.Image) -> tuple[np.ndarray, dict[str, float]]:
    """Per-pixel mean channel difference (native resized to the master's size) and summary metrics."""
    if native.size != master.size:
        native = native.resize(master.size, Image.Resampling.LANCZOS)
    a = np.asarray(master, dtype=np.int16)
    b = np.asarray(native, dtype=np.int16)
    diff = np.abs(a - b).mean(axis=2)
    metrics = {
        "mean_abs_diff": round(float(diff.mean()) / 255, 6),
        "changed_pixel_ratio": round(float((diff >= CHANGED_THRESHOLD).mean()), 6),
        "max_abs_diff": round(float(diff.max()) / 255, 6),
    }
    return diff, metrics


def make_diff_heatmap(job_dir: Path, slide_id: str, master: Path, native: Path, target: Path) -> dict[str, Any]:
    base = _rgb(master)
    diff, metrics = diff_metrics(base, _rgb(native))
    intensity = np.clip(diff * 3, 0, 255).astype(np.uint8)  # amplify small differences
    # Washed-out greyscale master underneath, so red differences stand out in context.
    grey = 255 - (255 - np.asarray(ImageOps.grayscale(base), dtype=np.float32)) * 0.35
    alpha = intensity.astype(np.float32) / 255
    heat = np.zeros((*diff.shape, 3), dtype=np.float32)
    heat[..., 0] = grey * (1 - alpha) + 220 * alpha
    heat[..., 1] = grey * (1 - alpha) + 40 * alpha
    heat[..., 2] = grey * (1 - alpha) + 40 * alpha
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(heat.clip(0, 255).astype(np.uint8), "RGB").save(target, "PNG", optimize=True)
    return _entry(job_dir, target, "diff_heatmap", slide_id, metrics=metrics)


def build_master_previews(job_dir: Path, masters: Mapping[str, Path], *, name: str = "masters") -> dict[str, Any]:
    """CP2 set: each master as a slide preview, thumbnails and one contact sheet."""
    job_dir = Path(job_dir)
    out_dir = service_path(job_dir, "previews") / name
    entries = [_entry(job_dir, Path(path), "slide", slide_id) for slide_id, path in masters.items()]
    entries += make_thumbnails(job_dir, masters, out_dir / "thumbs")
    entries.append(make_contact_sheet(job_dir, masters, out_dir / "contact-sheet.png", label="母版联系表 / masters"))
    return _write_manifest(job_dir, out_dir, name, entries)


def build_native_previews(
    job_dir: Path, masters: Mapping[str, Path], natives: Mapping[str, Path], *, name: str = "native"
) -> dict[str, Any]:
    """CP3 set: native renders, side-by-side with masters, heatmaps and a contact sheet."""
    job_dir = Path(job_dir)
    missing = sorted(set(masters) - set(natives))
    if missing:
        raise ValueError(f"no native render for slides: {', '.join(missing)}")
    out_dir = service_path(job_dir, "previews") / name
    entries: list[dict[str, Any]] = []
    for slide_id, master in masters.items():
        native = Path(natives[slide_id])
        entries.append(_entry(job_dir, native, "slide", slide_id))
        entries.append(make_side_by_side(job_dir, slide_id, Path(master), native, out_dir / f"{slide_id}-side-by-side.png"))
        entries.append(make_diff_heatmap(job_dir, slide_id, Path(master), native, out_dir / f"{slide_id}-diff.png"))
    entries.append(
        make_contact_sheet(
            job_dir, {sid: natives[sid] for sid in masters}, out_dir / "contact-sheet.png", label="原生版联系表 / native"
        )
    )
    return _write_manifest(job_dir, out_dir, name, entries)


def _write_manifest(job_dir: Path, out_dir: Path, name: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    manifest = {"schema_version": PREVIEWS_SCHEMA, "name": name, "generated_at": utc_now(), "previews": entries}
    metrics = {e["slide_id"]: e["metrics"] for e in entries if e["kind"] == "diff_heatmap"}
    if metrics:
        manifest["diff_metrics"] = metrics
        manifest["diff_metrics_note"] = "informational only; not a release gate"
    write_json_atomic(out_dir / "manifest.json", manifest)
    return manifest


def checkpoint_previews(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Preview entries trimmed to the checkpoint shape."""
    keep = ("path", "kind", "slide_id", "sha256")
    return [{key: entry[key] for key in keep if key in entry} for entry in manifest["previews"]]
