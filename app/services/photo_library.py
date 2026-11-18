"""Photo library for data-bound image layers.

Photos are stored content-addressed next to template images; ``photos.json`` maps each uploaded file name to its
SHA-256. A record value matches a photo by exact name, then case-insensitively, then by name without extension
(so ``Ada``, ``ada.JPG`` and ``photos/ada.jpg`` all find ``ada.jpg``).
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import threading
import zipfile
from pathlib import Path, PurePosixPath

from app.core.config import get_settings
from app.services import image_tools
from app.services.storage import find_by_sha

_lock = threading.Lock()
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_ZIP_ENTRY = 100 * 1024 * 1024
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif", ".pdf"}


def _index_path() -> Path:
    return get_settings().data_dir / "photos.json"


def load_index() -> dict[str, str]:
    try:
        data = json.loads(_index_path().read_text("utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_index(index: dict[str, str]) -> None:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(index.items(), key=lambda kv: kv[0].lower())), f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _clean_name(name: str) -> str:
    return PurePosixPath(name.replace("\\", "/")).name.strip()


def add_files(files: list[tuple[str, bytes]]) -> tuple[dict[str, str], list[str]]:
    """Store photos (ZIPs are expanded). Returns ``({name: ref} added, [errors])``. Same names are replaced."""
    added: dict[str, str] = {}
    errors: list[str] = []

    def store(name: str, data: bytes) -> None:
        name = _clean_name(name)
        if not name or name.startswith("."):
            return
        try:
            added[name] = image_tools.store_image(data).ref
        except ValueError as e:
            errors.append(f"{name}: {e}")

    for name, data in files:
        if data[:4] == b"PK\x03\x04":
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for info in zf.infolist():
                        if info.is_dir() or "__MACOSX" in info.filename:
                            continue
                        if Path(info.filename).suffix.lower() not in IMAGE_EXT:
                            continue
                        if info.file_size > MAX_ZIP_ENTRY:
                            errors.append(f"{info.filename}: too large")
                            continue
                        store(info.filename, zf.read(info))
            except zipfile.BadZipFile:
                errors.append(f"{name}: not a valid ZIP file")
        else:
            store(name, data)
    with _lock:
        index = load_index()
        index.update(added)
        _save_index(index)
    return added, errors


def remove(name: str) -> bool:
    with _lock:
        index = load_index()
        if name not in index:
            return False
        del index[name]
        _save_index(index)
    return True


class PhotoResolver:
    """Snapshot of the library used while rendering (one per renderer / job)."""

    def __init__(self, index: dict[str, str] | None = None) -> None:
        self.index = load_index() if index is None else index
        self.lower: dict[str, str] = {}
        self.stems: dict[str, str] = {}
        for name, ref in self.index.items():
            self.lower.setdefault(name.lower(), ref)
            self.stems.setdefault(Path(name).stem.lower(), ref)

    def ref_for(self, value: str) -> str | None:
        v = value.strip()
        if not v:
            return None
        if SHA_RE.match(v):
            return v
        name = _clean_name(v)
        return self.index.get(name) or self.lower.get(name.lower()) or self.stems.get(Path(name).stem.lower())

    @staticmethod
    def path_for(ref: str) -> Path | None:
        return find_by_sha(get_settings().templates_dir, ref)
