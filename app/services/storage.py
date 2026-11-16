"""Content-addressed file storage (files are named by their SHA-256, so identical uploads are deduplicated)."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

CHUNK = 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def store_bytes(directory: Path, data: bytes, ext: str) -> tuple[str, Path]:
    """Store ``data`` as ``<directory>/<sha256>.<ext>`` (atomically, only if new). Returns (sha, path)."""
    sha = sha256_bytes(data)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sha}.{ext.lstrip('.').lower()}"
    if not path.exists():
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return sha, path


def find_by_sha(directory: Path, sha: str) -> Path | None:
    """Locate a stored blob by hash regardless of extension."""
    if not sha or not all(c in "0123456789abcdef" for c in sha) or len(sha) != 64:
        return None
    matches = sorted(directory.glob(f"{sha}.*"))
    matches = [m for m in matches if not m.name.endswith(".tmp")]
    return matches[0] if matches else None
