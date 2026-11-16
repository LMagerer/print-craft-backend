"""Parsing of Excel / CSV / JSON data sources and temporary server-side dataset storage."""

from __future__ import annotations

import csv
import io
import json
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.config import get_settings


class DataError(ValueError):
    pass


Record = dict[str, str]


# --------------------------------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------------------------------


def cell_to_str(v: Any) -> str:
    """Normalise a cell value to the string shown on cards (1001.0 -> '1001', dates -> ISO)."""
    if v is None:
        return ""
    if isinstance(v, float):
        if v != v:
            return ""
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == dtime(0, 0) else v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, pd.Timestamp):
        return cell_to_str(v.to_pydatetime())
    if v is pd.NaT:
        return ""
    return str(v)


def unique_headers(raw: list[Any]) -> list[str]:
    """Clean header names: blanks become ``column_N``, duplicates get ``_2``, ``_3`` ..."""
    out: list[str] = []
    seen: set[str] = set()
    for i, h in enumerate(raw, start=1):
        name = cell_to_str(h).strip() or f"column_{i}"
        base, n = name, 2
        while name in seen:
            name = f"{base}_{n}"
            n += 1
        seen.add(name)
        out.append(name)
    return out


def flatten(obj: Any, prefix: str = "", out: dict[str, str] | None = None) -> dict[str, str]:
    """Flatten nested JSON with dot notation: ``{"a": {"b": 1}, "t": [x, y]}`` -> ``a.b``, ``t.0``, ``t.1``."""
    if out is None:
        out = {}
    if isinstance(obj, dict):
        if not obj and prefix:
            out[prefix] = ""
        for k, v in obj.items():
            flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, list):
        if not obj and prefix:
            out[prefix] = ""
        for i, v in enumerate(obj):
            flatten(v, f"{prefix}.{i}" if prefix else str(i), out)
    else:
        out[prefix or "value"] = cell_to_str(obj)
    return out


def collect_fields(records: list[Record]) -> list[str]:
    """Union of keys over all records, in first-seen order."""
    seen: dict[str, None] = {}
    for r in records:
        for k in r:
            seen.setdefault(k, None)
    return list(seen)


# --------------------------------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------------------------------


@dataclass
class ParsedData:
    records: list[Record]
    fields: list[str]
    kind: str
    sheets: list[str]
    sheet: str | None = None


def parse_json(data: bytes | str) -> ParsedData:
    """Accept an array of objects, or ``{"records": [...]}``."""
    try:
        obj = json.loads(data.decode("utf-8-sig") if isinstance(data, bytes) else data)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise DataError(f"Invalid JSON: {e}") from e
    if isinstance(obj, dict) and isinstance(obj.get("records"), list):
        obj = obj["records"]
    if not isinstance(obj, list):
        raise DataError('JSON must be an array of objects or {"records": [...]}')
    records: list[Record] = []
    for i, item in enumerate(obj):
        if not isinstance(item, dict):
            raise DataError(f"Record {i} is not an object")
        records.append(flatten(item))
    return ParsedData(records, collect_fields(records), "json", [])


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise DataError("Cannot decode CSV file")


