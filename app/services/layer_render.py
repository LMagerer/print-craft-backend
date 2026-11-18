"""Pillow drawing of image and shape layers.

Mirrors the Konva drawing in ``frontend/src/features/editor/CardCanvas.tsx``: strokes are centred on the shape
outline, rotation is clockwise around the box centre and opacity applies to the whole layer.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

from app.schemas.config import Layer
from app.services.image_tools import parse_color

# Upper bound for the supersampled mask used to anti-alias shapes.
MAX_SS_PIXELS = 24_000_000


def _ss_factor(w: float, h: float) -> int:
    return max(1, min(4, int(math.sqrt(MAX_SS_PIXELS / max(1.0, w * h)))))


def _color_tile(mask: Image.Image, color: tuple[int, int, int, int]) -> Image.Image:
    alpha = mask if color[3] == 255 else mask.point(lambda v, a=color[3]: v * a // 255)
    tile = Image.new("RGBA", mask.size, color[:3] + (0,))
    tile.putalpha(alpha)
    return tile


def _rounded_mask(size: tuple[int, int], radius_px: float) -> Image.Image:
    w, h = size
    ss = _ss_factor(w, h)
    m = Image.new("L", (w * ss, h * ss), 0)
    r = min(radius_px, w / 2, h / 2) * ss
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=r, fill=255)
    return m.resize(size, Image.Resampling.BOX) if ss > 1 else m


def fit_image(src: Image.Image, w: int, h: int, fit: str) -> Image.Image:
    """Scale ``src`` (RGBA) into a ``w x h`` tile."""
    sw, sh = src.size
    if fit == "fill" or (sw * h == sh * w):
        return src if (sw, sh) == (w, h) else src.resize((w, h), Image.Resampling.LANCZOS)
    if fit == "cover":
        aspect = w / h
        if sw / sh > aspect:
            cw, ch = sh * aspect, sh
        else:
            cw, ch = sw, sw / aspect
        x0, y0 = (sw - cw) / 2, (sh - ch) / 2
        return src.resize((w, h), Image.Resampling.LANCZOS, box=(x0, y0, x0 + cw, y0 + ch))
    k = min(w / sw, h / sh)
    iw, ih = max(1, round(sw * k)), max(1, round(sh * k))
    tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    tile.paste(src.resize((iw, ih), Image.Resampling.LANCZOS), ((w - iw) // 2, (h - ih) // 2))
    return tile


def image_tile(layer: Layer, src: Image.Image, px_per_mm: float) -> Image.Image:
    w = max(1, round(layer.width_mm * px_per_mm))
    h = max(1, round(layer.height_mm * px_per_mm))
    tile = fit_image(src, w, h, layer.image.fit if layer.image else "cover")
    if layer.corner_radius_mm > 0:
        tile = tile.copy() if tile is src else tile
        mask = _rounded_mask((w, h), layer.corner_radius_mm * px_per_mm)
        alpha = tile.getchannel("A")
        tile.putalpha(Image.composite(alpha, Image.new("L", (w, h), 0), mask))
    return tile


def shape_tile(layer: Layer, px_per_mm: float) -> tuple[Image.Image, float]:
    """Return the drawn shape and the padding (px) around its box (room for the outside half of the stroke)."""
    st = layer.shape
    assert st is not None
    w = layer.width_mm * px_per_mm
    h = layer.height_mm * px_per_mm
    stroke = st.stroke_width_mm * px_per_mm if st.stroke else 0.0
    pad = math.ceil(stroke / 2) + 1
    tw, th = max(1, math.ceil(w + 2 * pad)), max(1, math.ceil(h + 2 * pad))
    ss = _ss_factor(tw, th)
    big = (tw * ss, th * ss)
    p, W, H, s = pad * ss, w * ss, h * ss, stroke * ss
    tile = Image.new("RGBA", (tw, th), (0, 0, 0, 0))

    def down(m: Image.Image) -> Image.Image:
        return m.resize((tw, th), Image.Resampling.BOX) if ss > 1 else m

    if st.type == "line":
        if st.stroke and stroke > 0:
            m = Image.new("L", big, 0)
            cy = p + H / 2
            ImageDraw.Draw(m).rectangle((p, cy - s / 2, p + W - 1, cy + s / 2 - 1), fill=255)
            tile = _color_tile(down(m), parse_color(st.stroke))
        return tile, pad

    r = min(layer.corner_radius_mm * px_per_mm * ss, W / 2, H / 2)
    if st.fill:
        m = Image.new("L", big, 0)
        d = ImageDraw.Draw(m)
        box = (p, p, p + W - 1, p + H - 1)
        if st.type == "ellipse":
            d.ellipse(box, fill=255)
        else:
            d.rounded_rectangle(box, radius=r, fill=255)
        tile = _color_tile(down(m), parse_color(st.fill))
    if st.stroke and stroke > 0:
        m = Image.new("L", big, 0)
        d = ImageDraw.Draw(m)
        box = (p - s / 2, p - s / 2, p + W + s / 2 - 1, p + H + s / 2 - 1)
        width = max(1, round(s))
        if st.type == "ellipse":
            d.ellipse(box, outline=255, width=width)
        else:
            d.rounded_rectangle(box, radius=r + s / 2 if r > 0 else 0, outline=255, width=width)
        tile.alpha_composite(_color_tile(down(m), parse_color(st.stroke)))
    return tile, pad


def composite_layer(canvas: Image.Image, layer: Layer, tile: Image.Image, px_per_mm: float, pad: float = 0) -> None:
    """Alpha-composite ``tile`` (the layer box plus ``pad`` px on every side) onto ``canvas``."""
    if layer.opacity < 1:
        tile = tile.copy()
        tile.putalpha(tile.getchannel("A").point(lambda v, o=layer.opacity: round(v * o)))
    x = layer.x_mm * px_per_mm - pad
    y = layer.y_mm * px_per_mm - pad
    if layer.rotation % 360:
        cx, cy = x + tile.width / 2, y + tile.height / 2
        tile = tile.rotate(-layer.rotation, resample=Image.Resampling.BICUBIC, expand=True)
        x, y = cx - tile.width / 2, cy - tile.height / 2
    dx, dy = round(x), round(y)
    # alpha_composite needs a destination inside the canvas: crop the tile instead of passing negative offsets.
    sx, sy = max(0, -dx), max(0, -dy)
    ex, ey = min(tile.width, canvas.width - dx), min(tile.height, canvas.height - dy)
    if ex <= sx or ey <= sy:
        return
    if (sx, sy, ex, ey) != (0, 0, tile.width, tile.height):
        tile = tile.crop((sx, sy, ex, ey))
    canvas.alpha_composite(tile, dest=(dx + sx, dy + sy))
