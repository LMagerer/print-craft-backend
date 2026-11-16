"""SQLAlchemy ORM models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Dialect, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes, also on SQLite (which drops tzinfo on storage)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    pass


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    # Current (possibly edited) image, stored content-addressed on disk.
    image_path: Mapped[str] = mapped_column(String(500))
    image_sha256: Mapped[str] = mapped_column(String(64), index=True)
    # Untouched upload, kept so the size editor can always "Reset".
    original_image_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    width_px: Mapped[int] = mapped_column(Integer)
    height_px: Mapped[int] = mapped_column(Integer)
    dpi: Mapped[float] = mapped_column(Float)
    width_mm: Mapped[float] = mapped_column(Float)
    height_mm: Mapped[float] = mapped_column(Float)
    icc_profile: Mapped[str | None] = mapped_column(String(200), nullable=True)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Data source last chosen for this template (a dataset id; datasets live on disk, so no FK).
    dataset_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    versions: Mapped[list[TemplateVersion]] = relationship(
        back_populates="template", cascade="all, delete-orphan", order_by="TemplateVersion.version"
    )


class TemplateVersion(Base):
    __tablename__ = "template_versions"
    __table_args__ = (UniqueConstraint("template_id", "version", name="uq_template_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    # SHA-256 of the image used by this version.
    image_ref: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    template: Mapped[Template] = relationship(back_populates="versions")


class Font(Base):
    __tablename__ = "fonts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Display name, e.g. "Roboto Bold".
    name: Mapped[str] = mapped_column(String(200))
    family: Mapped[str] = mapped_column(String(200), default="", index=True)
    # One of: regular, bold, italic, bolditalic.
    style: Mapped[str] = mapped_column(String(20), default="regular")
    filename: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    file_path: Mapped[str] = mapped_column(String(500))
    # bundled | system | uploaded
    source: Mapped[str] = mapped_column(String(20), default="uploaded")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # cards (batch PNG render) | pdf (A4 merge)
    kind: Mapped[str] = mapped_column(String(20), default="cards")
    template_id: Mapped[int | None] = mapped_column(ForeignKey("templates.id", ondelete="SET NULL"), nullable=True)
    data_filename: Mapped[str] = mapped_column(String(300), default="")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    # queued | running | completed | completed_with_errors | failed | cancelled
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    # 0.0 - 1.0
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    error_log_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # Job parameters (config snapshot, layout, policies) so a job can be re-run/retried reproducibly.
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    output_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
