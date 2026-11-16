"""Shared API dependencies and helpers."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, UploadFile

from app.core.config import get_settings
from app.services.data_loader import Record, get_store
from app.services.storage import find_by_sha


def image_path(ref: str) -> Path:
    path = find_by_sha(get_settings().templates_dir, ref)
    if path is None:
        raise HTTPException(404, f"Image {ref} not found")
    return path


def load_dataset(dataset_id: str) -> tuple[dict, list[Record]]:
    try:
        return get_store().load(dataset_id)
    except KeyError:
        raise HTTPException(404, "Dataset not found. It may have been deleted; please upload the data again.") from None


async def read_upload(file: UploadFile) -> bytes:
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File larger than {get_settings().max_upload_mb} MB")
    if not data:
        raise HTTPException(400, "Empty file")
    return data
