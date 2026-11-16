"""Physical unit conversions. All lengths are stored in millimetres; pixels only exist at render time."""

from __future__ import annotations

from typing import Literal

Unit = Literal["mm", "cm", "in", "px", "pt"]

MM_PER_INCH = 25.4
PT_PER_INCH = 72.0


def to_mm(value: float, unit: Unit, dpi: float | None = None) -> float:
    """Convert ``value`` expressed in ``unit`` to millimetres (``px`` needs a DPI)."""
    if unit == "mm":
        return value
    if unit == "cm":
        return value * 10.0
    if unit == "in":
        return value * MM_PER_INCH
    if unit == "pt":
        return value * MM_PER_INCH / PT_PER_INCH
    if unit == "px":
        if not dpi or dpi <= 0:
            raise ValueError("DPI is required to convert pixels")
        return value * MM_PER_INCH / dpi
    raise ValueError(f"Unknown unit: {unit}")


def from_mm(value_mm: float, unit: Unit, dpi: float | None = None) -> float:
    """Convert millimetres to ``unit`` (``px`` needs a DPI)."""
    if unit == "mm":
        return value_mm
    if unit == "cm":
        return value_mm / 10.0
    if unit == "in":
        return value_mm / MM_PER_INCH
    if unit == "pt":
        return value_mm * PT_PER_INCH / MM_PER_INCH
    if unit == "px":
        if not dpi or dpi <= 0:
            raise ValueError("DPI is required to convert pixels")
        return value_mm * dpi / MM_PER_INCH
    raise ValueError(f"Unknown unit: {unit}")


def mm_to_px(value_mm: float, dpi: float) -> float:
    return value_mm * dpi / MM_PER_INCH


def px_to_mm(value_px: float, dpi: float) -> float:
    return value_px * MM_PER_INCH / dpi


def dpi_for(pixels: float, size_mm: float) -> float:
    """Effective DPI when ``pixels`` span ``size_mm``: DPI = pixels / (size_mm / 25.4)."""
    if size_mm <= 0:
        raise ValueError("size_mm must be positive")
    return pixels / (size_mm / MM_PER_INCH)


def pixels_for(size_mm: float, dpi: float) -> int:
    """Pixel count needed to print ``size_mm`` at ``dpi`` (rounded to the nearest pixel, min 1)."""
    return max(1, round(size_mm / MM_PER_INCH * dpi))


def pt_to_px(size_pt: float, dpi: float) -> float:
    return size_pt * dpi / PT_PER_INCH


def summary(width_mm: float, height_mm: float, width_px: int, height_px: int) -> str:
    """Human readable summary such as ``85.6 x 54.0 mm | 1012 x 638 px | 300 DPI``."""
    dpi = dpi_for(width_px, width_mm)
    return f"{width_mm:.1f} x {height_mm:.1f} mm | {width_px} x {height_px} px | {dpi:.0f} DPI"
