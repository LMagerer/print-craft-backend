"""Image upload, download and the size editor transform."""

from __future__ import annotations

import io

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from PIL import Image

from app.api.deps import image_path, read_upload
from app.core.config import get_settings
from app.core.units import summary
from app.schemas.image import ImageInfo, TransformRequest, TransformResponse
from app.services import image_tools
from app.services.storage import store_bytes

router = APIRouter(prefix="/images", tags=["images"])

@router.post("", response_model=ImageInfo, status_code=201)
async def upload_image(file: UploadFile = File(...)) -> ImageInfo:
    try:
        return image_tools.store_image(await read_upload(file))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/{ref}")
def get_image(ref: str) -> FileResponse:
    path = image_path(ref)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.get("/{ref}/info", response_model=ImageInfo)
def get_image_info(ref: str) -> ImageInfo:
    return image_tools.image_info(image_tools.open_image(image_path(ref)), ref)


@router.get("/{ref}/thumbnail")
def get_thumbnail(ref: str, size: int = 320) -> Response:
    size = max(32, min(1024, size))
    cache = get_settings().cache_dir / f"{ref}_{size}.png"
    if not cache.exists():
        cache.write_bytes(image_tools.thumbnail_png(image_path(ref), size))
    return FileResponse(cache, media_type="image/png", headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.post("/transform", response_model=TransformResponse)
def transform(req: TransformRequest) -> TransformResponse:
    """Apply size-editor operations to a copy of ``source_ref`` and store the result as a new lossless PNG."""
    src = image_tools.open_image(image_path(req.source_ref))
    try:
        result = image_tools.transform_image(src, req)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    data = image_tools.png_bytes(result.image, result.dpi, result.icc)
    sha, _ = store_bytes(get_settings().templates_dir, data, "png")
    info = image_tools.image_info(Image.open(io.BytesIO(data)), sha)
    config = None
    if req.config is not None:
        if req.scale_placeholders:
            config = image_tools.remap_config(req.config, src.size, result)
        else:
            size = req.config.size.model_copy(
                update={"width_mm": round(result.width_mm, 4), "height_mm": round(result.height_mm, 4),
                        "dpi": round(result.dpi, 4), "bleed_mm": result.bleed_mm, "mode": req.mode}
            )
            config = req.config.model_copy(update={"size": size})
        config.size.mode = req.mode
    return TransformResponse(
        image=info,
        width_mm=result.width_mm,
        height_mm=result.height_mm,
        dpi=result.dpi,
        bleed_mm=result.bleed_mm,
        summary=summary(result.width_mm, result.height_mm, result.image.width, result.image.height),
        warnings=result.warnings,
        config=config,
    )
