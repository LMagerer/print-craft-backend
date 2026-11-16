"""Batch card jobs, PDF jobs, progress streaming and downloads."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.api.deps import image_path, load_dataset
from app.db.models import Job
from app.db.repository import JobRepository, TemplateRepository, transaction
from app.db.session import SessionLocal, get_db
from app.schemas.api import JobCreate, JobDetail, JobError, JobImage, JobOut, LayoutRequest, LayoutResponse, PdfJobCreate
from app.schemas.config import TemplateConfig
from app.services import job_runner
from app.services.pdf_layout import compute_layout
from app.services.tokens import sanitize_filename

router = APIRouter(tags=["jobs"])


def _out(job: Job, detail: bool = False) -> JobOut | JobDetail:
    errors = job.error_log_json or []
    data = dict(
        id=job.id, kind=job.kind, template_id=job.template_id, data_filename=job.data_filename,
        row_count=job.row_count, status=job.status, progress=job.progress, processed=job.processed,
        created_at=job.created_at, finished_at=job.finished_at,
        has_output=bool(job.output_path and Path(job.output_path).exists()),
        error_count=sum(1 for e in errors if not e.get("skipped") and not e.get("warning")),
        warning_count=sum(1 for e in errors if e.get("warning")),
        skipped_count=sum(1 for e in errors if e.get("skipped")),
    )
    if not detail:
        return JobOut(**data)
    params = {k: v for k, v in job.params_json.items() if k not in ("config", "image_path")}
    return JobDetail(**data, errors=[JobError.model_validate(e) for e in errors], params=params)


def _get(db: Session, job_id: int) -> Job:
    job = JobRepository(db).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@router.post("/jobs", response_model=JobOut, status_code=202)
def create_job(body: JobCreate, background: BackgroundTasks, db: Session = Depends(get_db)) -> JobOut:
    template = TemplateRepository(db).get(body.template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    meta, records = load_dataset(body.dataset_id)
    config = body.config or TemplateConfig.model_validate(template.config_json)
    rows = body.rows
    if rows is not None:
        bad = [r for r in rows if r < 0 or r >= len(records)]
        if bad:
            raise HTTPException(422, f"Rows out of range: {bad[:10]}")
    image = image_path(template.image_sha256)
    with transaction(db):
        job = JobRepository(db).add(
            kind="cards",
            template_id=template.id,
            data_filename=meta.get("filename", ""),
            row_count=len(rows) if rows is not None else len(records),
            status="queued",
            params_json={
                "template_name": template.name,
                "template_version": template.version,
                "dataset_id": body.dataset_id,
                "filename_field": body.filename_field,
                "rows": rows,
                "empty_policy": body.empty_policy,
                "fill_value": body.fill_value,
                "config": config.model_dump(mode="json"),
                "image_path": str(image),
                "bleed_mm": config.size.bleed_mm,
                "width_mm": config.size.width_mm,
                "height_mm": config.size.height_mm,
            },
        )
        job_runner.prepare_card_job(job, records)
    background.add_task(job_runner.run_card_job, job.id)
    return _out(job)


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(kind: str | None = None, db: Session = Depends(get_db)) -> list[JobOut]:
    return [_out(j) for j in JobRepository(db).list(kind)]


@router.get("/jobs/{job_id}", response_model=JobDetail)
def get_job(job_id: int, db: Session = Depends(get_db)) -> JobDetail:
    return _out(_get(db, job_id), detail=True)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: int, db: Session = Depends(get_db)) -> JobOut:
    job = _get(db, job_id)
    if job.status in job_runner.TERMINAL:
        return _out(job)
    job_runner.request_cancel(job_id)
    if job.status == "queued":
        job.status = "cancelled"
    else:
        job.status = "cancelling"
    db.commit()
    return _out(job)


@router.post("/jobs/{job_id}/retry", response_model=JobOut, status_code=202)
def retry_job(job_id: int, background: BackgroundTasks, db: Session = Depends(get_db)) -> JobOut:
    """Re-render only the rows that failed, in place (output ZIP is rebuilt)."""
    job = _get(db, job_id)
    if job.kind != "cards":
        raise HTTPException(422, "Only card jobs can be retried")
    if job.status not in job_runner.TERMINAL:
        raise HTTPException(409, "Job is still running")
    if not any(not e.get("skipped") and not e.get("warning") and e.get("row", -1) >= 0 for e in job.error_log_json or []):
        raise HTTPException(422, "There are no failed rows to retry")
    job.status = "queued"
    db.commit()
    background.add_task(job_runner.run_card_job, job.id, True)
    return _out(job)


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: int, db: Session = Depends(get_db)) -> Response:
    job = _get(db, job_id)
    if job.status not in job_runner.TERMINAL:
        raise HTTPException(409, "Cancel the job before deleting it")
    db.delete(job)
    db.commit()
    job_runner.delete_job_files(job_id)
    return Response(status_code=204)


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: int, request: Request) -> EventSourceResponse:
    """Server-Sent Events: a ``progress`` event whenever the job changes, ``done`` once it is finished."""
    with SessionLocal() as db:
        _get(db, job_id)

    async def stream():
        last = None
        while True:
            if await request.is_disconnected():
                return
            with SessionLocal() as db:
                job = db.get(Job, job_id)
                if job is None:
                    yield {"event": "error", "data": json.dumps({"detail": "Job deleted"})}
                    return
                payload = _out(job).model_dump_json()
                terminal = job.status in job_runner.TERMINAL
            if payload != last:
                last = payload
                yield {"event": "done" if terminal else "progress", "data": payload}
            if terminal:
                return
            await asyncio.sleep(0.4)

    return EventSourceResponse(stream(), ping=15)


def _download_name(job: Job, ext: str) -> str:
    base = sanitize_filename(str(job.params_json.get("template_name", ""))) or "cards"
    return f"{base}_job{job.id}.{ext}"


@router.get("/jobs/{job_id}/download")
def download(job_id: int, db: Session = Depends(get_db)) -> FileResponse:
    job = _get(db, job_id)
    if not job.output_path or not Path(job.output_path).exists():
        raise HTTPException(404, "This job has no output yet")
    ext = "pdf" if job.kind == "pdf" else "zip"
    name = _download_name(job, ext)
    return FileResponse(
        job.output_path,
        media_type="application/pdf" if ext == "pdf" else "application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


@router.get("/jobs/{job_id}/images", response_model=list[JobImage])
def list_images(job_id: int, db: Session = Depends(get_db)) -> list[JobImage]:
    job = _get(db, job_id)
    return [JobImage(row=int(r), filename=n) for r, n in sorted(job_runner._read_json(job_runner.job_dir(job.id) / "manifest.json", {}).items(), key=lambda kv: int(kv[0]))]


@router.get("/jobs/{job_id}/images/{filename}")
def get_image(job_id: int, filename: str, db: Session = Depends(get_db)) -> FileResponse:
    _get(db, job_id)
    images = (job_runner.job_dir(job_id) / "images").resolve()
    path = (images / filename).resolve()
    if path.parent != images or not path.is_file():
        raise HTTPException(404, "Image not found")
    return FileResponse(path, media_type="image/png")


# ------------------------------------------------------------------------------------------------- PDF


@router.post("/pdf/layout", response_model=LayoutResponse, tags=["pdf"])
def pdf_layout(body: LayoutRequest) -> LayoutResponse:
    return compute_layout(body)


@router.post("/pdf/jobs", response_model=JobOut, status_code=202, tags=["pdf"])
def create_pdf_job(body: PdfJobCreate, background: BackgroundTasks, db: Session = Depends(get_db)) -> JobOut:
    source = _get(db, body.source_job_id)
    if source.kind != "cards":
        raise HTTPException(422, "The source must be a card job")
    if source.status not in ("completed", "completed_with_errors", "cancelled"):
        raise HTTPException(409, "The card job has not finished yet")
    count = len(job_runner.card_images(source))
    if count == 0:
        raise HTTPException(422, "The card job has no rendered cards")
    layout = compute_layout(body.layout.model_copy(update={"card_count": count}))
    if layout.per_page == 0:
        raise HTTPException(422, "The card does not fit on the page with these margins")
    with transaction(db):
        job = JobRepository(db).add(
            kind="pdf",
            template_id=source.template_id,
            data_filename=source.data_filename,
            row_count=count,
            status="queued",
            params_json={
                "source_job_id": source.id,
                "template_name": source.params_json.get("template_name", ""),
                "layout": body.layout.model_dump(mode="json"),
                "crop_marks": body.crop_marks,
                "bleed_mm": body.bleed_mm,
                "dpi": body.dpi,
                "image_format": body.image_format,
                "jpeg_quality": body.jpeg_quality,
            },
        )
    background.add_task(job_runner.run_pdf_job, job.id)
    return _out(job)