def parse_csv(data: bytes) -> ParsedData:
    text = _decode(data)
    if not text.strip():
        raise DataError("CSV file is empty")
    try:
        dialect = csv.Sniffer().sniff(text[:64 * 1024], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise DataError("CSV file has no rows")
    headers = unique_headers(rows[0])
    records = []
    for r in rows[1:]:
        r = r + [""] * (len(headers) - len(r))
        records.append({h: r[i] for i, h in enumerate(headers)})
    return ParsedData(records, headers, "csv", [])


def parse_excel(data: bytes, sheet: str | None = None) -> ParsedData:
    """First row is the header. Formulas yield their cached values (as shown in Excel)."""
    try:
        xls = pd.ExcelFile(io.BytesIO(data), engine="openpyxl")
    except Exception as e:
        raise DataError(f"Invalid Excel file: {e}") from e
    with xls:
        sheets = [str(n) for n in xls.sheet_names]
        name = sheet or sheets[0]
        if name not in sheets:
            raise DataError(f"Sheet '{name}' not found")
        df = pd.read_excel(xls, sheet_name=name, header=None, dtype=object)
    df = df.astype(object).where(pd.notna(df), None)
    rows = [r for r in df.values.tolist() if any(c is not None and str(c).strip() for c in r)]
    if not rows:
        return ParsedData([], [], "xlsx", sheets, name)
    headers = unique_headers(list(rows[0]))
    records = []
    for r in rows[1:]:
        vals = list(r) + [None] * (len(headers) - len(r))
        records.append({h: cell_to_str(vals[i]) for i, h in enumerate(headers)})
    return ParsedData(records, headers, "xlsx", sheets, name)


def parse_upload(filename: str, data: bytes, sheet: str | None = None) -> ParsedData:
    ext = Path(filename).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return parse_excel(data, sheet)
    if ext in (".csv", ".tsv", ".txt"):
        return parse_csv(data)
    if ext == ".json":
        return parse_json(data)
    raise DataError(f"Unsupported file type '{ext}'. Use .xlsx, .csv or .json")


# --------------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------------


def validate_records(
    records: list[Record],
    fields: list[str],
    required_fields: list[str],
    filename_field: str | None,
    max_rows_listed: int = 100,
) -> dict[str, Any]:
    """Empty values in ``required_fields``, duplicate filename values and fields missing from the data."""
    available = set(fields)
    empty: dict[str, dict[str, Any]] = {}
    for f in required_fields:
        if f not in available:
            continue
        rows = [i for i, r in enumerate(records) if not (r.get(f) or "").strip()]
        if rows:
            empty[f] = {"count": len(rows), "rows": rows[:max_rows_listed]}
    duplicates: list[dict[str, Any]] = []
    if filename_field and filename_field in available:
        by_value: dict[str, list[int]] = {}
        for i, r in enumerate(records):
            v = (r.get(filename_field) or "").strip()
            if v:
                by_value.setdefault(v.casefold(), []).append(i)
        duplicates = [
            {"value": records[rows[0]][filename_field], "rows": rows[:max_rows_listed]} for rows in by_value.values() if len(rows) > 1
        ]
    return {
        "row_count": len(records),
        "empty": empty,
        "duplicates": duplicates,
        "unmapped_fields": sorted({f for f in required_fields if f not in available} | ({filename_field} - available if filename_field else set())),
    }


# --------------------------------------------------------------------------------------------------
# Temporary dataset store
# --------------------------------------------------------------------------------------------------


class DatasetStore:
    """Datasets live on disk under ``data/datasets/<id>/``.

    They are kept until deleted, unless ``dataset_ttl_hours`` > 0, in which case they expire after that time.
    """

    def __init__(self, root: Path, ttl_seconds: float) -> None:
        self.root = root
        self.ttl = ttl_seconds
        self._cache: OrderedDict[str, tuple[dict[str, Any], list[Record]]] = OrderedDict()
        self._lock = threading.Lock()

    def save(self, parsed: ParsedData, filename: str) -> dict[str, Any]:
        ds_id = uuid.uuid4().hex
        d = self.root / ds_id
        d.mkdir(parents=True)
        now = time.time()
        meta = {
            "id": ds_id,
            "filename": filename,
            "kind": parsed.kind,
            "sheets": parsed.sheets,
            "sheet": parsed.sheet,
            "fields": parsed.fields,
            "row_count": len(parsed.records),
            "created_at": now,
            "expires_at": now + self.ttl if self.ttl > 0 else None,
        }
        (d / "records.json").write_text(json.dumps(parsed.records, ensure_ascii=False), encoding="utf-8")
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        self.cleanup()
        return meta

    def _valid_id(self, ds_id: str) -> bool:
        return len(ds_id) == 32 and all(c in "0123456789abcdef" for c in ds_id)

    def load(self, ds_id: str) -> tuple[dict[str, Any], list[Record]]:
        if not self._valid_id(ds_id):
            raise KeyError(ds_id)
        with self._lock:
            if ds_id in self._cache:
                self._cache.move_to_end(ds_id)
                meta, recs = self._cache[ds_id]
                if not self._expired(meta):
                    return meta, recs
                del self._cache[ds_id]
        d = self.root / ds_id
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            if self._expired(meta):
                shutil.rmtree(d, ignore_errors=True)
                raise KeyError(ds_id)
            records = json.loads((d / "records.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as e:
            raise KeyError(ds_id) from e
        with self._lock:
            self._cache[ds_id] = (meta, records)
            while len(self._cache) > 8:
                self._cache.popitem(last=False)
        return meta, records

    def _expired(self, meta: dict[str, Any]) -> bool:
        # Datasets saved while a TTL was configured keep a timestamp; it only applies while a TTL is set.
        exp = meta.get("expires_at")
        return self.ttl > 0 and exp is not None and exp <= time.time()

    def list(self) -> list[dict[str, Any]]:
        """Metadata of all stored datasets, newest first."""
        out: list[dict[str, Any]] = []
        if not self.root.exists():
            return out
        for d in self.root.iterdir():
            try:
                meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
                continue
            if not self._expired(meta):
                meta.setdefault("created_at", d.stat().st_mtime)
                out.append(meta)
        out.sort(key=lambda m: m["created_at"], reverse=True)
        return out

    def delete(self, ds_id: str) -> bool:
        if not self._valid_id(ds_id):
            return False
        with self._lock:
            self._cache.pop(ds_id, None)
        d = self.root / ds_id
        if not d.is_dir():
            return False
        shutil.rmtree(d, ignore_errors=True)
        return True

    def path(self, ds_id: str) -> Path:
        return self.root / ds_id / "records.json"

    def cleanup(self) -> int:
        removed = 0
        if self.ttl <= 0 or not self.root.exists():
            return 0
        now = time.time()
        for d in self.root.iterdir():
            meta_file = d / "meta.json"
            try:
                expired = self._expired(json.loads(meta_file.read_text()))
            except Exception:
                expired = d.is_dir() and (now - d.stat().st_mtime) > self.ttl
            if expired:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        return removed


_store: DatasetStore | None = None


def get_store() -> DatasetStore:
    global _store
    s = get_settings()
    if _store is None or _store.root != s.datasets_dir:
        _store = DatasetStore(s.datasets_dir, s.dataset_ttl_hours * 3600)
    return _store
