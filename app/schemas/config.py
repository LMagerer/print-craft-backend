"""Template configuration schema (``config_json``).

This is the single source of truth: its JSON Schema is exported to the frontend and converted to zod
(``npm run gen:api``), so both sides validate the same structure.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
RGBA_COLOR = re.compile(r"^rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(,\s*(0|1|0?\.\d+)\s*)?\)$")


def _check_color(value: str) -> str:
    v = value.strip()
    if not (HEX_COLOR.match(v) or RGBA_COLOR.match(v)):
        raise ValueError(f"Invalid color '{value}'. Use #RGB, #RRGGBB, #RRGGBBAA or rgba(r,g,b,a)")
    return v


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SizeConfig(_Model):
    mode: Literal["print_size_only", "resample", "crop_fit"] = "print_size_only"
    width_mm: float = Field(85.6, gt=0, le=2000)
    height_mm: float = Field(54.0, gt=0, le=2000)
    dpi: float = Field(300, gt=0, le=4800)
    bleed_mm: float = Field(0, ge=0, le=50)


class Binding(_Model):
    """``field``: value of one data column. ``template``: text with ``{{field}}`` tokens."""

    mode: Literal["field", "template"] = "field"
    field: str | None = None
    template: str | None = None


class Box(_Model):
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)


class Shadow(_Model):
    enabled: bool = False
    color: str = "#00000080"
    offset_x_mm: float = 0.3
    offset_y_mm: float = 0.3
    blur_mm: float = 0.3

    _color = field_validator("color")(lambda cls, v: _check_color(v))


class FontConfig(_Model):
    font_id: int | None = None
    size_pt: float = Field(12, gt=0, le=1000)
    color: str = "#000000"
    # Auto-shrink lowers the size (in 0.5 pt steps) until the text fits, but never below this.
    auto_shrink: bool = True
    min_size_pt: float | None = Field(6, gt=0)
    wrap: bool = False
    # Extra space between characters, in points.
    letter_spacing: float = 0
    # Line height as a multiple of the font size.
    line_height: float = Field(1.2, gt=0, le=10)
    transform: Literal["none", "uppercase", "lowercase", "capitalize"] = "none"
    bold: bool = False
    italic: bool = False
    stroke_width_pt: float = Field(0, ge=0)
    stroke_color: str = "#000000"
    shadow: Shadow = Field(default_factory=Shadow)
    fallback_font_id: int | None = None

    _color = field_validator("color", "stroke_color")(lambda cls, v: _check_color(v))


class Placeholder(_Model):
    id: str = Field(min_length=1, max_length=100)
    label: str = ""
    binding: Binding = Field(default_factory=Binding)
    # Top-left corner of the text box, measured from the top-left of the image (including bleed).
    x_mm: float
    y_mm: float
    box: Box
    align: Literal["left", "center", "right"] = "left"
    valign: Literal["top", "middle", "bottom"] = "top"
    font: FontConfig = Field(default_factory=FontConfig)
    sample_text: str = ""
    locked: bool = False
    hidden: bool = False


class OutputConfig(_Model):
    filename_field: str | None = None
    format: Literal["png"] = "png"


class TemplateConfig(_Model):
    size: SizeConfig = Field(default_factory=SizeConfig)
    placeholders: list[Placeholder] = Field(default_factory=list)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @model_validator(mode="after")
    def _unique_ids(self) -> TemplateConfig:
        ids = [p.id for p in self.placeholders]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"Duplicate placeholder ids: {sorted(dupes)}")
        return self
