"""Template library endpoints."""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import read_upload
from app.db.models import Template
from app.db.repository import TemplateRepository
from app.db.session import get_db
from app.schemas.api import DuplicateRequest, TemplateCreate, TemplateOut, TemplateSummary, TemplateUpdate, TemplateVersionOut
from app.schemas.config import TemplateConfig
from app.services import template_service as svc
from app.services.tokens import sanitize_filename

router = APIRouter(prefix="/templates", tags=["templates"])


def _get(db: Session, template_id: int) -> Template:
    t = TemplateRepository(db).get(template_id)
    if t is None:
        raise HTTPException(404, "Template not found")
    return t


def _out(t: Template) -> TemplateOut:
    data = TemplateSummary.model_validate(t).model_dump()
    return TemplateOut(
        **data,
        original_image_sha256=t.original_image_sha256,
        icc_profile=t.icc_profile,
        config=TemplateConfig.model_validate(t.config_json),
    )


def _run(fn, *args):
    try:
        return fn(*args)
    except svc.ServiceError as e:
        raise HTTPException(e.status, str(e)) from e


@router.get("", response_model=list[TemplateSummary])
def list_templates(
    search: str | None = None,
    sort: Literal["name", "created_at", "updated_at"] = "updated_at",
    order: Literal["asc", "desc"] = "desc",
    archived: Literal["exclude", "include", "only"] = "exclude",
    db: Session = Depends(get_db),
) -> list[Template]:
    return TemplateRepository(db).list(
        search, sort, order == "desc", include_archived=archived == "include", archived_only=archived == "only"
    )


@router.post("", response_model=TemplateOut, status_code=201)
def create_template(body: TemplateCreate, db: Session = Depends(get_db)) -> TemplateOut:
    return _out(_run(svc.create_template, db, body))


@router.post("/import", response_model=TemplateOut, status_code=201)
async def import_template(file: UploadFile = File(...), name: str | None = Form(None), db: Session = Depends(get_db)) -> TemplateOut:
    return _out(_run(svc.import_zip, db, await read_upload(file), name))


@router.get("/{template_id}", response_model=TemplateOut)
def get_template(template_id: int, db: Session = Depends(get_db)) -> TemplateOut:
    return _out(_get(db, template_id))


@router.put("/{template_id}", response_model=TemplateOut)
def update_template(template_id: int, body: TemplateUpdate, db: Session = Depends(get_db)) -> TemplateOut:
    return _out(_run(svc.update_template, db, _get(db, template_id), body))


@router.delete("/{template_id}", status_code=204)
def delete_template(template_id: int, db: Session = Depends(get_db)) -> Response:
    _run(svc.delete_template, db, _get(db, template_id))
    return Response(status_code=204)


@router.post("/{template_id}/duplicate", response_model=TemplateOut, status_code=201)
def duplicate_template(template_id: int, body: DuplicateRequest | None = None, db: Session = Depends(get_db)) -> TemplateOut:
    return _out(_run(svc.duplicate_template, db, _get(db, template_id), body.name if body else None))


@router.get("/{template_id}/versions", response_model=list[TemplateVersionOut])
def list_versions(template_id: int, db: Session = Depends(get_db)):
    return TemplateRepository(db).versions(_get(db, template_id))


@router.post("/{template_id}/restore/{version}", response_model=TemplateOut)
def restore_version(template_id: int, version: int, db: Session = Depends(get_db)) -> TemplateOut:
    return _out(_run(svc.restore_version, db, _get(db, template_id), version))


@router.get("/{template_id}/image")
def template_image(template_id: int, db: Session = Depends(get_db)) -> FileResponse:
    return FileResponse(_get(db, template_id).image_path)


@router.get("/{template_id}/export")
def export_template(template_id: int, db: Session = Depends(get_db)) -> Response:
    t = _get(db, template_id)
    data = svc.export_zip(db, t)
    name = sanitize_filename(t.name) or f"template_{t.id}"
    return Response(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.zip"},
    )
