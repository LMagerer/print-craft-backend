import io

import pytest
from PIL import Image

from app.schemas.config import Box, Placeholder, SizeConfig, TemplateConfig
from app.schemas.image import CropBox, TransformRequest
from app.services import image_tools as it


def _img(w=1000, h=500, mode="RGB", icc=None):
    img = Image.new(mode, (w, h), (10, 20, 30) if mode == "RGB" else (10, 20, 30, 128))
    if icc:
        img.info["icc_profile"] = icc
    return img


def _cfg(w_mm=100.0, h_mm=50.0):
    return TemplateConfig(
        size=SizeConfig(width_mm=w_mm, height_mm=h_mm, dpi=254),
        placeholders=[Placeholder(id="a", x_mm=10, y_mm=20, box=Box(width_mm=30, height_mm=10))],
    )


def test_parse_color():
    assert it.parse_color("#fff") == (255, 255, 255, 255)
    assert it.parse_color("#11223380") == (0x11, 0x22, 0x33, 0x80)
    assert it.parse_color("rgba(1, 2, 3, 0.5)") == (1, 2, 3, 128)
    with pytest.raises(ValueError):
        it.parse_color("blue")


def test_print_size_only_is_lossless():
    img = _img(1000, 500)
    res = it.transform_image(img, TransformRequest(source_ref="x", width_mm=50.8, height_mm=999))
    assert res.image.size == (1000, 500)
    assert res.dpi == pytest.approx(500)
    # Height follows the pixel aspect ratio, not the requested value.
    assert res.height_mm == pytest.approx(25.4)
    assert any("Height adjusted" in w for w in res.warnings)
    assert res.image.tobytes() == img.tobytes()


def test_resample_size_and_warnings():
    img = _img(400, 200)
    req = TransformRequest(source_ref="x", mode="resample", width_mm=85.6, height_mm=42.8, target_dpi=300)
    res = it.transform_image(img, req)
    assert res.image.size == (1011, 506)
    assert res.dpi == 300
    assert any("Upscaling" in w for w in res.warnings)
    assert any("DPI" in w for w in res.warnings)


def test_cover_and_contain_math():
    assert it.cover_crop_box(1000, 500, 1.0, "center") == (250, 0, 500, 500)
    assert it.cover_crop_box(1000, 500, 1.0, "left") == (0, 0, 500, 500)
    assert it.cover_crop_box(500, 1000, 1.0, "bottom") == (0, 500, 500, 500)
    assert it.contain_pad(1000, 500, 1.0, "center") == (1000, 1000, 0, 250)
    assert it.contain_pad(1000, 500, 1.0, "top") == (1000, 1000, 0, 0)


def test_crop_fit_explicit_box_and_placeholder_mapping():
    img = _img(1000, 500)  # 100 x 50 mm at 254 DPI -> 10 px/mm
    cfg = _cfg()
    req = TransformRequest(
        source_ref="x", mode="crop_fit", width_mm=50, height_mm=25, target_dpi=254,
        crop=CropBox(x=50, y=100, width=500, height=250), config=cfg,
    )
    res = it.transform_image(img, req)
    assert res.image.size == (500, 250)
    new_cfg = it.remap_config(cfg, img.size, res)
    p = new_cfg.placeholders[0]
    # Crop origin (5 mm, 10 mm) is subtracted; scale is unchanged.
    assert p.x_mm == pytest.approx(5)
    assert p.y_mm == pytest.approx(10)
    assert p.box.width_mm == pytest.approx(30)
    assert new_cfg.size.width_mm == pytest.approx(50)


def test_print_size_change_scales_placeholders_proportionally():
    img = _img(1000, 500)
    cfg = _cfg()
    res = it.transform_image(img, TransformRequest(source_ref="x", width_mm=200, height_mm=100, config=cfg))
    new_cfg = it.remap_config(cfg, img.size, res)
    p = new_cfg.placeholders[0]
    assert (p.x_mm, p.y_mm, p.box.width_mm) == pytest.approx((20, 40, 60))
    assert p.font.size_pt == pytest.approx(24)


def test_proportional_scaling_helper():
    cfg = _cfg()
    out = it.scale_placeholders_proportional(cfg, 50, 25)
    p = out.placeholders[0]
    assert (p.x_mm, p.y_mm, p.box.height_mm, p.font.size_pt) == pytest.approx((5, 10, 5, 6))


def test_bleed_add_and_trim():
    img = _img(1000, 500)
    res = it.transform_image(img, TransformRequest(source_ref="x", width_mm=100, height_mm=50, bleed_mm=2))
    # 2 mm at 254 DPI = 20 px on each side
    assert res.image.size == (1040, 540)
    assert res.bleed_mm == pytest.approx(2)
    res2 = it.transform_image(res.image, TransformRequest(source_ref="x", width_mm=104, height_mm=54, bleed_mm=-2))
    assert res2.image.size == (1000, 500)


def test_rotate_and_alpha_background():
    img = _img(1000, 500, mode="RGBA")
    res = it.transform_image(img, TransformRequest(source_ref="x", width_mm=50, height_mm=100, rotate=90, background="#ffffff"))
    assert res.image.size == (500, 1000)
    assert res.image.mode == "RGB"
    assert res.px_map is None


def test_icc_and_dpi_written(srgb_icc):
    img = _img(100, 50, icc=srgb_icc)
    res = it.transform_image(img, TransformRequest(source_ref="x", mode="resample", width_mm=20, height_mm=10, target_dpi=300))
    data = it.png_bytes(res.image, res.dpi, res.icc)
    out = Image.open(io.BytesIO(data))
    assert out.info["icc_profile"] == srgb_icc
    assert out.info["dpi"][0] == pytest.approx(300, abs=0.01)
