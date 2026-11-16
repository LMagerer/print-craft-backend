"""Schemas for image upload and the size editor."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.config import TemplateConfig

Anchor = Literal["top-left", "top", "top-right", "left", "center", "right", "bottom-left", "bottom", "bottom-right"]


class ImageInfo(BaseModel):
    ref: str = Field(description="SHA-256 of the stored image")
    width_px: int
    height_px: int
    # DPI stored in the file metadata (None when absent).
    dpi: float | None
    mode: str
    has_alpha: bool
    icc_profile: str | None = Field(None, description="Description of the embedded ICC profile, if any")
    format: str


class CropBox(BaseModel):
    """Crop rectangle in source pixels (after rotation/flip)."""

    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class TransformRequest(BaseModel):
    source_ref: str
    mode: Literal["print_size_only", "resample", "crop_fit"] = "print_size_only"
    # Target trim size (bleed is added on top). In print_size_only mode height follows the pixel aspect.
    width_mm: float = Field(gt=0, le=2000)
    height_mm: float = Field(gt=0, le=2000)
    target_dpi: float = Field(300, gt=0, le=4800)
    rotate: Literal[0, 90, 180, 270] = 0
    flip_h: bool = False
    flip_v: bool = False
    crop: CropBox | None = None
    # crop_fit without an explicit crop box: "cover" crops to the target aspect, "contain" pads.
    fit: Literal["cover", "contain"] = "cover"
    anchor: Anchor = "center"
    pad_color: str = "#FFFFFF"
    # Positive adds bleed on every side, negative trims.
    bleed_mm: float = Field(0, ge=-50, le=50)
    bleed_fill: Literal["mirror", "color"] = "mirror"
    # Flatten transparency onto this color (None keeps the alpha channel).
    background: str | None = None
    # Current template config; when given, placeholders are mapped to the new geometry.
    config: TemplateConfig | None = None
    scale_placeholders: bool = True


class TransformResponse(BaseModel):
    image: ImageInfo
    width_mm: float
    height_mm: float
    dpi: float
    bleed_mm: float
    summary: str
    warnings: list[str]
    config: TemplateConfig | None = None
