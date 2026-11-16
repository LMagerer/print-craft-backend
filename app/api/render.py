"""Exact server-side preview and glyph coverage checks."""

from __future__ import annotations

import io
import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from PIL import Image
from sqlalchemy.orm import Session

from app.api.deps import image_path, load_dataset
from app.db.session import get_db
from app.schemas.api import GlyphCheckItem, GlyphCheckRequest, GlyphCheckResponse, PreviewRequest
from app.services import image_tools
from app.services.card_renderer import CardRenderer, glyph_check
from app.services.font_manager import FontLibrary

router = APIRouter(prefix="/render", tags=["render"])


@router.post("/preview", response_class=Response, responses={200: {"content": {"image/png": {}}}})
def preview(req: PreviewRequest, db: Session = Depends(get_db)) -> Response:
    """Render one card at full resolution with the production renderer and return it as PNG.

    The ``X-Render-Report`` header carries per-placeholder info (final font size, overflow, missing glyphs).
    """
    record = req.record
    if record is None and req.dataset_id:
        _, records = load_dataset(req.dataset_id)
        idx = req.record_index or 0
        if idx >= len(records):
            raise HTTPException(422, f"Record {idx} out of range ({len(records)} rows)")
        record = records[idx]
    base = image_tools.open_image(image_path(req.image_ref))
    card = CardRenderer(base, req.config, FontLibrary.from_db(db)).render(record)
    img = card.image
    if req.max_width and img.width > req.max_width:
        img = img.copy()
        img.thumbnail((req.max_width, req.max_width * img.height // img.width + 1), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
    else:
        data = card.to_png()
    report = [r.__dict__ for r in card.reports]
    return Response(
        data,
        media_type="image/png",
        headers={"X-Render-Report": quote(json.dumps(report, ensure_ascii=False)), "Cache-Control": "no-store"},
    )


@router.post("/glyph-check", response_model=GlyphCheckResponse)
def glyph_check_endpoint(req: GlyphCheckRequest, db: Session = Depends(get_db)) -> GlyphCheckResponse:
    """Scan every record and return, per placeholder, characters that neither its font nor its fallback can draw."""
    if req.records is not None:
        records = req.records
    elif req.dataset_id:
        _, records = load_dataset(req.dataset_id)
    else:
        records = [{}]
    result = glyph_check(req.config, FontLibrary.from_db(db), records)
    return GlyphCheckResponse(
        checked_rows=len(records),
        placeholders=[GlyphCheckItem(placeholder_id=pid, missing=v["missing"], rows=v["rows"]) for pid, v in result.items()],
    )
