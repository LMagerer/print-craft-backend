import io
import json
import time
import zipfile
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.db import session as db_session
from app.db.models import Base
from app.main import create_app
from app.services.font_manager import seed_bundled_fonts
from tests.conftest import make_png

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    db_session.configure("sqlite://")
    Base.metadata.create_all(db_session.engine)
    with db_session.SessionLocal() as db:
        seed_bundled_fonts(db)
    return TestClient(create_app())


def _upload_image(client, w=1011, h=638):
    r = client.post(f"{API}/images", files={"file": ("card.png", make_png(w, h), "image/png")})
    assert r.status_code == 201, r.text
    return r.json()


def _config(width_mm=85.6, height_mm=54.0, field="name", **font):
    return {
        "size": {"width_mm": width_mm, "height_mm": height_mm, "dpi": 300},
        "placeholders": [
            {"id": "name", "binding": {"mode": "field", "field": field}, "x_mm": 5, "y_mm": 20,
             "box": {"width_mm": 75, "height_mm": 12}, "font": {"size_pt": 20, "color": "#ffffff", **font}}
        ],
        "output": {"filename_field": "name"},
    }


def _template(client, name="Card"):
    img = _upload_image(client)
    r = client.post(f"{API}/templates", json={"name": name, "image_ref": img["ref"], "config": _config()})
    assert r.status_code == 201, r.text
    return r.json()


def _dataset(client, records):
    r = client.post(f"{API}/datasets", data={"text": json.dumps(records)})
    assert r.status_code == 201, r.text
    return r.json()


