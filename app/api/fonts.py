"""Fonts: registered fonts, uploads, system fonts, font files and layout metrics."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import read_upload
from app.db.models import Font
from app.db.session import get_db
from app.schemas.api import FontMetricsOut, FontOut, SystemFont, SystemFontImport
from app.services import font_manager
from app.services.font_manager import FontError

router = APIRouter(prefix="/fonts", tags=["fonts"])


def _get(db: Session, font_id: int) -> Font:
    font = db.get(Font, font_id)
    if font is None:
        raise HTTPException(404, "Font not found")
    return font


@router.get("", response_model=list[FontOut])
def list_fonts(db: Session = Depends(get_db)) -> list[Font]:
    return list(db.scalars(select(Font).order_by(Font.family, Font.style)))


@router.post("", response_model=FontOut, status_code=201)
async def upload_font(file: UploadFile = File(...), db: Session = Depends(get_db)) -> Font:
    if not (file.filename or "").lower().endswith((".ttf", ".otf")):
        raise HTTPException(422, "Only .ttf and .otf fonts are supported")
    data = await read_upload(file)
    try:
        font, _ = font_manager.register_font_bytes(db, data, file.filename or "font.ttf")
        db.commit()
    except FontError as e:
        db.rollback()
        raise HTTPException(422, str(e)) from e
    return font


@router.get("/system", response_model=list[SystemFont])
def system_fonts() -> list[dict[str, str]]:
    return font_manager.list_system_fonts()


@router.post("/system/import", response_model=FontOut, status_code=201)
def import_system_font(body: SystemFontImport, db: Session = Depends(get_db)) -> Font:
    try:
        font = font_manager.import_system_font(db, body.path)
        db.commit()
    except FontError as e:
        db.rollback()
        raise HTTPException(422, str(e)) from e
    return font


@router.get("/{font_id}/file")
def font_file(font_id: int, db: Session = Depends(get_db)) -> FileResponse:
    font = _get(db, font_id)
    media = "font/otf" if font.file_path.endswith(".otf") else "font/ttf"
    return FileResponse(font.file_path, media_type=media, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.get("/{font_id}/metrics", response_model=FontMetricsOut)
def font_metrics(font_id: int, db: Session = Depends(get_db)) -> dict:
    """Advance widths and vertical metrics used by the shared text layout (browser == server)."""
    return font_manager.load_metrics(_get(db, font_id).file_path).to_json()
