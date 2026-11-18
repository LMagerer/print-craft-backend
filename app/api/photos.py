"""Photo library for data-bound image layers (a column value picks the photo by file name)."""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Response, UploadFile
from pydantic import BaseModel

from app.api.deps import read_upload
from app.services import photo_library

router = APIRouter(prefix="/photos", tags=["photos"])


class PhotoOut(BaseModel):
    name: str
    ref: str


class PhotoUploadResult(BaseModel):
    added: list[PhotoOut]
    errors: list[str]


@router.get("", response_model=list[PhotoOut])
def list_photos() -> list[PhotoOut]:
    return [PhotoOut(name=n, ref=r) for n, r in photo_library.load_index().items()]


@router.post("", response_model=PhotoUploadResult, status_code=201)
async def upload_photos(files: list[UploadFile] = File(...)) -> PhotoUploadResult:
    """Upload photos (images, PDFs or ZIPs of them). A photo with the same file name is replaced."""
    payload = [(f.filename or "photo", await read_upload(f)) for f in files]
    added, errors = photo_library.add_files(payload)
    if not added and errors:
        raise HTTPException(422, "; ".join(errors[:5]))
    return PhotoUploadResult(added=[PhotoOut(name=n, ref=r) for n, r in added.items()], errors=errors)


@router.delete("/{name}", status_code=204)
def delete_photo(name: str) -> Response:
    if not photo_library.remove(name):
        raise HTTPException(404, "Photo not found")
    return Response(status_code=204)
