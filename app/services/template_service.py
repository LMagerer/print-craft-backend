"""Template business logic: create/update with versioning, duplicate, restore, ZIP import/export."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.units import MM_PER_INCH, dpi_for
from app.db.models import Font, Template
from app.db.repository import TemplateRepository, transaction
from app.schemas.api import TemplateCreate, TemplateUpdate
from app.schemas.config import SizeConfig, TemplateConfig
from app.services import font_manager, image_tools
from app.services.data_loader import get_store
from app.services.storage import find_by_sha, store_bytes

EXPORT_FORMAT = "printcraft-template"
# Exports made before the rename are still accepted.
IMPORT_FORMATS = {EXPORT_FORMAT, "tag-creator-template"}
DEFAULT_DPI = 300.0


class ServiceError(Exception):
    status = 400


class NotFound(ServiceError):
    status = 404


class Conflict(ServiceError):
    status = 409


class Invalid(ServiceError):
    status = 422


def _image(ref: str) -> tuple[Path, Image.Image]:
    path = find_by_sha(get_settings().templates_dir, ref)
    if path is None:
        raise NotFound(f"Image {ref} not found")
    return path, image_tools.open_image(path)


def default_size(img: Image.Image) -> SizeConfig:
    """Physical size from the file's DPI metadata (300 DPI when absent)."""
    dpi = image_tools.file_dpi(img) or DEFAULT_DPI
    if dpi < 10:
        dpi = DEFAULT_DPI
    return SizeConfig(width_mm=round(img.width / dpi * MM_PER_INCH, 4), height_mm=round(img.height / dpi * MM_PER_INCH, 4), dpi=dpi)


def _apply_image(t: Template, ref: str, config: TemplateConfig) -> None:
    """Point the template at image ``ref`` and sync the denormalised geometry columns from ``config.size``."""
    path, img = _image(ref)
    t.image_sha256 = ref
    t.image_path = str(path)
    t.width_px, t.height_px = img.width, img.height
    t.width_mm, t.height_mm = config.size.width_mm, config.size.height_mm
    t.dpi = round(dpi_for(img.width, config.size.width_mm), 3)
    t.icc_profile = image_tools.icc_description(img.info.get("icc_profile"))
    t.config_json = config.model_dump(mode="json")


def create_template(db: Session, body: TemplateCreate) -> Template:
    repo = TemplateRepository(db)
    with transaction(db):
        if repo.get_by_name(body.name):
            raise Conflict(f"A template named '{body.name}' already exists")
        _, img = _image(body.image_ref)
        config = body.config or TemplateConfig(size=default_size(img))
        t = Template(name=body.name, description=body.description, original_image_sha256=body.image_ref)
        _apply_image(t, body.image_ref, config)
        repo.add(t)
    return t


def update_template(db: Session, t: Template, body: TemplateUpdate) -> Template:
    """Save. Image/config changes create a new version; name/description/archive changes do not."""
    repo = TemplateRepository(db)
    with transaction(db):
        if body.name is not None and body.name != t.name:
            other = repo.get_by_name(body.name)
            if other and other.id != t.id:
                raise Conflict(f"A template named '{body.name}' already exists")
            t.name = body.name
        if body.description is not None:
            t.description = body.description
        if body.is_archived is not None:
            t.is_archived = body.is_archived
        if "dataset_id" in body.model_fields_set and body.dataset_id != t.dataset_id:
            if body.dataset_id is not None:
                try:
                    get_store().load(body.dataset_id)
                except KeyError as e:
                    raise NotFound("Dataset not found") from e
            t.dataset_id = body.dataset_id
        new_ref = body.image_ref or t.image_sha256
        new_config = body.config or TemplateConfig.model_validate(t.config_json)
        changed = new_ref != t.image_sha256 or new_config.model_dump(mode="json") != t.config_json
        if changed:
            _apply_image(t, new_ref, new_config)
            repo.new_version(t)
        db.flush()
    return t


def duplicate_template(db: Session, t: Template, name: str | None = None) -> Template:
    repo = TemplateRepository(db)
    with transaction(db):
        new_name = name or repo.unique_name(f"{t.name} (copy)")
        if repo.get_by_name(new_name):
            raise Conflict(f"A template named '{new_name}' already exists")
        copy = Template(
            name=new_name,
            description=t.description,
            original_image_sha256=t.original_image_sha256,
        )
        _apply_image(copy, t.image_sha256, TemplateConfig.model_validate(t.config_json))
        repo.add(copy)
    return copy


