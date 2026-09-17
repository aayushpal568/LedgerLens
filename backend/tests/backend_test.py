"""Backend regression tests for Accounting Document Checker."""
import io
import os
import time
import pytest
import requests

BASE = os.environ.get("REACT_APP_BACKEND_URL", "http://127.0.0.1:8001").rstrip("/") + "/api"


@pytest.fixture(scope="session")
def s():
    session = requests.Session()
    token = os.environ.get("LEDGERLENS_BACKEND_TOKEN", "")
    if token:
        session.headers["x-ledgerlens-token"] = token
    return session


@pytest.fixture(scope="session")
def client_id(s):
    r = s.post(f"{BASE}/clients", json={"name": "TEST_Regression Inc", "client_type": "Small Business"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    yield cid
    s.delete(f"{BASE}/clients/{cid}")


# ------------------ firm ------------------
def test_firm_get_and_put(s):
    r = s.get(f"{BASE}/firm")
    assert r.status_code == 200
    r = s.put(f"{BASE}/firm", json={"name": "TEST_Firm", "contact_email": "test@x.com", "retention_note": "local"})
    assert r.status_code == 200
    assert r.json()["name"] == "TEST_Firm"
    r2 = s.get(f"{BASE}/firm")
    assert r2.json()["name"] == "TEST_Firm"


# --------------- clients CRUD ---------------
def test_client_list_has_fields(s, client_id):
    r = s.get(f"{BASE}/clients")
    assert r.status_code == 200
    cs = r.json()
    found = [c for c in cs if c["id"] == client_id]
    assert found and "file_count" in found[0] and "last_scan" in found[0]


def test_client_get(s, client_id):
    r = s.get(f"{BASE}/clients/{client_id}")
    assert r.status_code == 200
    assert r.json()["id"] == client_id


# ------------ checklist templates seeded ---
def test_templates_seeded_and_crud(s):
    r = s.get(f"{BASE}/checklist-templates")
    assert r.status_code == 200
    names = [t["name"] for t in r.json()]
    for n in ["Individual Tax Return", "Small Business Package", "Corporation Year-End"]:
        assert n in names, f"Missing seeded template {n}. Got {names}"

    # Create + update + delete
    body = {"name": "TEST_Tpl", "client_type": "Small Business",
            "items": [{"name": "Bank Statement", "aliases": ["bank"], "allowed_types": ["PDF"], "rule": {}}]}
    c = s.post(f"{BASE}/checklist-templates", json=body)
    assert c.status_code == 200
    tid = c.json()["id"]
    body["name"] = "TEST_Tpl2"
    u = s.put(f"{BASE}/checklist-templates/{tid}", json=body)
    assert u.status_code == 200 and u.json()["name"] == "TEST_Tpl2"
    d = s.delete(f"{BASE}/checklist-templates/{tid}")
    assert d.status_code == 200


# ------------- files upload/list/delete ----
def _upload(s, cid, files):
    return s.post(f"{BASE}/clients/{cid}/files", files=files)


def test_file_upload_and_list(s, client_id):
    # two byte-identical CSVs
    csv_bytes = b"date,amount\n2024-01-01,100\n2024-02-01,200\n"
    files = [
        ("files", ("ledger_a.csv", io.BytesIO(csv_bytes), "text/csv")),
        ("files", ("ledger_b.csv", io.BytesIO(csv_bytes), "text/csv")),
        ("files", ("profit_and_loss_2022.csv", io.BytesIO(b"date,amount\n2022-05-01,50\n"), "text/csv")),
        ("files", ("receipt.png", io.BytesIO(b"\x89PNG\r\n\x1a\nfakeimg"), "image/png")),
    ]
    r = _upload(s, client_id, files)
    assert r.status_code == 200, r.text
    assert r.json()["uploaded"] == 4
    lr = s.get(f"{BASE}/clients/{client_id}/files")
    assert lr.status_code == 200 and len(lr.json()) >= 4


# --------------- scan pipeline -------------
def test_scan_end_to_end(s, client_id):
    # pick Small Business template
    tpls = s.get(f"{BASE}/checklist-templates").json()
    sb = next(t for t in tpls if t["name"] == "Small Business Package")

    r = s.post(f"{BASE}/clients/{client_id}/scan",
               json={"template_id": sb["id"], "expected_period": 2024})
    assert r.status_code == 200, r.text
    scan_id = r.json()["id"]

    # poll
    completed = None
    for _ in range(40):
        time.sleep(1)
        g = s.get(f"{BASE}/scans/{scan_id}").json()
        if g["status"] in ("completed", "error"):
            completed = g
            break
    assert completed is not None, "Scan did not finish in time"
    assert completed["status"] == "completed", f"Scan errored: {completed}"
    assert completed["progress"] == 100
    counts = completed["counts"]
    assert counts.get("exact_duplicate", 0) >= 1
    assert counts.get("wrong_period", 0) >= 1
    assert counts.get("missing_doc", 0) >= 1
    assert counts.get("unreadable", 0) >= 1  # png image should trigger unreadable

    # findings endpoint
    fr = s.get(f"{BASE}/scans/{scan_id}/findings")
    assert fr.status_code == 200
    findings = fr.json()
    assert len(findings) >= 1
    fnd = findings[0]
    for k in ("category", "confidence", "confidence_level", "files", "evidence"):
        assert k in fnd

    # patch finding
    fid = fnd["id"]
    pr = s.patch(f"{BASE}/findings/{fid}", json={"status": "keep", "note": "ok"})
    assert pr.status_code == 200
    assert pr.json()["status"] == "keep"

    # report exports
    for fmt, ctype_frag in [("csv", "csv"), ("xlsx", "spreadsheetml"), ("pdf", "pdf")]:
        rp = s.get(f"{BASE}/scans/{scan_id}/report", params={"format": fmt})
        assert rp.status_code == 200, f"{fmt} failed: {rp.text}"
        assert ctype_frag in rp.headers.get("content-type", "").lower()
        assert len(rp.content) > 50, f"{fmt} report empty"


# --------------- soft delete file -----------
def test_file_soft_delete(s, client_id):
    lst = s.get(f"{BASE}/clients/{client_id}/files").json()
    if lst:
        fid = lst[0]["id"]
        d = s.delete(f"{BASE}/clients/{client_id}/files/{fid}")
        assert d.status_code == 200
        lst2 = s.get(f"{BASE}/clients/{client_id}/files").json()
        assert all(f["id"] != fid for f in lst2)


# ---------------- 404 handling --------------
def test_client_not_found(s):
    r = s.get(f"{BASE}/clients/nope-id")
    assert r.status_code == 404
    r = s.get(f"{BASE}/scans/nope-id")
    assert r.status_code == 404



# ---------------- cancel scan endpoint (NEW) ----------------
def test_cancel_scan_404():
    s2 = requests.Session()
    r = s2.post(f"{BASE}/scans/does-not-exist-xyz/cancel")
    assert r.status_code == 404


def test_cancel_scan_on_live_scan(s, client_id):
    # upload a small file so scan has something to do
    csv_bytes = b"date,amount\n2024-01-01,1\n"
    _upload(s, client_id, [("files", ("cancel_test.csv", io.BytesIO(csv_bytes), "text/csv"))])
    tpls = s.get(f"{BASE}/checklist-templates").json()
    sb = next(t for t in tpls if t["name"] == "Small Business Package")
    r = s.post(f"{BASE}/clients/{client_id}/scan",
               json={"template_id": sb["id"], "expected_period": 2024})
    assert r.status_code == 200
    scan_id = r.json()["id"]

    # immediately cancel
    cr = s.post(f"{BASE}/scans/{scan_id}/cancel")
    assert cr.status_code == 200
    body = cr.json()
    assert body.get("ok") is True

    # poll to final state
    final = None
    for _ in range(30):
        time.sleep(1)
        g = s.get(f"{BASE}/scans/{scan_id}").json()
        if g["status"] in ("completed", "cancelled", "error"):
            final = g
            break
    assert final is not None, "Scan did not reach terminal state"
    # Either final state acceptable per spec; must not be 'error'
    assert final["status"] in ("completed", "cancelled"), f"Bad terminal status: {final}"

