"""PNG / JPG / WebP / HEIC uploads: verify, measure, and register as assets.

HEIC/HEIF (phone photos) is decoded with ``pillow_heif`` and transcoded to a
PNG asset, because PowerPoint and the image tools downstream do not read HEIC;
without ``pillow_heif`` the file is marked ``needs_transcode``.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from autoppt_service.parsers.base import (
    IntakeLimits,
    ParsedSource,
    SourceFile,
    SourceRejected,
    image_asset,
    make_item,
)

ROLE_NOTES = {
    "keep_as_image": "原图保留：作为有边界的图片使用，ImageGen 不得重绘",
    "logo": "Logo：作为有边界的图片使用，ImageGen 不得重绘或仿制",
    "style_reference": "风格参考：只用于版式和配色参考，不作为内容证据",
    "vectorize_source": "待矢量化的页面图",
    "image_asset": "图片素材",
}


def _heic_opener_available() -> bool:
    try:
        import pillow_heif  # type: ignore[import-not-found]
    except ImportError:
        return False
    pillow_heif.register_heif_opener()
    return True


def parse_image(source: SourceFile, work_dir: Path, limits: IntakeLimits) -> ParsedSource:
    result = ParsedSource(parser="image")
    path = source.path
    suffix = path.suffix.lower()
    if suffix in {".heic", ".heif"} and not _heic_opener_available():
        result.status = "needs_transcode"
        result.warnings.append("HEIC/HEIF 需要先转码为 PNG/JPG")
        result.stats = {"format": "HEIC"}
        return result

    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = limits.max_image_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
                image_format = image.format
                mode = image.mode
                if width * height > limits.max_image_pixels:
                    raise SourceRejected("image_too_large", f"图片像素过多：{width}×{height}")
                image.load()
    except SourceRejected:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise SourceRejected("image_too_large", f"图片像素过多：{exc}") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise SourceRejected("image_unreadable", f"无法读取图片：{exc}") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous

    origin = "upload"
    if suffix in {".heic", ".heif"}:
        transcoded = work_dir / "media" / source.file_id / "image.png"
        transcoded.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(path) as image:
            image.save(transcoded, "PNG")
        path, origin, image_format = transcoded, f"upload ({image_format} transcoded to PNG)", "PNG"
    asset = image_asset(source, "image", path, origin=origin, width=width, height=height, image_format=image_format)
    result.assets.append(asset)
    result.items.append(
        make_item(
            source,
            "image",
            item_type="figure",
            text=f"{ROLE_NOTES.get(source.role, '图片')}：{source.name}（{width}×{height} {image_format}）",
            title=source.name,
            location="image",
            figure=asset["asset_id"],
            extra={"asset_id": asset["asset_id"], "keep_as_image": asset["keep_as_image"]},
        )
    )
    result.stats = {"width": width, "height": height, "format": image_format, "mode": mode}
    return result
