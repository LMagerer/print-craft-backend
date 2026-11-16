import pytest

from app.services.font_manager import FontMetrics
from app.services.text_layout import LayoutParams, layout_text

# Synthetic monospace font: every glyph is 500 units wide on a 1000 upm em -> 0.5 * size px.
MONO = FontMetrics(1000, 800, 200, {cp: 500 for cp in range(32, 127)}, 500)
FALLBACK = FontMetrics(1000, 800, 200, {ord("é"): 600}, 600)


def params(**kw):
    base = dict(box_x=0, box_y=0, box_w=100, box_h=100, size_px=20, min_size_px=20, step_px=1)
    base.update(kw)
    return LayoutParams(**base)


def test_single_line_alignment():
    lay = layout_text("abcd", params(align="center"), MONO)  # width 40
    assert lay.lines[0].width == pytest.approx(40)
    assert lay.lines[0].x == pytest.approx(30)
    lay = layout_text("abcd", params(align="right"), MONO)
    assert lay.lines[0].x == pytest.approx(60)


def test_letter_spacing_between_glyphs():
    lay = layout_text("abc", params(letter_spacing_px=2), MONO)
    assert [g.x for g in lay.lines[0].glyphs] == pytest.approx([0, 12, 24])
    assert lay.lines[0].width == pytest.approx(34)


def test_wrap_greedy_and_long_word_break():
    lay = layout_text("aa bb cc", params(box_w=50, wrap=True), MONO)  # "aa bb" = 50
    assert [ln.text for ln in lay.lines] == ["aa bb", "cc"]
    lay = layout_text("abcdefghijkl", params(box_w=50, wrap=True), MONO)  # 5 chars per line
    assert [ln.text for ln in lay.lines] == ["abcde", "fghij", "kl"]


def test_explicit_newlines_without_wrap():
    lay = layout_text("a\nb", params(), MONO)
    assert [ln.text for ln in lay.lines] == ["a", "b"]
    assert lay.lines[1].baseline - lay.lines[0].baseline == pytest.approx(24)


def test_auto_shrink_stops_at_fit_and_at_min():
    # 20 chars * 0.5 * size must fit 100 px -> size 10
    lay = layout_text("a" * 20, params(auto_shrink=True, min_size_px=5), MONO)
    assert lay.size_px == pytest.approx(10)
    assert not lay.overflow
    lay = layout_text("a" * 40, params(auto_shrink=True, min_size_px=8), MONO)
    assert lay.size_px == pytest.approx(8)
    assert lay.overflow


def test_vertical_alignment_and_baseline():
    lay = layout_text("a", params(valign="middle", line_height=1.0), MONO)
    # line 20px centred in 100 -> top 40; asc 16 + desc 4 fill the line -> baseline 56
    assert lay.lines[0].baseline == pytest.approx(56)


def test_fallback_and_missing_glyphs():
    lay = layout_text("aé☃", params(), MONO, FALLBACK)
    assert [g.font for g in lay.lines[0].glyphs] == [0, 1, 0]
    assert lay.missing == ["☃"]
    assert lay.lines[0].width == pytest.approx(10 + 12 + 10)
