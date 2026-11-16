"""Data sources: upload/paste, paginated records and validation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import load_dataset, read_upload
from app.db.models import Template
from app.db.session import get_db
from app.schemas.api import DatasetOut, RecordsPage, ValidateRequest, ValidateResponse
from app.services.data_loader import DataError, get_store, parse_json, parse_upload, validate_records

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.get("", response_model=list[DatasetOut])
def list_datasets(db: Session = Depends(get_db)) -> list[DatasetOut]:
    """All uploaded datasets, newest first, with the templates linked to each."""
    used: dict[str, list[int]] = {}
    for tid, ds in db.execute(select(Template.id, Template.dataset_id).where(Template.dataset_id.is_not(None))):
        used.setdefault(ds, []).append(tid)
    return [DatasetOut(**meta, template_ids=used.get(meta["id"], [])) for meta in get_store().list()]


@router.post("", response_model=DatasetOut, status_code=201)
async def create_dataset(
    file: UploadFile | None = File(None),
    text: str | None = Form(None, description="Pasted JSON (alternative to a file upload)"),
    sheet: str | None = Form(None, description="Excel sheet name (default: first sheet)"),
) -> DatasetOut:
    try:
        if file is not None and file.filename:
            data = await read_upload(file)
            parsed = parse_upload(file.filename, data, sheet)
            filename = file.filename
        elif text and text.strip():
            parsed = parse_json(text)
            filename = "pasted.json"
        else:
            raise HTTPException(400, "Upload a file or paste JSON")
    except DataError as e:
        raise HTTPException(422, str(e)) from e
    meta = get_store().save(parsed, filename)
    return DatasetOut(**meta)


@router.get("/{dataset_id}", response_model=DatasetOut)
def get_dataset(dataset_id: str) -> DatasetOut:
    meta, _ = load_dataset(dataset_id)
    return DatasetOut(**meta)


@router.get("/{dataset_id}/records", response_model=RecordsPage)
def get_records(dataset_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=1000)) -> RecordsPage:
    meta, records = load_dataset(dataset_id)
    return RecordsPage(offset=offset, limit=limit, total=len(records), fields=meta["fields"], records=records[offset : offset + limit])


@router.post("/{dataset_id}/validate", response_model=ValidateResponse)
def validate_dataset(dataset_id: str, body: ValidateRequest) -> ValidateResponse:
    meta, records = load_dataset(dataset_id)
    return ValidateResponse(**validate_records(records, meta["fields"], body.required_fields, body.filename_field))


@router.delete("/{dataset_id}", status_code=204)
def delete_dataset(dataset_id: str, db: Session = Depends(get_db)) -> Response:
    if not get_store().delete(dataset_id):
        raise HTTPException(404, "Dataset not found")
    db.execute(update(Template).where(Template.dataset_id == dataset_id).values(dataset_id=None))
    db.commit()
    return Response(status_code=204)
