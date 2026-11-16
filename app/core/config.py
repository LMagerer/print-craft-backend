"""Application settings, loaded from environment variables (prefix ``TC_``) or a ``.env`` file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TC_", env_file=".env", extra="ignore")

    data_dir: Path = BACKEND_DIR / "data"
    # Any SQLAlchemy URL (e.g. postgresql+psycopg://user:pw@host/db); defaults to SQLite at <data_dir>/app.db.
    database_url: str = ""
    bundled_fonts_dir: Path = BACKEND_DIR / "fonts"
    # Built frontend (served by FastAPI in production). Ignored when the directory does not exist.
    frontend_dist: Path = BACKEND_DIR.parent / "frontend" / "dist"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    # Uploaded datasets are kept until deleted; set > 0 to expire them after this many hours.
    dataset_ttl_hours: float = 0
    max_upload_mb: int = 100
    # Run Alembic migrations on startup (disable when migrations are managed externally).
    auto_migrate: bool = True

    @model_validator(mode="after")
    def _default_database_url(self) -> Settings:
        if not self.database_url:
            self.database_url = f"sqlite:///{self.data_dir / 'app.db'}"
        return self

    @property
    def templates_dir(self) -> Path:
        return self.data_dir / "templates"

    @property
    def fonts_dir(self) -> Path:
        return self.data_dir / "fonts"

    @property
    def datasets_dir(self) -> Path:
        return self.data_dir / "datasets"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.templates_dir, self.fonts_dir, self.datasets_dir, self.jobs_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite:///"):
            Path(self.database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
