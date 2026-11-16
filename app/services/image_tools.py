"""Image size editor operations (lossless print-size change, Lanczos resample, crop/fit, rotate, flip, bleed).

The original upload is never modified: every operation produces a *new* lossless PNG that keeps the
ICC profile and carries DPI metadata.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

from app.core.config import get_settings
from app.core.units import MM_PER_INCH, dpi_for, pixels_for
from app.schemas.config import Placeholder, SizeConfig, TemplateConfig
from app.schemas.image import ImageInfo, TransformRequest
from app.services.storage import store_bytes

Image.MAX_IMAGE_PIXELS = 400_000_000
LOW_DPI_WARNING = 150


def parse_color(value: str) -> tuple[int, int, int, int]:
    """Parse ``#RGB``, ``#RGBA``, ``#RRGGBB``, ``#RRGGBBAA`` or ``rgb[a](r,g,b[,a])`` into an RGBA tuple."""
    v = value.strip()
    if v.startswith("#"):
        h = v[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) == 6:
            h += "ff"
        if len(h) != 8 or not re.fullmatch(r"[0-9a-fA-F]{8}", h):
            raise ValueError(f"Invalid color: {value}")
        return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4, 6))  # type: ignore[return-value]
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)", v)
    if not m:
        raise ValueError(f"Invalid color: {value}")
    a = float(m.group(4)) if m.group(4) is not None else 1.0
    r, g, b = (min(255, int(m.group(i))) for i in (1, 2, 3))
    return r, g, b, max(0, min(255, round(a * 255)))


def icc_description(icc: bytes | None) -> str | None:
    if not icc:
        return None
    try:
        return ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip() or "Embedded ICC profile"
    except Exception:
        return "Embedded ICC profile"


def has_alpha(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)


def file_dpi(img: Image.Image) -> float | None:
    dpi = img.info.get("dpi")
    if dpi and dpi[0]:
        return round(float(dpi[0]), 3)
    return None


def open_image(path: Path | str) -> Image.Image:
    img = Image.open(path)
    img.load()
    return img


def image_info(img: Image.Image, ref: str) -> ImageInfo:
    return ImageInfo(
        ref=ref,
        width_px=img.width,
        height_px=img.height,
        dpi=file_dpi(img),
        mode=img.mode,
        has_alpha=has_alpha(img),
        icc_profile=icc_description(img.info.get("icc_profile")),
        format=img.format or "PNG",
    )


FORMAT_EXT = {"PNG": "png", "JPEG": "jpg", "TIFF": "tif", "WEBP": "webp", "BMP": "bmp", "GIF": "gif"}


