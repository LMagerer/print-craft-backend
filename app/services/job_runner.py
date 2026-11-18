"""Batch job execution: card rendering and PDF assembly.

Jobs run in a worker thread (FastAPI ``BackgroundTasks``). Each job owns ``data/jobs/<id>/``::

    records.json     snapshot of the dataset rows (retries keep working after the dataset expires)
    manifest.json    {row_index: filename} of successfully rendered cards
    images/          one PNG per card
    cards.zip        all PNGs (stored, not recompressed)   -- card jobs
    cards.pdf        merged layout                          -- PDF jobs

A failing row never stops the batch; it is logged in ``error_log_json`` and can be retried.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Job, utcnow
from app.db.session import SessionLocal
from app.schemas.api import LayoutRequest
from app.schemas.config import TemplateConfig
from app.services import image_tools
from app.services.card_renderer import CardRenderer
from app.services.photo_library import PhotoResolver
from app.services.font_manager import FontLibrary
from app.services.pdf_layout import compute_layout, render_pdf
from app.services.tokens import FilenameDeduper, binding_fields, render_template, sanitize_filename, value_to_str

log = logging.getLogger(__name__)

TERMINAL = {"completed", "completed_with_errors", "failed", "cancelled"}
PROGRESS_INTERVAL_S = 0.25

_cancel_flags: set[int] = set()
_lock = threading.Lock()


def job_dir(job_id: int) -> Path:
    return get_settings().jobs_dir / str(job_id)


def request_cancel(job_id: int) -> None:
    with _lock:
        _cancel_flags.add(job_id)


def _cancelled(job_id: int) -> bool:
    with _lock:
        return job_id in _cancel_flags


def _clear_cancel(job_id: int) -> None:
    with _lock:
        _cancel_flags.discard(job_id)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    tmp.replace(path)


def prepare_card_job(job: Job, records: list[dict[str, str]]) -> None:
    d = job_dir(job.id)
    (d / "images").mkdir(parents=True, exist_ok=True)
    _write_json(d / "records.json", records)


def delete_job_files(job_id: int) -> None:
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


class _Progress:
    """Throttled progress writes so SQLite isn't hammered on every row."""

    def __init__(self, db: Session, job: Job, total: int) -> None:
        self.db, self.job, self.total = db, job, max(total, 1)
        self.last = 0.0

    def update(self, processed: int, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last < PROGRESS_INTERVAL_S:
            return
        self.last = now
        self.job.processed = processed
        self.job.progress = round(min(1.0, processed / self.total), 4)
        self.db.commit()

    def status_cancelled(self) -> bool:
        if _cancelled(self.job.id):
            return True
        self.db.refresh(self.job, ["status"])
        return self.job.status == "cancelling"


def _used_fields(config: TemplateConfig) -> list[str]:
    out: list[str] = []
    for p in config.placeholders:
        if p.hidden:
            continue
        for f in binding_fields(p.binding):
            if f not in out:
                out.append(f)
    return out


def _filename_stem(record: Mapping[str, str], field: str | None, row: int) -> str:
    if field:
        raw = render_template(field, record) if "{{" in field else value_to_str(record.get(field, ""))
        stem = sanitize_filename(raw)
        if stem:
            return stem
    return f"card_{row + 1:05d}"


def _build_zip(d: Path, manifest: dict[str, str]) -> Path:
    out = d / "cards.zip"
    tmp = d / "cards.zip.tmp"
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as zf:
        for row in sorted(manifest, key=int):
            zf.write(d / "images" / manifest[row], arcname=manifest[row])
    tmp.replace(out)
    return out


def run_card_job(job_id: int, retry: bool = False) -> None:
    """Render cards for a job. With ``retry`` only previously failed (non-skipped) rows are rendered again."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return
        params = job.params_json
        d = job_dir(job_id)
        records: list[dict[str, str]] = _read_json(d / "records.json", [])
        manifest: dict[str, str] = _read_json(d / "manifest.json", {})
        config = TemplateConfig.model_validate(params["config"])
        rows: list[int] = params.get("rows") or list(range(len(records)))
        errors: list[dict[str, Any]] = list(job.error_log_json or [])
        if retry:
            failed = {e["row"] for e in errors if not e.get("skipped")}
            errors = [e for e in errors if e["row"] not in failed]
            todo = [r for r in rows if r in failed]
            done_before = len(rows) - len(todo)
        else:
            todo, done_before = rows, 0

        job.status = "running"
        job.finished_at = None
        job.error_log_json = list(errors)
        db.commit()

        fonts = FontLibrary.from_db(db)
        base = image_tools.open_image(Path(params["image_path"]))
        renderer = CardRenderer(base, config, fonts, PhotoResolver())
        used = _used_fields(config)
        filename_field = params.get("filename_field") or config.output.filename_field
        policy = params.get("empty_policy", "render")
        fill_value = params.get("fill_value", "")
        deduper = FilenameDeduper()
        for name in manifest.values():
            deduper.claim(Path(name).stem, "png")

        progress = _Progress(db, job, len(rows))
        progress.update(done_before, force=True)
        cancelled = False
        for i, row in enumerate(todo):
            if i % 10 == 0 and progress.status_cancelled():
                cancelled = True
                break
            try:
                if row < 0 or row >= len(records):
                    raise IndexError(f"Row {row + 1} does not exist")
                record = dict(records[row])
                empty = [f for f in used if not value_to_str(record.get(f, "")).strip()]
                if empty and policy == "skip":
                    errors.append({"row": row, "message": f"Skipped: empty {', '.join(empty)}", "skipped": True})
                    continue
                if empty and policy == "fill":
                    for f in empty:
                        record[f] = fill_value
                card = renderer.render(record)
                name = manifest.get(str(row)) or deduper.claim(_filename_stem(record, filename_field, row), "png")
                (d / "images" / name).write_bytes(card.to_png())
                manifest[str(row)] = name
                overflow = [r.id for r in card.reports if r.overflow]
                missing = sorted({g for r in card.reports for g in r.missing_glyphs})
                if overflow or missing or card.warnings:
                    msg = list(card.warnings)
                    if overflow:
                        msg.append(f"text overflows in {', '.join(overflow)}")
                    if missing:
                        msg.append(f"missing glyphs {''.join(missing)!r}")
                    errors.append({"row": row, "message": "Warning: " + "; ".join(msg), "skipped": False, "warning": True})
            except Exception as e:  # noqa: BLE001 - one bad row must not stop the batch
                log.exception("Job %s row %s failed", job_id, row)
                errors.append({"row": row, "message": f"{type(e).__name__}: {e}", "skipped": False})
            finally:
                progress.update(done_before + i + 1)

        _write_json(d / "manifest.json", manifest)
        job.error_log_json = list(errors)
        if manifest:
            job.output_path = str(_build_zip(d, manifest))
        hard_errors = [e for e in errors if not e.get("skipped") and not e.get("warning")]
        if cancelled:
            job.status = "cancelled"
        else:
            progress.update(len(rows), force=True)
            job.status = "completed_with_errors" if hard_errors else "completed"
        job.finished_at = utcnow()
        db.commit()
    except Exception as e:  # noqa: BLE001
        log.exception("Job %s failed", job_id)
        db.rollback()
        job = db.get(Job, job_id)
        if job is not None:
            job.status = "failed"
            job.error_log_json = list(job.error_log_json or []) + [{"row": -1, "message": f"{type(e).__name__}: {e}"}]
            job.finished_at = utcnow()
            db.commit()
    finally:
        _clear_cancel(job_id)
        db.close()


def card_images(source: Job) -> list[Path]:
    d = job_dir(source.id)
    manifest: dict[str, str] = _read_json(d / "manifest.json", {})
    return [d / "images" / manifest[k] for k in sorted(manifest, key=int) if (d / "images" / manifest[k]).exists()]


def run_pdf_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return
        params = job.params_json
        source = db.get(Job, params["source_job_id"])
        if source is None:
            raise ValueError("Source card job no longer exists")
        images = card_images(source)
        if not images:
            raise ValueError("The source job has no rendered cards")
        req = LayoutRequest.model_validate(params["layout"]).model_copy(update={"card_count": len(images)})
        layout = compute_layout(req)
        job.status = "running"
        job.row_count = len(images)
        db.commit()
        d = job_dir(job_id)
        d.mkdir(parents=True, exist_ok=True)
        out = d / "cards.pdf"
        progress = _Progress(db, job, len(images))
        ok = render_pdf(
            images, layout, out,
            crop_marks=params.get("crop_marks", True),
            bleed_mm=params.get("bleed_mm", 0.0),
            # Jobs created before these options existed embedded the original PNGs.
            dpi=params.get("dpi"),
            image_format=params.get("image_format", "png"),
            jpeg_quality=params.get("jpeg_quality", 90),
            on_progress=progress.update,
            should_cancel=lambda: _cancelled(job_id),
        )
        if ok:
            progress.update(len(images), force=True)
            job.output_path = str(out)
            job.status = "completed"
        else:
            out.unlink(missing_ok=True)
            job.status = "cancelled"
        job.finished_at = utcnow()
        db.commit()
    except Exception as e:  # noqa: BLE001
        log.exception("PDF job %s failed", job_id)
        db.rollback()
        job = db.get(Job, job_id)
        if job is not None:
            job.status = "failed"
            job.error_log_json = [{"row": -1, "message": f"{type(e).__name__}: {e}"}]
            job.finished_at = utcnow()
            db.commit()
    finally:
        _clear_cancel(job_id)
        db.close()


def mark_interrupted_jobs(db: Session) -> int:
    """On startup, jobs left running by a previous process can never finish; mark them failed."""
    n = 0
    for job in db.query(Job).filter(Job.status.in_(["queued", "running", "cancelling"])):
        job.status = "failed"
        job.error_log_json = list(job.error_log_json or []) + [{"row": -1, "message": "Interrupted by server restart"}]
        job.finished_at = utcnow()
        n += 1
    db.commit()
    return n
