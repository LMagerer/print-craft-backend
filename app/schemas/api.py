"""Request/response schemas for fonts, datasets, rendering, templates, jobs and PDF layout."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.config import TemplateConfig, _check_color

# ----------------------------------------------------------------------------------------------- fonts


class FontOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    family: str
    style: str
    filename: str
    sha256: str
    source: str


class SystemFont(BaseModel):
    family: str
    style: str
    path: str


class SystemFontImport(BaseModel):
    path: str


class FontMetricsOut(BaseModel):
    units_per_em: int
    ascender: int
    descender: int
    notdef_advance: int
    advances: dict[str, int]


# -------------------------------------------------------------------------------------------- datasets


class DatasetOut(BaseModel):
    id: str
    filename: str
    kind: str
    sheets: list[str]
    sheet: str | None
    fields: list[str]
    row_count: int
    created_at: float = 0
    expires_at: float | None = None
    # Templates that use this dataset (only filled by the list endpoint).
    template_ids: list[int] = Field(default_factory=list)


class RecordsPage(BaseModel):
    offset: int
    limit: int
    total: int
    fields: list[str]
    records: list[dict[str, str]]


class ValidateRequest(BaseModel):
    required_fields: list[str] = Field(default_factory=list, description="Fields that must not be empty")
    filename_field: str | None = None


class EmptyInfo(BaseModel):
    count: int
    rows: list[int]


class DuplicateInfo(BaseModel):
    value: str
    rows: list[int]


class ValidateResponse(BaseModel):
    row_count: int
    empty: dict[str, EmptyInfo]
    duplicates: list[DuplicateInfo]
    unmapped_fields: list[str]


# ----------------------------------------------------------------------------------------------- render


class PreviewRequest(BaseModel):
    config: TemplateConfig
    image_ref: str
    dataset_id: str | None = None
    record_index: int | None = Field(None, ge=0)
    # Inline record (wins over dataset_id/record_index).
    record: dict[str, str] | None = None
    # Downscale the returned PNG (rendering itself is always full resolution).
    max_width: int | None = Field(None, gt=0)


class GlyphCheckRequest(BaseModel):
    config: TemplateConfig
    dataset_id: str | None = None
    records: list[dict[str, str]] | None = None


class GlyphCheckItem(BaseModel):
    placeholder_id: str
    missing: dict[str, int] = Field(description="Missing character -> number of rows affected")
    rows: list[int]


class GlyphCheckResponse(BaseModel):
    checked_rows: int
    placeholders: list[GlyphCheckItem]


# -------------------------------------------------------------------------------------------- templates


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    image_ref: str
    config: TemplateConfig | None = None


class BlankTemplateCreate(BaseModel):
    """A new design. Without ``image_ref`` an empty canvas of the given size; with it, the canvas takes the image's
    size and the image is added as a layer."""

    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    width_mm: float = Field(85.6, gt=0, le=2000)
    height_mm: float = Field(54.0, gt=0, le=2000)
    dpi: float = Field(300, ge=50, le=1200)
    background: str | None = "#ffffff"
    image_ref: str | None = None
    image_name: str | None = None

    @field_validator("background")
    @classmethod
    def _bg(cls, v: str | None) -> str | None:
        return None if v is None or v == "" else _check_color(v)


class TemplateUpdate(BaseModel):
    """Every save creates a new version when the image or config changes."""

    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    image_ref: str | None = None
    config: TemplateConfig | None = None
    is_archived: bool | None = None
    # Dataset linked to the template; send null to unlink. Omit to leave unchanged. Does not create a version.
    dataset_id: str | None = None


class TemplateSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    created_at: datetime
    updated_at: datetime
    image_sha256: str
    width_px: int
    height_px: int
    dpi: float
    width_mm: float
    height_mm: float
    version: int
    is_archived: bool
    dataset_id: str | None = None


class TemplateOut(TemplateSummary):
    original_image_sha256: str | None
    icc_profile: str | None
    config: TemplateConfig


class TemplateVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version: int
    image_ref: str
    created_at: datetime


class DuplicateRequest(BaseModel):
    name: str | None = None


# ------------------------------------------------------------------------------------------------- jobs

JobStatus = Literal["queued", "running", "completed", "completed_with_errors", "failed", "cancelled"]


class JobCreate(BaseModel):
    template_id: int
    dataset_id: str
    # Overrides the template's output.filename_field.
    filename_field: str | None = None
    # Only render these rows (0-based). None = all rows.
    rows: list[int] | None = None
    # What to do with rows whose bound fields are empty.
    empty_policy: Literal["render", "skip", "fill"] = "render"
    fill_value: str = ""
    # Use this config instead of the saved one (e.g. unsaved editor state).
    config: TemplateConfig | None = None


class JobError(BaseModel):
    row: int
    message: str
    skipped: bool = False
    warning: bool = False


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    template_id: int | None
    data_filename: str
    row_count: int
    status: str
    progress: float
    processed: int
    created_at: datetime
    finished_at: datetime | None
    has_output: bool = False
    error_count: int = 0
    warning_count: int = 0
    skipped_count: int = 0


class JobImage(BaseModel):
    row: int
    filename: str


class JobDetail(JobOut):
    errors: list[JobError]
    params: dict[str, Any]


# -------------------------------------------------------------------------------------------------- pdf


class Margins(BaseModel):
    top: float = Field(10, ge=0)
    right: float = Field(10, ge=0)
    bottom: float = Field(10, ge=0)
    left: float = Field(10, ge=0)


class LayoutRequest(BaseModel):
    card_width_mm: float = Field(gt=0)
    card_height_mm: float = Field(gt=0)
    page_width_mm: float = Field(210, gt=0)
    page_height_mm: float = Field(297, gt=0)
    margins: Margins = Field(default_factory=Margins)
    gap_mm: float = Field(0, ge=0)
    allow_rotate: bool = True
    card_count: int = Field(0, ge=0)


class CardSlot(BaseModel):
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float


class LayoutResponse(BaseModel):
    columns: int
    rows: int
    per_page: int
    rotated: bool
    pages: int
    page_width_mm: float
    page_height_mm: float
    slot_width_mm: float
    slot_height_mm: float
    slots: list[CardSlot]


class PdfJobCreate(BaseModel):
    source_job_id: int = Field(description="Completed card job whose PNGs are merged")
    layout: LayoutRequest
    crop_marks: bool = True
    # Width of the bleed included in the card images; crop marks are drawn at the trim line.
    bleed_mm: float = Field(0, ge=0)
    # Cards above this resolution are downsampled before embedding; None keeps the original pixels.
    dpi: int | None = Field(300, ge=72, le=2400)
    # "png" is lossless; "jpeg" is much smaller (transparency is flattened onto white).
    image_format: Literal["png", "jpeg"] = "png"
    jpeg_quality: int = Field(90, ge=40, le=100)