def store_image(data: bytes) -> ImageInfo:
    """Store uploaded image bytes untouched (never recompressed), deduplicated by SHA-256."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as e:
        raise ValueError(f"Not a supported image: {e}") from e
    ext = FORMAT_EXT.get(img.format or "")
    if ext is None:
        raise ValueError(f"Unsupported image format {img.format}")
    sha, _ = store_bytes(get_settings().templates_dir, data, ext)
    return image_info(img, sha)


def to_working_mode(img: Image.Image) -> Image.Image:
    """Convert to RGB/RGBA for processing. CMYK is colour-managed to sRGB when it has a profile."""
    icc = img.info.get("icc_profile")
    if img.mode == "CMYK":
        if icc:
            try:
                src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                out = ImageCms.profileToProfile(img, src, ImageCms.createProfile("sRGB"), outputMode="RGB")
                assert out is not None
                out.info.pop("icc_profile", None)
                return out
            except Exception:
                pass
        out = img.convert("RGB")
        out.info.pop("icc_profile", None)
        return out
    if img.mode in ("RGB", "RGBA"):
        return img
    out = img.convert("RGBA" if has_alpha(img) else "RGB")
    if icc:
        out.info["icc_profile"] = icc
    return out


def png_bytes(img: Image.Image, dpi: float | None, icc: bytes | None) -> bytes:
    """Encode as lossless PNG with DPI metadata and the ICC profile."""
    buf = io.BytesIO()
    kwargs: dict = {"format": "PNG", "optimize": False, "compress_level": 6}
    if dpi:
        kwargs["dpi"] = (dpi, dpi)
    if icc:
        kwargs["icc_profile"] = icc
    img.save(buf, **kwargs)
    return buf.getvalue()


def thumbnail_png(path: Path, max_side: int = 320) -> bytes:
    img = to_working_mode(open_image(path))
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------------------------------


def anchor_fraction(anchor: str) -> tuple[float, float]:
    fx = 0.0 if "left" in anchor else 1.0 if "right" in anchor else 0.5
    fy = 0.0 if anchor.startswith("top") else 1.0 if anchor.startswith("bottom") else 0.5
    return fx, fy


def cover_crop_box(w: int, h: int, aspect: float, anchor: str) -> tuple[int, int, int, int]:
    """Largest box with ``aspect`` (w/h) inside ``w x h``, positioned by ``anchor``."""
    if w / h > aspect:
        cw, ch = round(h * aspect), h
    else:
        cw, ch = w, round(w / aspect)
    cw, ch = max(1, min(w, cw)), max(1, min(h, ch))
    fx, fy = anchor_fraction(anchor)
    x, y = round((w - cw) * fx), round((h - ch) * fy)
    return x, y, cw, ch


def contain_pad(w: int, h: int, aspect: float, anchor: str) -> tuple[int, int, int, int]:
    """Canvas size with ``aspect`` that contains ``w x h``; returns (canvas_w, canvas_h, offset_x, offset_y)."""
    if w / h > aspect:
        cw, ch = w, round(w / aspect)
    else:
        cw, ch = round(h * aspect), h
    cw, ch = max(w, cw), max(h, ch)
    fx, fy = anchor_fraction(anchor)
    return cw, ch, round((cw - w) * fx), round((ch - h) * fy)


def expand_mirror(img: Image.Image, b: int) -> Image.Image:
    """Add ``b`` pixels on every side by mirroring the edges (good default for print bleed)."""
    w, h = img.size
    out = Image.new(img.mode, (w + 2 * b, h + 2 * b))
    out.paste(img, (b, b))
    out.paste(img.crop((0, 0, b, h)).transpose(Image.Transpose.FLIP_LEFT_RIGHT), (0, b))
    out.paste(img.crop((w - b, 0, w, h)).transpose(Image.Transpose.FLIP_LEFT_RIGHT), (w + b, b))
    out.paste(out.crop((0, b, w + 2 * b, 2 * b)).transpose(Image.Transpose.FLIP_TOP_BOTTOM), (0, 0))
    out.paste(out.crop((0, h, w + 2 * b, h + b)).transpose(Image.Transpose.FLIP_TOP_BOTTOM), (0, h + b))
    return out


def fill_for(img: Image.Image, color: str) -> tuple[int, ...]:
    rgba = parse_color(color)
    return rgba if img.mode == "RGBA" else rgba[:3]


# --------------------------------------------------------------------------------------------------
# Placeholder mapping
# --------------------------------------------------------------------------------------------------


def map_placeholders(config: TemplateConfig, kx: float, ky: float, ox_mm: float = 0.0, oy_mm: float = 0.0) -> TemplateConfig:
    """Apply ``new = old * k + o`` (mm) to every placeholder; font metrics scale by ``min(kx, ky)``."""
    kf = min(kx, ky)
    out: list[Placeholder] = []
    for p in config.placeholders:
        q = p.model_copy(deep=True)
        q.x_mm = p.x_mm * kx + ox_mm
        q.y_mm = p.y_mm * ky + oy_mm
        q.box.width_mm = p.box.width_mm * kx
        q.box.height_mm = p.box.height_mm * ky
        f = q.font
        f.size_pt = p.font.size_pt * kf
        if f.min_size_pt is not None:
            f.min_size_pt = p.font.min_size_pt * kf  # type: ignore[operator]
        f.letter_spacing = p.font.letter_spacing * kf
        f.stroke_width_pt = p.font.stroke_width_pt * kf
        f.shadow.offset_x_mm = p.font.shadow.offset_x_mm * kf
        f.shadow.offset_y_mm = p.font.shadow.offset_y_mm * kf
        f.shadow.blur_mm = p.font.shadow.blur_mm * kf
        out.append(q)
    return config.model_copy(update={"placeholders": out}, deep=True)


def scale_placeholders_proportional(config: TemplateConfig, new_w_mm: float, new_h_mm: float) -> TemplateConfig:
    """Keep placeholders at the same *relative* position when the card size changes."""
    return map_placeholders(config, new_w_mm / config.size.width_mm, new_h_mm / config.size.height_mm)


# --------------------------------------------------------------------------------------------------
# Transform pipeline
# --------------------------------------------------------------------------------------------------


@dataclass
class TransformResult:
    image: Image.Image
    icc: bytes | None
    dpi: float
    width_mm: float
    height_mm: float
    bleed_mm: float
    warnings: list[str] = field(default_factory=list)
    # Pixel-space mapping old image -> new image per axis: new = old * s + t. None if rotated/flipped.
    px_map: tuple[float, float, float, float] | None = (1.0, 0.0, 1.0, 0.0)


def _resample_warnings(src_w: int, src_h: int, tw: int, th: int, width_mm: float, height_mm: float) -> list[str]:
    warnings: list[str] = []
    if tw > src_w or th > src_h:
        warnings.append(f"Upscaling from {src_w}x{src_h} px to {tw}x{th} px: the result will look soft.")
    eff = min(dpi_for(src_w, width_mm), dpi_for(src_h, height_mm))
    if eff < LOW_DPI_WARNING:
        warnings.append(f"Source resolution is only {eff:.0f} DPI at this size (recommended >= {LOW_DPI_WARNING}).")
    if abs(src_w / src_h - tw / th) > 0.01 * (tw / th):
        warnings.append("Aspect ratio changes: the image will be stretched. Use Crop / fit to avoid distortion.")
    return warnings


def transform_image(img: Image.Image, req: TransformRequest) -> TransformResult:
    icc = img.info.get("icc_profile")
    if img.mode == "CMYK":
        # Converted to sRGB below; the CMYK profile no longer applies.
        icc = None
    img = to_working_mode(img)
    warnings: list[str] = []
    sx, tx, sy, ty = 1.0, 0.0, 1.0, 0.0
    exact = True

    if req.rotate:
        # Clockwise rotation, lossless.
        op = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}[req.rotate]
        img = img.transpose(op)
        exact = False
    if req.flip_h:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        exact = False
    if req.flip_v:
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        exact = False

    w, h = img.size
    if req.mode == "print_size_only":
        # Lossless: only the physical size / DPI metadata changes. Aspect follows the pixels.
        dpi = dpi_for(w, req.width_mm)
        if abs(dpi_for(h, req.height_mm) - dpi) / dpi > 0.01:
            warnings.append("Height adjusted to keep the pixel aspect ratio (print size only never resamples).")
        if dpi < LOW_DPI_WARNING:
            warnings.append(f"Effective resolution is {dpi:.0f} DPI (recommended >= {LOW_DPI_WARNING}).")
    else:
        dpi = req.target_dpi
        if req.mode == "crop_fit":
            aspect = req.width_mm / req.height_mm
            if req.crop is not None:
                x0 = max(0, min(w - 1, round(req.crop.x)))
                y0 = max(0, min(h - 1, round(req.crop.y)))
                x1 = max(x0 + 1, min(w, round(req.crop.x + req.crop.width)))
                y1 = max(y0 + 1, min(h, round(req.crop.y + req.crop.height)))
                img = img.crop((x0, y0, x1, y1))
                tx, ty = tx - x0, ty - y0
            elif req.fit == "cover":
                x0, y0, cw, ch = cover_crop_box(w, h, aspect, req.anchor)
                img = img.crop((x0, y0, x0 + cw, y0 + ch))
                tx, ty = tx - x0, ty - y0
            else:
                cw, ch, ox, oy = contain_pad(w, h, aspect, req.anchor)
                canvas = Image.new(img.mode, (cw, ch), fill_for(img, req.pad_color))
                canvas.paste(img, (ox, oy))
                img = canvas
                tx, ty = tx + ox, ty + oy
            w, h = img.size
        tw, th = pixels_for(req.width_mm, dpi), pixels_for(req.height_mm, dpi)
        warnings += _resample_warnings(w, h, tw, th, req.width_mm, req.height_mm)
        if (tw, th) != (w, h):
            img = img.resize((tw, th), Image.Resampling.LANCZOS)
            kx, ky = tw / w, th / h
            sx, tx, sy, ty = sx * kx, tx * kx, sy * ky, ty * ky
        w, h = img.size

    base_bleed = 0.0
    if req.config is not None:
        base_bleed = req.config.size.bleed_mm
    bleed_mm = base_bleed
    if req.bleed_mm:
        b = round(abs(req.bleed_mm) * dpi / MM_PER_INCH)
        if req.bleed_mm > 0 and b:
            if req.bleed_fill == "mirror" and b <= min(w, h):
                img = expand_mirror(img, b)
            else:
                img = ImageOps.expand(img, border=b, fill=fill_for(img, req.pad_color))
            tx, ty = tx + b, ty + b
        elif b:
            if 2 * b >= min(w, h):
                raise ValueError("Bleed trim is larger than the image")
            img = img.crop((b, b, w - b, h - b))
            tx, ty = tx - b, ty - b
        bleed_mm = max(0.0, base_bleed + b * MM_PER_INCH / dpi * (1 if req.bleed_mm > 0 else -1))

    if req.background and has_alpha(img):
        bg = Image.new("RGBA", img.size, parse_color(req.background)[:3] + (255,))
        img = Image.alpha_composite(bg, img.convert("RGBA")).convert("RGB")

    width_mm = img.width / dpi * MM_PER_INCH
    height_mm = img.height / dpi * MM_PER_INCH
    return TransformResult(img, icc, dpi, width_mm, height_mm, round(bleed_mm, 4), warnings, (sx, tx, sy, ty) if exact else None)


def remap_config(config: TemplateConfig, old_px: tuple[int, int], result: TransformResult) -> TemplateConfig:
    """Move placeholders so they stay on the same image content after ``transform_image``.

    Rotations/flips cannot be mapped exactly (text is not rotated), so placeholders keep their relative position.
    """
    new_size = SizeConfig(
        mode=config.size.mode,
        width_mm=round(result.width_mm, 4),
        height_mm=round(result.height_mm, 4),
        dpi=round(result.dpi, 4),
        bleed_mm=result.bleed_mm,
    )
    if result.px_map is None:
        mapped = scale_placeholders_proportional(config, result.width_mm, result.height_mm)
    else:
        sx, tx, sy, ty = result.px_map
        # old mm -> old px -> new px -> new mm
        p0x = old_px[0] / config.size.width_mm
        p0y = old_px[1] / config.size.height_mm
        k = MM_PER_INCH / result.dpi
        mapped = map_placeholders(config, sx * p0x * k, sy * p0y * k, tx * k, ty * k)
    return mapped.model_copy(update={"size": new_size})
