"""FastAPI application entry point."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import datasets, fonts, images, jobs, render, templates
from app.core.config import BACKEND_DIR, get_settings
from app.db.session import SessionLocal
from app.services.data_loader import get_store
from app.services.font_manager import seed_bundled_fonts
from app.services.job_runner import mark_interrupted_jobs

log = logging.getLogger("printcraft")
API_PREFIX = "/api/v1"


def run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, "head")


def startup() -> None:
    settings = get_settings()
    settings.ensure_dirs()
    if settings.auto_migrate:
        run_migrations()
    with SessionLocal() as db:
        added = seed_bundled_fonts(db)
        if added:
            log.info("Registered %d bundled fonts", added)
        mark_interrupted_jobs(db)
    get_store().cleanup()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    startup()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="PrintCraft API", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Render-Report", "Content-Disposition"],
    )

    api = APIRouter(prefix=API_PREFIX)
    for module in (templates, datasets, images, fonts, render, jobs):
        api.include_router(module.router)

    @api.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(api)

    dist: Path = settings.frontend_dist
    if dist.is_dir() and (dist / "index.html").exists():
        if (dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise HTTPException(404, "Not found")
            candidate = (dist / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app


app = create_app()
