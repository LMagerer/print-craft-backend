"""Deterministic text layout shared by the Pillow renderer and the browser canvas.

*** Keep in sync with ``frontend/src/lib/textLayout.ts`` (same algorithm, same operation order). ***

All measurements come from font-unit advance widths (``FontMetrics``), never from a rasteriser, so both
implementations produce identical line breaks, font sizes and glyph positions for the same input.

Units: every length here is in *template pixels* (pixels of the full-resolution template image).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from app.services.font_manager import FontMetrics

EPS = 0.01
SHRINK_STEP_PT = 0.5


@dataclass
class Glyph:
    char: str
    x: float
    # 0 = primary font, 1 = fallback font
    font: int


@dataclass
class Line:
    text: str
    width: float
    x: float
    baseline: float
    glyphs: list[Glyph] = field(default_factory=list)


@dataclass
class TextLayout:
    size_px: float
    line_height_px: float
    lines: list[Line]
    overflow: bool
    missing: list[str]


@dataclass
class LayoutParams:
    box_x: float
    box_y: float
    box_w: float
    box_h: float
    size_px: float
    min_size_px: float
    # Size decrement per auto-shrink step, in px (0.5 pt at the template DPI).
    step_px: float
    letter_spacing_px: float = 0.0
    line_height: float = 1.2
    wrap: bool = False
    auto_shrink: bool = False
    align: str = "left"
    valign: str = "top"


def is_ignorable(ch: str) -> bool:
    """Control characters never need a glyph."""
    return unicodedata.category(ch) == "Cc"


class Measurer:
    def __init__(self, primary: FontMetrics, fallback: FontMetrics | None = None) -> None:
        self.primary = primary
        self.fallback = fallback

    def pick(self, ch: str) -> int:
        cp = ord(ch)
        if self.primary.has(cp):
            return 0
        if self.fallback is not None and self.fallback.has(cp):
            return 1
        return 0

    def advance(self, ch: str, size_px: float) -> float:
        cp = ord(ch)
        if self.primary.has(cp):
            return self.primary.advances[cp] * size_px / self.primary.units_per_em
        if self.fallback is not None and self.fallback.has(cp):
            return self.fallback.advances[cp] * size_px / self.fallback.units_per_em
        return self.primary.notdef_advance * size_px / self.primary.units_per_em

    def width(self, text: str, size_px: float, letter_spacing_px: float) -> float:
        w = 0.0
        n = 0
        for ch in text:
            w += self.advance(ch, size_px)
            n += 1
        if n > 1:
            w += letter_spacing_px * (n - 1)
        return w

    def missing(self, text: str) -> list[str]:
        """Characters that neither the primary nor the fallback font can draw."""
        out: list[str] = []
        for ch in text:
            if is_ignorable(ch) or ch in out:
                continue
            cp = ord(ch)
            if not self.primary.has(cp) and not (self.fallback is not None and self.fallback.has(cp)):
                out.append(ch)
        return out


def break_lines(m: Measurer, text: str, size_px: float, ls_px: float, max_w: float, wrap: bool) -> list[str]:
    """Split on explicit newlines; when ``wrap`` is on, greedily word-wrap (breaking long words by character)."""
    out: list[str] = []
    for para in text.split("\n"):
        if not wrap:
            out.append(para)
            continue
        line: str | None = None
        for word in para.split(" "):
            cand = word if line is None else line + " " + word
            if m.width(cand, size_px, ls_px) <= max_w + EPS:
                line = cand
                continue
            if line is not None:
                out.append(line)
                line = None
            if m.width(word, size_px, ls_px) <= max_w + EPS:
                line = word
                continue
            chunk = ""
            for ch in word:
                if chunk != "" and m.width(chunk + ch, size_px, ls_px) > max_w + EPS:
                    out.append(chunk)
                    chunk = ch
                else:
                    chunk += ch
            line = chunk
        if line is not None:
            out.append(line)
    return out


def layout_text(text: str, p: LayoutParams, primary: FontMetrics, fallback: FontMetrics | None = None) -> TextLayout:
    m = Measurer(primary, fallback)
    size = p.size_px
    min_size = min(p.min_size_px, size) if p.auto_shrink else size
    while True:
        ls = p.letter_spacing_px * size / p.size_px
        lines = break_lines(m, text, size, ls, p.box_w, p.wrap)
        line_h = size * p.line_height
        widest = 0.0
        for ln in lines:
            w = m.width(ln, size, ls)
            if w > widest:
                widest = w
        fits = widest <= p.box_w + EPS and len(lines) * line_h <= p.box_h + EPS
        if fits or size <= min_size + 1e-9:
            break
        size = max(min_size, size - p.step_px)

    block_h = len(lines) * line_h
    if p.valign == "middle":
        y0 = p.box_y + (p.box_h - block_h) / 2
    elif p.valign == "bottom":
        y0 = p.box_y + p.box_h - block_h
    else:
        y0 = p.box_y
    asc = primary.ascender * size / primary.units_per_em
    desc = primary.descender * size / primary.units_per_em
    baseline_off = (line_h - (asc + desc)) / 2 + asc

    out: list[Line] = []
    for i, text_line in enumerate(lines):
        w = m.width(text_line, size, ls)
        if p.align == "center":
            x = p.box_x + (p.box_w - w) / 2
        elif p.align == "right":
            x = p.box_x + p.box_w - w
        else:
            x = p.box_x
        glyphs: list[Glyph] = []
        gx = x
        for ch in text_line:
            glyphs.append(Glyph(ch, gx, m.pick(ch)))
            gx += m.advance(ch, size) + ls
        out.append(Line(text_line, w, x, y0 + i * line_h + baseline_off, glyphs))

    return TextLayout(size, line_h, out, overflow=not fits, missing=m.missing(text))
