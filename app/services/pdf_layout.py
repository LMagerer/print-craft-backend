"""A4 (or any page size) grid layout math and multi-page PDF generation."""

from __future__ import annotations

import math
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

from PIL import Image

from reportlab import rl_config
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas

from app.schemas.api import CardSlot, LayoutRequest, LayoutResponse

# Binary image streams; ASCII85 would inflate every embedded image by 25%.
rl_config.useA85 = 0

EPS = 1e-6
MARK_LEN_MM = 5.0
MARK_OFFSET_MM = 1.5


def _grid(usable_w: float, usable_h: float, w: float, h: float, gap: float) -> tuple[int, int]:
    cols = max(0, math.floor((usable_w + gap + EPS) / (w + gap)))
    rows = max(0, math.floor((usable_h + gap + EPS) / (h + gap)))
    return cols, rows


def compute_layout(req: LayoutRequest) -> LayoutResponse:
    """Cards per row/column on the page; rotates cards 90 degrees when that fits more (if allowed).

    The grid is centred inside the margins. Slot coordinates are in mm from the page's top-left corner.
    """
    m = req.margins
    uw = req.page_width_mm - m.left - m.right
    uh = req.page_height_mm - m.top - m.bottom
    cw, ch, gap = req.card_width_mm, req.card_height_mm, req.gap_mm
    cols, rows = _grid(uw, uh, cw, ch, gap)
    rotated = False
    if req.allow_rotate:
        rc, rr = _grid(uw, uh, ch, cw, gap)
        if rc * rr > cols * rows:
            cols, rows, rotated = rc, rr, True
    sw, sh = (ch, cw) if rotated else (cw, ch)
    per_page = cols * rows
    grid_w = cols * sw + max(0, cols - 1) * gap
    grid_h = rows * sh + max(0, rows - 1) * gap
    x0 = m.left + (uw - grid_w) / 2
    y0 = m.top + (uh - grid_h) / 2
    slots = [
        CardSlot(x_mm=round(x0 + c * (sw + gap), 4), y_mm=round(y0 + r * (sh + gap), 4), width_mm=sw, height_mm=sh)
        for r in range(rows)
        for c in range(cols)
    ]
    pages = math.ceil(req.card_count / per_page) if per_page and req.card_count else 0
    return LayoutResponse(
        columns=cols, rows=rows, per_page=per_page, rotated=rotated, pages=pages,
        page_width_mm=req.page_width_mm, page_height_mm=req.page_height_mm,
        slot_width_mm=sw, slot_height_mm=sh, slots=slots,
    )


def crop_mark_lines(layout: LayoutResponse, bleed_mm: float) -> list[tuple[float, float, float, float]]:
    """Crop marks (x1, y1, x2, y2 in mm from top-left) at the trim lines, drawn outside the card grid."""
    if not layout.slots:
        return []
    b = bleed_mm
    xs = sorted({round(v, 4) for s in layout.slots for v in (s.x_mm + b, s.x_mm + s.width_mm - b)})
    ys = sorted({round(v, 4) for s in layout.slots for v in (s.y_mm + b, s.y_mm + s.height_mm - b)})
    left = min(s.x_mm for s in layout.slots)
    right = max(s.x_mm + s.width_mm for s in layout.slots)
    top = min(s.y_mm for s in layout.slots)
    bottom = max(s.y_mm + s.height_mm for s in layout.slots)
    pw, ph = layout.page_width_mm, layout.page_height_mm
    lines = []
    for x in xs:
        lines.append((x, max(0, top - MARK_OFFSET_MM - MARK_LEN_MM), x, max(0, top - MARK_OFFSET_MM)))
        lines.append((x, min(ph, bottom + MARK_OFFSET_MM), x, min(ph, bottom + MARK_OFFSET_MM + MARK_LEN_MM)))
    for y in ys:
        lines.append((max(0, left - MARK_OFFSET_MM - MARK_LEN_MM), y, max(0, left - MARK_OFFSET_MM), y))
        lines.append((min(pw, right + MARK_OFFSET_MM), y, min(pw, right + MARK_OFFSET_MM + MARK_LEN_MM), y))
    return [ln for ln in lines if abs(ln[0] - ln[2]) > EPS or abs(ln[1] - ln[3]) > EPS]


