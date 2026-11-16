import pytest

from app.core import units


def test_mm_conversions_roundtrip():
    assert units.to_mm(1, "in") == pytest.approx(25.4)
    assert units.to_mm(8.56, "cm") == pytest.approx(85.6)
    assert units.to_mm(72, "pt") == pytest.approx(25.4)
    assert units.to_mm(300, "px", dpi=300) == pytest.approx(25.4)
    for unit in ("mm", "cm", "in", "pt"):
        assert units.to_mm(units.from_mm(54.0, unit), unit) == pytest.approx(54.0)
    assert units.from_mm(25.4, "px", dpi=600) == pytest.approx(600)


def test_px_requires_dpi():
    with pytest.raises(ValueError):
        units.to_mm(10, "px")


def test_dpi_and_pixels():
    # 1012 px over 85.6 mm is ~300 DPI (CR80 card)
    assert units.dpi_for(1012, 85.6) == pytest.approx(300.28, abs=0.01)
    assert units.pixels_for(85.6, 300) == 1011
    assert units.pixels_for(54.0, 300) == 638
    assert units.pt_to_px(12, 300) == pytest.approx(50)


def test_summary():
    assert units.summary(85.6, 54.0, 1012, 638) == "85.6 x 54.0 mm | 1012 x 638 px | 300 DPI"
