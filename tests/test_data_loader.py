import io
import json

import pandas as pd
import pytest

from app.services.data_loader import DataError, parse_csv, parse_excel, parse_json, parse_upload, validate_records


def test_json_flatten_nested_and_lists():
    data = json.dumps({"records": [{"name": "Ada", "team": {"city": "London"}, "tags": ["a", "b"], "n": 7.0}]})
    p = parse_json(data)
    assert p.records == [{"name": "Ada", "team.city": "London", "tags.0": "a", "tags.1": "b", "n": "7"}]
    assert p.fields == ["name", "team.city", "tags.0", "tags.1", "n"]


def test_json_rejects_scalar():
    with pytest.raises(DataError):
        parse_json("42")


def test_csv_semicolon_and_cp1252():
    raw = "Name;Größe\nMüller;3,5\n".encode("cp1252")
    p = parse_csv(raw)
    assert p.fields == ["Name", "Größe"]
    assert p.records == [{"Name": "Müller", "Größe": "3,5"}]


def test_excel_sheets_types_and_headers():
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        pd.DataFrame({"Name": ["Ada", "Bob"], "Number": [7, 8.5], "Name ": ["x", None]}).to_excel(w, sheet_name="Players", index=False)
        pd.DataFrame({"A": [1]}).to_excel(w, sheet_name="Other", index=False)
    data = buf.getvalue()
    p = parse_excel(data)
    assert p.sheets == ["Players", "Other"]
    assert p.sheet == "Players"
    assert p.records[0]["Number"] == "7"
    assert p.records[1]["Number"] == "8.5"
    assert len(set(p.fields)) == len(p.fields)
    assert parse_upload("x.xlsx", data, "Other").records == [{"A": "1"}]


def test_validate_records_empty_and_duplicates():
    recs = [{"name": "A", "id": "1"}, {"name": "", "id": "1"}, {"name": "C", "id": "2"}]
    r = validate_records(recs, ["name", "id"], ["name"], "id")
    assert r["empty"]["name"]["count"] == 1
    assert r["empty"]["name"]["rows"] == [1]
    assert r["duplicates"][0]["value"] == "1"
