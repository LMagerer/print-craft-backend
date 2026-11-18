import io

import pytest
from PIL import Image

from app.schemas.config import Binding, Box, FontConfig, Placeholder, Shadow, SizeConfig, TemplateConfig
from app.services.card_renderer import CardRenderer, glyph_check


def _config(font_id=None, **font_kw):
    return TemplateConfig(
        size=SizeConfig(width_mm=85.6, height_mm=54.0, dpi=300),
        placeholders=[
            Placeholder(
                id="name", binding=Binding(field="name"), x_mm=10, y_mm=20, box=Box(width_mm=65, height_mm=10),
                align="center", valign="middle", font=FontConfig(font_id=font_id, size_pt=18, color="#ffffff", **font_kw),
            )
        ],
    )


def test_output_size_dpi_icc_preserved(font_library, srgb_icc):
    base = Image.new("RGB", (1011, 638), (0, 0, 80))
    base.info["icc_profile"] = srgb_icc
    card = CardRenderer(base, _config(), font_library).render({"name": "Ada Lovelace"})
    out = Image.open(io.BytesIO(card.to_png()))
    assert out.size == (1011, 638)
    assert out.mode == "RGB"
    assert out.info["icc_profile"] == srgb_icc
    assert out.info["dpi"][0] == pytest.approx(300, abs=0.5)
    # White text was drawn inside the box
    box = out.crop((118, 236, 886, 354))
    assert box.getextrema()[0][1] > 200


def test_alpha_preserved_and_shadow_stroke(font_library):
    base = Image.new("RGBA", (500, 300), (0, 0, 0, 0))
    cfg = TemplateConfig(
        size=SizeConfig(width_mm=50, height_mm=30, dpi=254),
        placeholders=[
            Placeholder(
                id="t", binding=Binding(field="t"), x_mm=5, y_mm=5, box=Box(width_mm=40, height_mm=20),
                font=FontConfig(size_pt=20, color="#ff000080", stroke_width_pt=1, stroke_color="#00ff00",
                                shadow=Shadow(enabled=True, color="#0000ff"), bold=True),
            )
        ],
    )
    card = CardRenderer(base, cfg, font_library).render({"t": "Hi"})
    assert card.image.mode == "RGBA"
    lo, hi = card.image.getchannel("A").getextrema()
    assert lo == 0 and hi > 0


def test_auto_shrink_report_and_overflow(font_library):
    cfg = _config(min_size_pt=6)
    r = CardRenderer(Image.new("RGB", (1011, 638)), cfg, font_library)
    short = r.render({"name": "Al"}).reports[0]
    long_ = r.render({"name": "A very long name that will never fit into the box " * 3}).reports[0]
    assert short.size_pt == 18 and not short.overflow
    assert long_.size_pt == 6 and long_.overflow
    assert r.render({}).reports[0].missing_fields == ["name"]


def test_glyph_check_reports_missing(font_library):
    cfg = _config()
    res = glyph_check(cfg, font_library, [{"name": "ok"}, {"name": "snow ☃"}, {"name": "☃☃"}])
    assert res["name"]["missing"] == {"☃": 2}
    assert res["name"]["rows"] == [1, 2]


def test_variant_resolution(font_library):
    regular = font_library.resolve(None)
    assert regular.family == "Noto Sans" and regular.style == "regular"
    roboto = next(f for f in font_library.faces.values() if f.family == "Roboto" and f.style == "regular")
    assert font_library.resolve(roboto.id, bold=True, italic=True).style == "bolditalic"


def _layer_config(**kw):
    from app.schemas.config import Layer, ShapeStyle

    return TemplateConfig(
        size=SizeConfig(width_mm=100, height_mm=50, dpi=254),  # 10 px per mm
        background="#ffffff",
        placeholders=[Placeholder(id="t", binding=Binding(mode="template", template="X"), x_mm=0, y_mm=0, box=Box(width_mm=20, height_mm=10),
                                  font=FontConfig(size_pt=20, color="#000000"))],
        layers=[
            Layer(id="r", kind="shape", x_mm=10, y_mm=10, width_mm=20, height_mm=10, shape=ShapeStyle(type="rect", fill="#ff0000")),
            Layer(id="t", kind="text"),
            # Covers the text: the stack order must put it on top.
            Layer(id="e", kind="shape", x_mm=0, y_mm=0, width_mm=20, height_mm=10, shape=ShapeStyle(type="rect", fill="#00ff00"), opacity=0.5),
        ],
        **kw,
    )


def test_layers_background_order_and_opacity(font_library):
    base = Image.new("RGBA", (1000, 500), (0, 0, 0, 0))
    card = CardRenderer(base, _layer_config(), font_library).render({})
    img = card.image
    assert img.mode == "RGB"  # opaque background drops alpha
    assert img.getpixel((500, 400)) == (255, 255, 255)
    assert img.getpixel((200, 150)) == (255, 0, 0)
    # Half-transparent green over white (and over the text, which is below it).
    r, g, b = img.getpixel((195, 95))
    assert g > 200 and r < 140 and abs(r - b) < 3


def test_rotated_image_layer_and_photo_lookup(font_library, tmp_path):
    from app.schemas.config import ImageSource, Layer
    from app.services import image_tools
    from app.services.photo_library import PhotoResolver

    buf = io.BytesIO()
    Image.new("RGB", (40, 20), (0, 0, 255)).save(buf, format="PNG")
    blue = image_tools.store_image(buf.getvalue()).ref
    cfg = TemplateConfig(
        size=SizeConfig(width_mm=100, height_mm=50, dpi=254),
        layers=[Layer(id="p", kind="image", x_mm=40, y_mm=20, width_mm=20, height_mm=10, rotation=90,
                      image=ImageSource(mode="field", field="photo", fit="fill"))],
    )
    base = Image.new("RGB", (1000, 500), (255, 255, 255))
    r = CardRenderer(base, cfg, font_library, PhotoResolver({"Ada.PNG": blue}))
    card = r.render({"photo": "ada"})
    assert not card.warnings
    # Rotated 90°: the 200x100 px box becomes 100 wide x 200 tall around its centre (500, 250).
    assert card.image.getpixel((500, 170)) == (0, 0, 255)
    assert card.image.getpixel((420, 250)) == (255, 255, 255)
    missing = r.render({"photo": "nobody.jpg"})
    assert missing.warnings and "nobody.jpg" in missing.warnings[0]
