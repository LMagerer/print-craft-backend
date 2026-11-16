from pathlib import Path

import pytest
from pypdf import PdfReader

from app.schemas.api import LayoutRequest, Margins
from app.services.pdf_layout import compute_layout, crop_mark_lines, render_pdf
from tests.conftest import make_png


def test_credit_card_grid_on_a4():
    lay = compute_layout(LayoutRequest(card_width_mm=85.6, card_height_mm=54, card_count=25, allow_rotate=False))
    assert (lay.columns, lay.rows, lay.per_page, lay.pages) == (2, 5, 10, 3)
    # Grid is centred in the usable area
    left = lay.slots[0].x_mm
    right = 210 - (lay.slots[1].x_mm + 85.6)
    assert left == pytest.approx(right)


def test_rotation_chosen_when_more_fit():
    req = LayoutRequest(card_width_mm=120, card_height_mm=60, margins=Margins(top=5, right=5, bottom=5, left=5))
    assert compute_layout(req.model_copy(update={"allow_rotate": False})).per_page == 4
    lay = compute_layout(req)
    assert lay.rotated and lay.per_page == 6
    assert lay.slot_width_mm == 60


def test_card_too_big():
    assert compute_layout(LayoutRequest(card_width_mm=300, card_height_mm=300, allow_rotate=True)).per_page == 0


def test_exact_fit_with_gap():
    lay = compute_layout(LayoutRequest(card_width_mm=95, card_height_mm=50, gap_mm=0, margins=Margins(top=10, right=10, bottom=10, left=10)))
    assert lay.columns == 2


def test_crop_marks_at_trim_line():
    lay = compute_layout(LayoutRequest(card_width_mm=90, card_height_mm=60, allow_rotate=False))
    lines = crop_mark_lines(lay, bleed_mm=3)
    xs = {round(l[0], 3) for l in lines if l[0] == l[2]}
    assert round(lay.slots[0].x_mm + 3, 3) in xs
    assert round(lay.slots[0].x_mm + 90 - 3, 3) in xs


def test_render_pdf_pages(tmp_path):
    imgs = []
    for i in range(5):
        p = tmp_path / f"{i}.png"
        p.write_bytes(make_png(100, 60))
        imgs.append(p)
    lay = compute_layout(LayoutRequest(card_width_mm=100, card_height_mm=60, card_count=5, allow_rotate=False))
    out = tmp_path / "o.pdf"
    seen = []
    assert render_pdf(imgs, lay, out, on_progress=seen.append)
    reader = PdfReader(str(out))
    assert len(reader.pages) == -(-5 // lay.per_page)
    assert float(reader.pages[0].mediabox.width) == pytest.approx(210 / 25.4 * 72, abs=0.1)
    assert seen[-1] == 5


def _embedded_images(pdf: Path) -> list[tuple[int, int, object]]:
    out = []
    for page in PdfReader(str(pdf)).pages:
        for obj in page["/Resources"]["/XObject"].values():
            o = obj.get_object()
            out.append((int(o["/Width"]), int(o["/Height"]), o.get("/Filter")))
    return out


@pytest.mark.parametrize("rotate", [False, True])
def test_render_pdf_downsamples_and_compresses(tmp_path, rotate):
    # 100 x 60 mm at ~1200 DPI with alpha; 300 DPI target = 1181 x 709 px.
    src = tmp_path / "big.png"
    src.write_bytes(make_png(4724, 2835, mode="RGBA"))
    lay = compute_layout(LayoutRequest(card_width_mm=100, card_height_mm=60, card_count=2, allow_rotate=rotate))
    lossless, small = tmp_path / "a.pdf", tmp_path / "b.pdf"
    assert render_pdf([src, src], lay, lossless, dpi=None)
    assert render_pdf([src, src], lay, small, dpi=300, image_format="jpeg", jpeg_quality=85)

    assert {(w, h) for w, h, _ in _embedded_images(lossless)} == {(4724, 2835)}
    w, h, flt = _embedded_images(small)[0]
    assert (w, h) == (1181, 709)
    assert "/DCTDecode" in str(flt)
    assert small.stat().st_size < lossless.stat().st_size


def test_render_pdf_keeps_low_res_cards(tmp_path):
    src = tmp_path / "small.png"
    src.write_bytes(make_png(200, 120))
    lay = compute_layout(LayoutRequest(card_width_mm=100, card_height_mm=60, card_count=1))
    out = tmp_path / "o.pdf"
    assert render_pdf([src], lay, out, dpi=300)
    assert [(w, h) for w, h, _ in _embedded_images(out)] == [(200, 120)]
