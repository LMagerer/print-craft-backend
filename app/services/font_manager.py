"""Font handling: metrics extraction, variant resolution, bundled/system font discovery and registration.

Text layout never asks FreeType or the browser for measurements. Both renderers use the *unhinted*
advance widths from the font's ``hmtx`` table (exposed to the browser via ``GET /fonts/{id}/metrics``),
so line breaks, auto-shrink and glyph positions are identical in the canvas preview and the Pillow output.
"""

from __future__ import annotations

import os
import platform
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from fontTools.ttLib import TTFont, TTLibError
from PIL import ImageFont
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Font
from app.services.storage import sha256_bytes, store_bytes

FONT_EXTS = {".ttf", ".otf"}
STYLES = ("regular", "bold", "italic", "bolditalic")
DEFAULT_FAMILY = "Noto Sans"


class FontError(ValueError):
    pass


@dataclass(frozen=True)
class FontMetrics:
    units_per_em: int
    ascender: int
    # Positive distance below the baseline.
    descender: int
    # Unicode code point -> advance width (font units).
    advances: dict[int, int] = field(hash=False, compare=False)
    notdef_advance: int = 0

    def has(self, cp: int) -> bool:
        return cp in self.advances

    def to_json(self) -> dict[str, Any]:
        return {
            "units_per_em": self.units_per_em,
            "ascender": self.ascender,
            "descender": self.descender,
            "notdef_advance": self.notdef_advance,
            # JSON keys must be strings; the frontend parses them back to numbers.
            "advances": {str(k): v for k, v in self.advances.items()},
        }


def read_names(font: TTFont) -> tuple[str, str]:
    """(family, subfamily) using typographic names (16/17) when present."""
    name = font["name"]
    family = name.getDebugName(16) or name.getDebugName(1) or "Unknown"
    sub = name.getDebugName(17) or name.getDebugName(2) or "Regular"
    return family.strip(), sub.strip()


def style_from(font: TTFont, subfamily: str) -> str:
    bold = italic = False
    if "OS/2" in font:
        sel = font["OS/2"].fsSelection
        italic = bool(sel & 0x01)
        bold = bool(sel & 0x20)
    s = subfamily.lower()
    bold = bold or "bold" in s
    italic = italic or "italic" in s or "oblique" in s
    return ("bold" if bold else "") + ("italic" if italic else "") or "regular"


@lru_cache(maxsize=64)
def load_metrics(path: str) -> FontMetrics:
    """Read advance widths and vertical metrics with fontTools (cached per file)."""
    try:
        f = TTFont(path, lazy=True, fontNumber=0)
    except (TTLibError, OSError) as e:
        raise FontError(f"Cannot read font {path}: {e}") from e
    cmap = f.getBestCmap() or {}
    hmtx = f["hmtx"].metrics
    advances = {cp: hmtx[g][0] for cp, g in cmap.items() if g in hmtx}
    hhea = f["hhea"]
    notdef = hmtx.get(".notdef", (0, 0))[0]
    upm = f["head"].unitsPerEm
    f.close()
    return FontMetrics(upm, int(hhea.ascent), abs(int(hhea.descent)), advances, notdef)


_pil_lock = threading.Lock()


@lru_cache(maxsize=256)
def _pil_font(path: str, size_px_q: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size_px_q / 64, layout_engine=ImageFont.Layout.BASIC)


def pil_font(path: str, size_px: float) -> ImageFont.FreeTypeFont:
    """Pillow font object at ``size_px`` (quantised to 1/64 px, FreeType's native resolution)."""
    with _pil_lock:
        return _pil_font(path, max(1, round(size_px * 64)))


@dataclass(frozen=True)
class FontFace:
    id: int
    family: str
    style: str
    path: str

    @property
    def metrics(self) -> FontMetrics:
        return load_metrics(self.path)


