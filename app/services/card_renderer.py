"""Final card rendering with Pillow at the template's full original resolution.

This is the only renderer used for batch output and PDFs. The browser canvas is just an approximate preview
built on the same layout algorithm (``text_layout``) and the same font files.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

from app.core.units import MM_PER_INCH, pt_to_px
from app.schemas.config import Placeholder, TemplateConfig
from app.services.font_manager import FontFace, FontLibrary, pil_font
from app.services.image_tools import has_alpha, parse_color, png_bytes, to_working_mode
from app.services.text_layout import SHRINK_STEP_PT, LayoutParams, TextLayout, layout_text
from app.services.tokens import apply_transform, binding_fields, resolve_binding


@dataclass
class PlaceholderReport:
    id: str
    text: str
    size_pt: float
    overflow: bool
    missing_glyphs: list[str]
    missing_fields: list[str] = field(default_factory=list)


@dataclass
class RenderedCard:
    image: Image.Image
    dpi: float
    icc: bytes | None
    reports: list[PlaceholderReport]

    def to_png(self) -> bytes:
        return png_bytes(self.image, self.dpi, self.icc)


def _composite(canvas: Image.Image, mask: Image.Image, color: tuple[int, int, int, int], dest: tuple[int, int]) -> None:
    """Alpha-composite ``color`` through a coverage ``mask`` (avoids Pillow's dark fringes on RGBA draws)."""
    alpha = mask if color[3] == 255 else mask.point(lambda v, a=color[3]: v * a // 255)
    layer = Image.new("RGBA", mask.size, color[:3] + (0,))
    layer.putalpha(alpha)
    canvas.alpha_composite(layer, dest=dest)


class CardRenderer:
    """Renders cards for one template. Create once per job and call :meth:`render` per record."""

    def __init__(self, base: Image.Image, config: TemplateConfig, fonts: FontLibrary) -> None:
        self.config = config
        self.fonts = fonts
        self.icc = None if base.mode == "CMYK" else base.info.get("icc_profile")
        self.keep_alpha = has_alpha(base)
        self.base = to_working_mode(base).convert("RGBA")
        # Placeholder geometry is in mm; the pixel scale comes from the actual image size.
        self.px_per_mm = self.base.width / config.size.width_mm
        self.dpi = self.px_per_mm * MM_PER_INCH

    # ---------------------------------------------------------------- layout
    def faces(self, p: Placeholder) -> tuple[FontFace, FontFace | None]:
        f = p.font
        primary = self.fonts.resolve(f.font_id, f.bold, f.italic)
        fallback = self.fonts.resolve(f.fallback_font_id, f.bold, f.italic) if f.fallback_font_id is not None else None
        return primary, fallback

    def text_for(self, p: Placeholder, record: Mapping[str, Any] | None) -> str:
        return apply_transform(resolve_binding(p.binding, record, p.sample_text), p.font.transform)

    def layout(self, p: Placeholder, text: str) -> tuple[TextLayout, FontFace, FontFace | None]:
        primary, fallback = self.faces(p)
        f = p.font
        size_px = pt_to_px(f.size_pt, self.dpi)
        params = LayoutParams(
            box_x=p.x_mm * self.px_per_mm,
            box_y=p.y_mm * self.px_per_mm,
            box_w=p.box.width_mm * self.px_per_mm,
            box_h=p.box.height_mm * self.px_per_mm,
            size_px=size_px,
            min_size_px=pt_to_px(f.min_size_pt, self.dpi) if f.min_size_pt else size_px,
            step_px=pt_to_px(SHRINK_STEP_PT, self.dpi),
            letter_spacing_px=pt_to_px(f.letter_spacing, self.dpi),
            line_height=f.line_height,
            wrap=f.wrap,
            auto_shrink=f.auto_shrink,
            align=p.align,
            valign=p.valign,
        )
        lay = layout_text(text, params, primary.metrics, fallback.metrics if fallback else None)
        return lay, primary, fallback

    # ---------------------------------------------------------------- drawing
    def _draw(self, canvas: Image.Image, p: Placeholder, lay: TextLayout, primary: FontFace, fallback: FontFace | None) -> None:
        if not lay.lines:
            return
        f = p.font
        stroke_px = pt_to_px(f.stroke_width_pt, self.dpi)
        sh = f.shadow
        sh_dx = sh.offset_x_mm * self.px_per_mm if sh.enabled else 0.0
        sh_dy = sh.offset_y_mm * self.px_per_mm if sh.enabled else 0.0
        sh_blur = sh.blur_mm * self.px_per_mm if sh.enabled else 0.0

        # Work in a region around the text instead of full-size masks.
        pad = lay.size_px * 0.6 + stroke_px + abs(sh_dx) + abs(sh_dy) + sh_blur * 3 + 4
        m = primary.metrics
        asc = m.ascender * lay.size_px / m.units_per_em
        desc = m.descender * lay.size_px / m.units_per_em
        x0 = int(min(ln.x for ln in lay.lines) - pad)
        x1 = int(max(ln.x + ln.width for ln in lay.lines) + pad) + 1
        y0 = int(min(ln.baseline for ln in lay.lines) - asc - pad)
        y1 = int(max(ln.baseline for ln in lay.lines) + desc + pad) + 1
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(canvas.width, x1), min(canvas.height, y1)
        if x1 <= x0 or y1 <= y0:
            return
        size = (x1 - x0, y1 - y0)

        fill_mask = Image.new("L", size, 0)
        fd = ImageDraw.Draw(fill_mask)
        stroke_mask = Image.new("L", size, 0) if stroke_px > 0 else None
        sd = ImageDraw.Draw(stroke_mask) if stroke_mask is not None else None
        fonts = {0: pil_font(primary.path, lay.size_px)}
        if fallback is not None:
            fonts[1] = pil_font(fallback.path, lay.size_px)
        for ln in lay.lines:
            for g in ln.glyphs:
                if g.char.isspace():
                    continue
                xy = (g.x - x0, ln.baseline - y0)
                font = fonts.get(g.font, fonts[0])
                fd.text(xy, g.char, font=font, fill=255, anchor="ls")
                if sd is not None:
                    sd.text(xy, g.char, font=font, fill=255, anchor="ls", stroke_width=stroke_px, stroke_fill=255)

        outline = stroke_mask if stroke_mask is not None else fill_mask
        if sh.enabled:
            shadow = Image.new("L", size, 0)
            shadow.paste(outline, (round(sh_dx), round(sh_dy)))
            if sh_blur > 0:
                # Canvas shadowBlur = 2 * sigma; Pillow's GaussianBlur radius is sigma.
                shadow = shadow.filter(ImageFilter.GaussianBlur(sh_blur / 2))
            _composite(canvas, shadow, parse_color(sh.color), (x0, y0))
        if stroke_mask is not None:
            _composite(canvas, stroke_mask, parse_color(f.stroke_color), (x0, y0))
        _composite(canvas, fill_mask, parse_color(f.color), (x0, y0))

    def render(self, record: Mapping[str, Any] | None) -> RenderedCard:
        canvas = self.base.copy()
        reports: list[PlaceholderReport] = []
        for p in self.config.placeholders:
            if p.hidden:
                continue
            text = self.text_for(p, record)
            missing_fields = [] if record is None else [k for k in binding_fields(p.binding) if k not in record]
            lay, primary, fallback = self.layout(p, text)
            self._draw(canvas, p, lay, primary, fallback)
            reports.append(
                PlaceholderReport(p.id, text, round(lay.size_px * 72 / self.dpi, 3), lay.overflow, lay.missing, missing_fields)
            )
        out = canvas if self.keep_alpha else canvas.convert("RGB")
        return RenderedCard(out, self.dpi, self.icc, reports)


def glyph_check(config: TemplateConfig, fonts: FontLibrary, records: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Scan every record and report characters each placeholder's fonts cannot draw.

    Returns ``{placeholder_id: {"missing": {char: row_count}, "rows": [first row indices]}}``.
    """
    from app.services.text_layout import Measurer

    measurers = {}
    for p in config.placeholders:
        primary = fonts.resolve(p.font.font_id, p.font.bold, p.font.italic)
        fb = fonts.resolve(p.font.fallback_font_id, p.font.bold, p.font.italic) if p.font.fallback_font_id is not None else None
        measurers[p.id] = Measurer(primary.metrics, fb.metrics if fb else None)
    result: dict[str, dict[str, Any]] = {p.id: {"missing": {}, "rows": []} for p in config.placeholders}
    for idx, rec in enumerate(records):
        for p in config.placeholders:
            text = apply_transform(resolve_binding(p.binding, rec), p.font.transform)
            miss = measurers[p.id].missing(text)
            if miss:
                entry = result[p.id]
                for ch in miss:
                    entry["missing"][ch] = entry["missing"].get(ch, 0) + 1
                if len(entry["rows"]) < 50:
                    entry["rows"].append(idx)
    return result
