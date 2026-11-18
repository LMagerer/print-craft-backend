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


class ImageSource(_Model):
    """``static``: always ``ref``. ``field``: per record, the column value names an uploaded photo (``ref`` is the
    editor sample / fallback)."""

    mode: Literal["static", "field"] = "static"
    ref: str | None = None
    field: str | None = None
    # fill: stretch to the box; contain: fit inside (letterbox); cover: fill the box and crop the overflow.
    fit: Literal["fill", "contain", "cover"] = "cover"


class ShapeStyle(_Model):
    # A line runs along the horizontal centre of its box (rotate the layer for other angles).
    type: Literal["rect", "ellipse", "line"] = "rect"
    fill: str | None = "#3b82f6"
    stroke: str | None = None
    stroke_width_mm: float = Field(0, ge=0, le=100)

    @field_validator("fill", "stroke")
    @classmethod
    def _colors(cls, v: str | None) -> str | None:
        return None if v is None or v == "" else _check_color(v)


class Layer(_Model):
    """One entry in the design stack (``TemplateConfig.layers``, bottom to top).

    ``text`` layers only reference a placeholder by id (``id == placeholder id``): its geometry, visibility and lock
    state live on the placeholder. Image and shape layers carry their own geometry; ``rotation`` is clockwise
    degrees around the box centre.
    """

    id: str = Field(min_length=1, max_length=100)
    kind: Literal["text", "image", "shape"]
    name: str = ""
    group_id: str | None = None
    x_mm: float = 0
    y_mm: float = 0
    width_mm: float = Field(10, gt=0)
    height_mm: float = Field(10, gt=0)
    rotation: float = Field(0, ge=-360, le=360)
    opacity: float = Field(1, ge=0, le=1)
    corner_radius_mm: float = Field(0, ge=0)
    locked: bool = False
    hidden: bool = False
    image: ImageSource | None = None
    shape: ShapeStyle | None = None

    @model_validator(mode="after")
    def _kind_payload(self) -> Layer:
        if self.kind == "image" and self.image is None:
            self.image = ImageSource()
        if self.kind == "shape" and self.shape is None:
            self.shape = ShapeStyle()
        return self


class Group(_Model):
    id: str = Field(min_length=1, max_length=100)
    name: str = "Group"


class TemplateConfig(_Model):
    size: SizeConfig = Field(default_factory=SizeConfig)
    # Painted under everything (``None`` keeps the base image / transparency as is).
    background: str | None = None
    placeholders: list[Placeholder] = Field(default_factory=list)
    # Design stack, bottom to top. Placeholders without a text layer are drawn on top, in list order.
    layers: list[Layer] = Field(default_factory=list)
    groups: list[Group] = Field(default_factory=list)
    output: OutputConfig = Field(default_factory=OutputConfig)

    _background = field_validator("background")(lambda cls, v: None if v is None or v == "" else _check_color(v))

    @model_validator(mode="after")
    def _unique_ids(self) -> TemplateConfig:
        ids = [p.id for p in self.placeholders]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"Duplicate placeholder ids: {sorted(dupes)}")
        layer_ids = [ly.id for ly in self.layers]
        dupes = {i for i in layer_ids if layer_ids.count(i) > 1}
        if dupes:
            raise ValueError(f"Duplicate layer ids: {sorted(dupes)}")
        placeholder_ids = set(ids)
        for ly in self.layers:
            if ly.kind == "text" and ly.id not in placeholder_ids:
                raise ValueError(f"Text layer '{ly.id}' has no matching placeholder")
            if ly.kind != "text" and ly.id in placeholder_ids:
                raise ValueError(f"Layer id '{ly.id}' is already used by a placeholder")
        group_ids = {g.id for g in self.groups}
        if len(group_ids) != len(self.groups):
            raise ValueError("Duplicate group ids")
        for ly in self.layers:
            if ly.group_id is not None and ly.group_id not in group_ids:
                raise ValueError(f"Layer '{ly.id}' references unknown group '{ly.group_id}'")
        return self

    def stack(self) -> list[Layer | Placeholder]:
        """Everything to draw, bottom to top: the layers, then placeholders that have no text layer."""
        by_id = {p.id: p for p in self.placeholders}
        out: list[Layer | Placeholder] = []
        seen: set[str] = set()
        for ly in self.layers:
            if ly.kind == "text":
                out.append(by_id[ly.id])
                seen.add(ly.id)
            else:
                out.append(ly)
        out += [p for p in self.placeholders if p.id not in seen]
        return out