class FontLibrary:
    """Thread-safe snapshot of the fonts table, used by the renderer (no DB access while rendering)."""

    def __init__(self, faces: list[FontFace]) -> None:
        self.faces = {f.id: f for f in faces}
        self.by_family: dict[tuple[str, str], FontFace] = {}
        for f in faces:
            self.by_family.setdefault((f.family.casefold(), f.style), f)

    @classmethod
    def from_db(cls, db: Session) -> FontLibrary:
        rows = db.scalars(select(Font)).all()
        return cls([FontFace(r.id, r.family, r.style, r.file_path) for r in rows if Path(r.file_path).exists()])

    def default(self) -> FontFace:
        face = self.by_family.get((DEFAULT_FAMILY.casefold(), "regular"))
        if face:
            return face
        if not self.faces:
            raise FontError("No fonts available")
        return self.faces[min(self.faces)]

    def get(self, font_id: int | None) -> FontFace | None:
        return self.faces.get(font_id) if font_id is not None else None

    def resolve(self, font_id: int | None, bold: bool = False, italic: bool = False) -> FontFace:
        """Pick the requested face, switching to the bold/italic variant of the same family when it exists."""
        base = self.get(font_id) or self.default()
        if not (bold or italic):
            return base
        # Combine requested flags with the base face's own style (selecting "Roboto Bold" + italic -> BoldItalic).
        want_bold = bold or "bold" in base.style
        want_italic = italic or "italic" in base.style
        style = ("bold" if want_bold else "") + ("italic" if want_italic else "") or "regular"
        return self.by_family.get((base.family.casefold(), style), base)


# --------------------------------------------------------------------------------------------------
# Registration / discovery
# --------------------------------------------------------------------------------------------------


def inspect_font_bytes(data: bytes) -> tuple[str, str, str]:
    """Validate font bytes; returns (family, style, ext)."""
    import io

    try:
        f = TTFont(io.BytesIO(data), lazy=True)
    except Exception as e:  # fontTools raises many exception types for garbage input
        raise FontError(f"Not a valid TrueType/OpenType font: {e}") from e
    if "hmtx" not in f or "cmap" not in f:
        raise FontError("Font has no cmap/hmtx tables")
    family, sub = read_names(f)
    ext = "otf" if f.sfntVersion == "OTTO" else "ttf"
    return family, style_from(f, sub), ext


def register_font_bytes(db: Session, data: bytes, filename: str, source: str = "uploaded") -> tuple[Font, bool]:
    """Store a font (deduplicated by SHA-256). Returns (font, created)."""
    sha = sha256_bytes(data)
    existing = db.scalar(select(Font).where(Font.sha256 == sha))
    if existing:
        return existing, False
    family, style, ext = inspect_font_bytes(data)
    _, path = store_bytes(get_settings().fonts_dir, data, ext)
    label = family if style == "regular" else f"{family} {style.replace('bolditalic', 'Bold Italic').title()}"
    font = Font(name=label, family=family, style=style, filename=filename, sha256=sha, file_path=str(path), source=source)
    db.add(font)
    db.flush()
    return font, True


def seed_bundled_fonts(db: Session) -> int:
    """Register the fonts shipped in ``backend/fonts`` (idempotent). Returns how many were added."""
    added = 0
    d = get_settings().bundled_fonts_dir
    if not d.exists():
        return 0
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in FONT_EXTS:
            _, created = register_font_bytes(db, p.read_bytes(), p.name, source="bundled")
            added += int(created)
    db.commit()
    return added


def system_font_dirs() -> list[Path]:
    system = platform.system()
    home = Path.home()
    if system == "Darwin":
        dirs = [Path("/System/Library/Fonts"), Path("/Library/Fonts"), home / "Library/Fonts"]
    elif system == "Windows":
        dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts", home / r"AppData\Local\Microsoft\Windows\Fonts"]
    else:
        dirs = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), home / ".local/share/fonts", home / ".fonts"]
    return [d for d in dirs if d.exists()]


@lru_cache(maxsize=1)
def _scan_system_fonts() -> tuple[tuple[str, str, str], ...]:
    found: list[tuple[str, str, str]] = []
    for d in system_font_dirs():
        for root, _dirs, files in os.walk(d):
            for name in files:
                p = Path(root) / name
                if p.suffix.lower() not in FONT_EXTS:
                    continue
                try:
                    f = TTFont(str(p), lazy=True)
                    family, sub = read_names(f)
                    found.append((family, style_from(f, sub), str(p)))
                    f.close()
                except Exception:
                    continue
    return tuple(sorted(found))


def list_system_fonts() -> list[dict[str, str]]:
    """Fonts installed on the server (TTC collections are skipped: browsers cannot address faces in them)."""
    return [{"family": fam, "style": style, "path": path} for fam, style, path in _scan_system_fonts()]


def import_system_font(db: Session, path: str) -> Font:
    """Copy a system font into the font store. Only paths returned by the system scan are accepted."""
    allowed = {p for _, _, p in _scan_system_fonts()}
    if path not in allowed:
        raise FontError("Unknown system font path")
    font, _ = register_font_bytes(db, Path(path).read_bytes(), Path(path).name, source="system")
    return font
