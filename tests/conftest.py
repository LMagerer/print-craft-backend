from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

import pytest

# Isolate all on-disk state before the app modules read settings.
_TMP = Path(tempfile.mkdtemp(prefix="tc-test-"))
os.environ["TC_DATA_DIR"] = str(_TMP / "data")
os.environ["TC_DATABASE_URL"] = "sqlite://"
os.environ["TC_AUTO_MIGRATE"] = "false"
os.environ["TC_FRONTEND_DIST"] = str(_TMP / "no-frontend")

from fontTools.ttLib import TTFont  # noqa: E402
from PIL import Image, ImageCms  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.services.font_manager import FontFace, FontLibrary, read_names, style_from  # noqa: E402

FONTS_DIR = Path(__file__).resolve().parents[1] / "fonts"


@pytest.fixture(scope="session")
def font_library() -> FontLibrary:
    faces = []
    for i, p in enumerate(sorted(FONTS_DIR.glob("*.ttf")), start=1):
        f = TTFont(str(p), lazy=True)
        fam, sub = read_names(f)
        faces.append(FontFace(i, fam, style_from(f, sub), str(p)))
    return FontLibrary(faces)


@pytest.fixture(scope="session")
def srgb_icc() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def make_png(w: int, h: int, color=(30, 60, 200), mode: str = "RGB", dpi: float | None = 300, icc: bytes | None = None) -> bytes:
    img = Image.new(mode, (w, h), color if mode == "RGB" else color + (255,) if len(color) == 3 else color)
    buf = io.BytesIO()
    kw: dict = {"format": "PNG"}
    if dpi:
        kw["dpi"] = (dpi, dpi)
    if icc:
        kw["icc_profile"] = icc
    img.save(buf, **kw)
    return buf.getvalue()


@pytest.fixture(scope="session", autouse=True)
def _dirs():
    get_settings().ensure_dirs()
    yield