def restore_version(db: Session, t: Template, version: int) -> Template:
    """Restoring creates a new version equal to the old one (history is never rewritten)."""
    repo = TemplateRepository(db)
    with transaction(db):
        v = repo.version(t, version)
        if v is None:
            raise NotFound(f"Version {version} not found")
        _apply_image(t, v.image_ref, TemplateConfig.model_validate(v.config_json))
        repo.new_version(t)
    return t


def delete_template(db: Session, t: Template) -> None:
    with transaction(db):
        TemplateRepository(db).delete(t)


# --------------------------------------------------------------------------------------------------
# ZIP import / export
# --------------------------------------------------------------------------------------------------


def _font_ids(config: TemplateConfig) -> set[int]:
    ids: set[int] = set()
    for p in config.placeholders:
        for fid in (p.font.font_id, p.font.fallback_font_id):
            if fid is not None:
                ids.add(fid)
    return ids


def export_zip(db: Session, t: Template) -> bytes:
    """ZIP with ``config.json``, the image (original bytes) and every referenced font file."""
    config = TemplateConfig.model_validate(t.config_json)
    image_path = Path(t.image_path)
    # Include bold/italic siblings so variant switching still works after import.
    fonts: dict[int, Font] = {}
    for fid in _font_ids(config):
        f = db.get(Font, fid)
        if f is None:
            continue
        fonts[f.id] = f
        for sib in db.scalars(select(Font).where(Font.family == f.family)):
            fonts.setdefault(sib.id, sib)
    manifest: dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "format_version": 1,
        "name": t.name,
        "description": t.description,
        "image": f"image{image_path.suffix}",
        "config": config.model_dump(mode="json"),
        "fonts": [
            {"id": f.id, "file": f"fonts/{f.sha256}{Path(f.file_path).suffix}", "name": f.name, "family": f.family,
             "style": f.style, "filename": f.filename}
            for f in fonts.values()
        ],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("config.json", json.dumps(manifest, indent=2, ensure_ascii=False), compress_type=zipfile.ZIP_DEFLATED)
        # Images and fonts are stored as-is (PNG is already compressed).
        zf.write(image_path, manifest["image"], compress_type=zipfile.ZIP_STORED)
        for entry, f in zip(manifest["fonts"], fonts.values()):
            zf.write(f.file_path, entry["file"], compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


MAX_ZIP_ENTRY = 200 * 1024 * 1024


def _read_entry(zf: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = zf.getinfo(name)
    except KeyError as e:
        raise Invalid(f"ZIP entry '{name}' missing") from e
    if info.file_size > MAX_ZIP_ENTRY:
        raise Invalid(f"ZIP entry '{name}' is too large")
    return zf.read(info)


def import_zip(db: Session, data: bytes, name: str | None = None) -> Template:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise Invalid("Not a ZIP file") from e
    with zf:
        try:
            manifest = json.loads(_read_entry(zf, "config.json"))
        except json.JSONDecodeError as e:
            raise Invalid(f"config.json is not valid JSON: {e}") from e
        if manifest.get("format") not in IMPORT_FORMATS:
            raise Invalid("Not a template export (unknown format)")
        try:
            image_info = image_tools.store_image(_read_entry(zf, str(manifest["image"])))
        except ValueError as e:
            raise Invalid(str(e)) from e
        repo = TemplateRepository(db)
        with transaction(db):
            id_map: dict[int, int] = {}
            for entry in manifest.get("fonts", []):
                try:
                    font, _ = font_manager.register_font_bytes(db, _read_entry(zf, entry["file"]), entry.get("filename") or entry["file"])
                except font_manager.FontError as e:
                    raise Invalid(f"Font {entry.get('name')}: {e}") from e
                id_map[int(entry["id"])] = font.id
            raw = manifest["config"]
            for p in raw.get("placeholders", []):
                f = p.get("font", {})
                for key in ("font_id", "fallback_font_id"):
                    if f.get(key) is not None:
                        f[key] = id_map.get(int(f[key]))
            try:
                config = TemplateConfig.model_validate(raw)
            except ValidationError as e:
                raise Invalid(f"Invalid template config: {e}") from e
            t = Template(
                name=repo.unique_name(name or manifest.get("name") or "Imported template"),
                description=manifest.get("description", ""),
                original_image_sha256=image_info.ref,
            )
            _apply_image(t, image_info.ref, config)
            repo.add(t)
    return t