def _wait(client, job_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"{API}/jobs/{job_id}").json()
        if j["status"] in ("completed", "completed_with_errors", "failed", "cancelled"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_fonts_seeded_and_metrics(client):
    fonts = client.get(f"{API}/fonts").json()
    assert {f["family"] for f in fonts} >= {"Noto Sans", "Roboto", "Montserrat"}
    m = client.get(f"{API}/fonts/{fonts[0]['id']}/metrics").json()
    assert m["units_per_em"] > 0 and str(ord("A")) in m["advances"]


def test_dataset_upload_paging_validate(client):
    ds = _dataset(client, [{"name": "Ada", "n": 1}, {"name": "", "n": 2}, {"name": "Cy", "n": 2}])
    assert ds["fields"] == ["name", "n"] and ds["row_count"] == 3
    page = client.get(f"{API}/datasets/{ds['id']}/records", params={"offset": 1, "limit": 1}).json()
    assert page["records"] == [{"name": "", "n": "2"}]
    v = client.post(f"{API}/datasets/{ds['id']}/validate", json={"required_fields": ["name"], "filename_field": "n"}).json()
    assert v["empty"]["name"]["rows"] == [1]
    assert v["duplicates"][0]["value"] == "2"
    assert client.get(f"{API}/datasets/" + "0" * 32).status_code == 404


def test_dataset_list_template_link_and_delete(client):
    a = _dataset(client, [{"name": "A"}])
    b = _dataset(client, [{"name": "B"}, {"name": "C"}])
    assert a["expires_at"] is None
    t = _template(client, "Linked")

    r = client.put(f"{API}/templates/{t['id']}", json={"dataset_id": a["id"]})
    assert r.status_code == 200 and r.json()["dataset_id"] == a["id"]
    assert r.json()["version"] == t["version"]  # linking data is not a design change
    assert client.put(f"{API}/templates/{t['id']}", json={"dataset_id": "f" * 32}).status_code == 404
    # Other updates leave the link alone.
    assert client.put(f"{API}/templates/{t['id']}", json={"description": "x"}).json()["dataset_id"] == a["id"]

    listed = {d["id"]: d for d in client.get(f"{API}/datasets").json()}
    assert listed[a["id"]]["template_ids"] == [t["id"]] and listed[b["id"]]["row_count"] == 2
    ids = list(listed)
    assert ids.index(b["id"]) < ids.index(a["id"])  # newest first

    assert client.delete(f"{API}/datasets/{a['id']}").status_code == 204
    assert client.get(f"{API}/datasets/{a['id']}").status_code == 404
    assert client.get(f"{API}/templates/{t['id']}").json()["dataset_id"] is None
    assert a["id"] not in {d["id"] for d in client.get(f"{API}/datasets").json()}
    assert client.delete(f"{API}/datasets/{a['id']}").status_code == 404


def test_csv_upload(client):
    r = client.post(f"{API}/datasets", files={"file": ("d.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 201 and r.json()["fields"] == ["a", "b"]


def test_template_crud_versions_and_restore(client):
    t = _template(client, "Players")
    assert t["version"] == 1 and t["width_px"] == 1011
    # Duplicate name -> unique name or conflict
    r = client.post(f"{API}/templates", json={"name": "players", "image_ref": t["image_sha256"] if "image_sha256" in t else _upload_image(client)["ref"]})
    assert r.status_code in (201, 409)
    cfg = t["config"]
    cfg["placeholders"][0]["font"]["size_pt"] = 30
    u = client.put(f"{API}/templates/{t['id']}", json={"config": cfg}).json()
    assert u["version"] == 2
    same = client.put(f"{API}/templates/{t['id']}", json={"description": "d"}).json()
    assert same["version"] == 2
    versions = client.get(f"{API}/templates/{t['id']}/versions").json()
    assert [v["version"] for v in versions][:2] in ([2, 1], [1, 2])
    restored = client.post(f"{API}/templates/{t['id']}/restore/1").json()
    assert restored["version"] == 3
    assert restored["config"]["placeholders"][0]["font"]["size_pt"] == 20
    names = [x["name"] for x in client.get(f"{API}/templates", params={"search": "play"}).json()]
    assert "Players" in names
    client.put(f"{API}/templates/{t['id']}", json={"is_archived": True})
    assert "Players" not in [x["name"] for x in client.get(f"{API}/templates").json()]
    assert "Players" in [x["name"] for x in client.get(f"{API}/templates", params={"archived": "only"}).json()]
    dup = client.post(f"{API}/templates/{t['id']}/duplicate", json={}).json()
    assert dup["name"] != "Players" and dup["version"] == 1
    assert client.delete(f"{API}/templates/{dup['id']}").status_code == 204
    assert client.get(f"{API}/templates/{dup['id']}").status_code == 404


def test_invalid_config_rejected(client):
    img = _upload_image(client)
    cfg = _config()
    cfg["placeholders"].append(dict(cfg["placeholders"][0]))
    r = client.post(f"{API}/templates", json={"name": "Bad", "image_ref": img["ref"], "config": cfg})
    assert r.status_code == 422


def test_export_import_roundtrip(client):
    t = _template(client, "Export me")
    r = client.get(f"{API}/templates/{t['id']}/export")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert "config.json" in zf.namelist()
    imp = client.post(f"{API}/templates/import", files={"file": ("t.zip", r.content, "application/zip")})
    assert imp.status_code == 201, imp.text
    assert imp.json()["name"] != "Export me"
    assert imp.json()["config"]["placeholders"] == t["config"]["placeholders"]


def test_transform_resample_and_bleed(client):
    img = _upload_image(client, 1000, 600)
    r = client.post(f"{API}/images/transform", json={
        "source_ref": img["ref"], "mode": "resample", "width_mm": 50, "height_mm": 30, "target_dpi": 300,
        "bleed_mm": 3, "config": _config(width_mm=84.667, height_mm=50.8),
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["width_mm"] == pytest.approx(56, abs=0.05)
    assert body["bleed_mm"] == pytest.approx(3, abs=25.4 / 300)  # whole pixels
    assert body["image"]["width_px"] == round(56 / 25.4 * 300)


def test_preview_and_glyph_check(client):
    img = _upload_image(client)
    r = client.post(f"{API}/render/preview", json={"config": _config(), "image_ref": img["ref"], "record": {"name": "Zoë ✓"}})
    assert r.status_code == 200
    assert Image.open(io.BytesIO(r.content)).size == (1011, 638)
    report = json.loads(unquote(r.headers["X-Render-Report"]))
    assert report[0]["id"] == "name"
    g = client.post(f"{API}/render/glyph-check", json={"config": _config(), "records": [{"name": "Hi \U0001F600"}, {"name": "ok"}]}).json()
    assert g["checked_rows"] == 2
    assert g["placeholders"][0]["rows"] == [0]


def test_card_job_zip_errors_retry_and_pdf(client):
    t = _template(client, "Job tpl")
    ds = _dataset(client, [{"name": "Ada"}, {"name": "ada"}, {"name": ""}, {"name": "Bob/Ünïcode"}])
    r = client.post(f"{API}/jobs", json={"template_id": t["id"], "dataset_id": ds["id"], "empty_policy": "skip"})
    assert r.status_code == 202, r.text
    job = _wait(client, r.json()["id"])
    assert job["status"] == "completed", job
    assert job["skipped_count"] == 1 and job["processed"] == 4
    z = client.get(f"{API}/jobs/{job['id']}/download")
    names = sorted(zipfile.ZipFile(io.BytesIO(z.content)).namelist())
    assert names == ["Ada.png", "Bob_Ünïcode.png", "ada_2.png"]
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())
        assert Image.open(io.BytesIO(zf.read("Ada.png"))).size == (1011, 638)
    # SSE stream ends with a done event for a finished job
    with client.stream("GET", f"{API}/jobs/{job['id']}/events") as s:
        text = "".join(s.iter_text())
    assert "event: done" in text
    # No failed rows -> retry refused
    assert client.post(f"{API}/jobs/{job['id']}/retry").status_code == 422

    lay = client.post(f"{API}/pdf/layout", json={"card_width_mm": 85.6, "card_height_mm": 54, "card_count": 3}).json()
    assert lay["per_page"] == 10 and lay["pages"] == 1
    p = client.post(f"{API}/pdf/jobs", json={"source_job_id": job["id"], "layout": {"card_width_mm": 85.6, "card_height_mm": 54}})
    assert p.status_code == 202, p.text
    pj = _wait(client, p.json()["id"])
    assert pj["status"] == "completed", pj
    pdf = client.get(f"{API}/jobs/{pj['id']}/download")
    assert pdf.content[:4] == b"%PDF"


def test_job_row_failure_does_not_stop_batch_and_retry(client, monkeypatch):
    from app.services import card_renderer

    t = _template(client, "Flaky")
    ds = _dataset(client, [{"name": "One"}, {"name": "BOOM"}, {"name": "Three"}])
    original = card_renderer.CardRenderer.render

    def flaky(self, record):
        if record and record.get("name") == "BOOM":
            raise RuntimeError("kaputt")
        return original(self, record)

    monkeypatch.setattr(card_renderer.CardRenderer, "render", flaky)
    job = _wait(client, client.post(f"{API}/jobs", json={"template_id": t["id"], "dataset_id": ds["id"]}).json()["id"])
    assert job["status"] == "completed_with_errors"
    detail = client.get(f"{API}/jobs/{job['id']}").json()
    assert detail["errors"][0]["row"] == 1 and "kaputt" in detail["errors"][0]["message"]
    assert len(client.get(f"{API}/jobs/{job['id']}/images").json()) == 2

    monkeypatch.setattr(card_renderer.CardRenderer, "render", original)
    assert client.post(f"{API}/jobs/{job['id']}/retry").status_code == 202
    job = _wait(client, job["id"])
    assert job["status"] == "completed" and job["error_count"] == 0
    assert [i["filename"] for i in client.get(f"{API}/jobs/{job['id']}/images").json()] == ["One.png", "BOOM.png", "Three.png"]


def test_cancel_queued_job(client):
    t = _template(client, "Cancel")
    ds = _dataset(client, [{"name": "x"}])
    jid = client.post(f"{API}/jobs", json={"template_id": t["id"], "dataset_id": ds["id"]}).json()["id"]
    r = client.post(f"{API}/jobs/{jid}/cancel")
    assert r.status_code == 200
    assert client.get(f"{API}/jobs").status_code == 200
