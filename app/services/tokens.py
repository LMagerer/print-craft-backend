"""Placeholder binding resolution: ``{{field}}`` tokens, text transforms and filename sanitising."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

from app.schemas.config import Binding

TOKEN_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def extract_tokens(template: str) -> list[str]:
    """Field names referenced by ``{{field}}`` tokens, in order of first appearance."""
    seen: list[str] = []
    for m in TOKEN_RE.finditer(template or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def validate_tokens(template: str, fields: Iterable[str]) -> list[str]:
    """Return the tokens that do not match any available field."""
    available = set(fields)
    return [t for t in extract_tokens(template) if t not in available]


def value_to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        if value.is_integer():
            return str(int(value))
    return str(value)


def render_template(template: str, record: Mapping[str, Any]) -> str:
    """Replace ``{{field}}`` tokens with record values (unknown fields become empty strings)."""
    return TOKEN_RE.sub(lambda m: value_to_str(record.get(m.group(1))), template or "")


def resolve_binding(binding: Binding, record: Mapping[str, Any] | None, sample_text: str = "") -> str:
    """Text for a placeholder. Without a record (editor preview) the sample text / binding is shown."""
    if record is None:
        if sample_text:
            return sample_text
        if binding.mode == "template":
            return binding.template or ""
        return f"{{{{{binding.field}}}}}" if binding.field else ""
    if binding.mode == "template":
        return render_template(binding.template or "", record)
    if not binding.field:
        return ""
    return value_to_str(record.get(binding.field))


def binding_fields(binding: Binding) -> list[str]:
    if binding.mode == "template":
        return extract_tokens(binding.template or "")
    return [binding.field] if binding.field else []


def apply_transform(text: str, transform: str) -> str:
    """Text transform. ``capitalize`` upper-cases the first letter of each whitespace-separated word.

    Mirrors ``applyTransform`` in ``frontend/src/lib/tokens.ts``.
    """
    if transform == "uppercase":
        return text.upper()
    if transform == "lowercase":
        return text.lower()
    if transform == "capitalize":
        out = []
        prev_space = True
        for ch in text:
            out.append(ch.upper() if prev_space else ch)
            prev_space = ch.isspace()
        return "".join(out)
    return text


def suggest_bindings(labels: Iterable[str], fields: Iterable[str]) -> dict[str, str]:
    """Case-insensitive match of placeholder ids/labels to data fields (ignores spaces, '_' and '-')."""

    def norm(s: str) -> str:
        return re.sub(r"[\s_\-.]+", "", s).casefold()

    by_norm = {norm(f): f for f in fields}
    return {label: by_norm[norm(label)] for label in labels if norm(label) in by_norm}


_INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """Make ``name`` safe on Windows, macOS and Linux. Returns '' when nothing usable remains."""
    name = unicodedata.normalize("NFC", name or "")
    name = _INVALID_FS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()
    if name.upper().split(".")[0] in _RESERVED:
        name = f"_{name}"
    return name[:max_len].rstrip(" .")


class FilenameDeduper:
    """Hands out unique filenames: ``a``, ``a_2``, ``a_3`` ... (case-insensitive, for case-insensitive filesystems)."""

    def __init__(self) -> None:
        self._used: set[str] = set()

    def claim(self, stem: str, ext: str) -> str:
        candidate = f"{stem}.{ext}"
        n = 2
        while candidate.casefold() in self._used:
            candidate = f"{stem}_{n}.{ext}"
            n += 1
        self._used.add(candidate.casefold())
        return candidate