def prepare_image(
    src: Path,
    width_mm: float,
    height_mm: float,
    dpi: int | None,
    image_format: Literal["png", "jpeg"],
    jpeg_quality: int,
    tmp_dir: Path,
) -> Path:
    """Downsample ``src`` to at most ``dpi`` at the given physical size and re-encode it if requested.

    Returns ``src`` unchanged when nothing needs to be done (original resolution, lossless).
    """
    with Image.open(src) as im:
        target: tuple[int, int] | None = None
        if dpi is not None:
            tw = max(1, round(width_mm / 25.4 * dpi))
            th = max(1, round(height_mm / 25.4 * dpi))
            if im.width > tw or im.height > th:
                target = (tw, th)
        if target is None and image_format == "png":
            return src
        out = im.convert("RGBA") if im.mode in ("P", "LA", "PA") else im.copy()
        if target is not None:
            out = out.resize(target, Image.Resampling.LANCZOS)
        if image_format == "jpeg":
            if out.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", out.size, (255, 255, 255))
                bg.paste(out, mask=out.getchannel("A"))
                out = bg
            elif out.mode != "RGB":
                out = out.convert("RGB")
            dst = tmp_dir / f"{src.stem}.jpg"
            # ReportLab embeds JPEG files as-is (DCTDecode), so the PDF keeps this compression.
            out.save(dst, "JPEG", quality=jpeg_quality, optimize=True, subsampling=0 if jpeg_quality >= 90 else 2)
        else:
            dst = tmp_dir / f"{src.stem}.png"
            out.save(dst, "PNG")
        return dst


def render_pdf(
    images: Sequence[Path],
    layout: LayoutResponse,
    out_path: Path,
    crop_marks: bool = True,
    bleed_mm: float = 0.0,
    on_progress: Callable[[int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    dpi: int | None = None,
    image_format: Literal["png", "jpeg"] = "png",
    jpeg_quality: int = 90,
) -> bool:
    """Write the PDF page by page.

    Cards are downsampled to ``dpi`` (None = original pixels) and embedded either losslessly (PNG -> Flate)
    or as JPEG. Returns False when cancelled.
    """
    with tempfile.TemporaryDirectory(prefix="tc-pdf-") as tmp:
        return _render_pdf(
            images, layout, out_path, crop_marks, bleed_mm, on_progress, should_cancel,
            lambda img, w, h: prepare_image(img, w, h, dpi, image_format, jpeg_quality, Path(tmp)),
        )


def _render_pdf(
    images: Sequence[Path],
    layout: LayoutResponse,
    out_path: Path,
    crop_marks: bool,
    bleed_mm: float,
    on_progress: Callable[[int], None] | None,
    should_cancel: Callable[[], bool] | None,
    prepare: Callable[[Path, float, float], Path],
) -> bool:
    if layout.per_page == 0:
        raise ValueError("The card does not fit on the page with these margins")
    pw, ph = layout.page_width_mm, layout.page_height_mm
    c = rl_canvas.Canvas(str(out_path), pagesize=(pw * mm, ph * mm), pageCompression=1)
    c.setTitle("Cards")
    c.setCreator("PrintCraft")
    marks = crop_mark_lines(layout, bleed_mm) if crop_marks else []
    for i, img in enumerate(images):
        if should_cancel and should_cancel():
            return False
        slot = layout.slots[i % layout.per_page]
        x = slot.x_mm * mm
        y = (ph - slot.y_mm - slot.height_mm) * mm
        w, h = slot.width_mm * mm, slot.height_mm * mm
        # The image is in card orientation; rotated slots are swapped.
        card_w_mm, card_h_mm = (slot.height_mm, slot.width_mm) if layout.rotated else (slot.width_mm, slot.height_mm)
        img = prepare(img, card_w_mm, card_h_mm)
        if layout.rotated:
            # Rotate 90 deg counter-clockwise into the (landscape <-> portrait) slot.
            c.saveState()
            c.translate(x + w, y)
            c.rotate(90)
            c.drawImage(str(img), 0, 0, width=h, height=w, mask="auto")
            c.restoreState()
        else:
            c.drawImage(str(img), x, y, width=w, height=h, mask="auto")
        last_on_page = (i + 1) % layout.per_page == 0 or i == len(images) - 1
        if last_on_page:
            if marks:
                c.setLineWidth(0.25)
                c.setStrokeColorRGB(0, 0, 0)
                for x1, y1, x2, y2 in marks:
                    c.line(x1 * mm, (ph - y1) * mm, x2 * mm, (ph - y2) * mm)
            c.showPage()
        if on_progress:
            on_progress(i + 1)
    c.save()
    return True
