"""Regression tests for independently reproduced LedgerLens QA defects.

All filesystem data is synthetic and isolated under pytest temporary folders.
"""
import csv
import importlib
import io
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engine.checklist import detect_period  # noqa: E402
from engine import report as report_engine  # noqa: E402


def test_period_detection_accepts_underscores_and_prefers_filename():
    period = detect_period(
        "profit_and_loss_2024.csv",
        "Comparative prior year 2023. Current reporting period 2024.",
    )
    assert period["year"] == 2024


def test_period_detection_ignores_identifier_serials_in_body():
    """Regression: an internal serial like 'S-2001' must not be read as the year.

    A sales register whose only pre-2024 four-digit run is an invoice serial
    (S-2001/S-2002) but whose real dates are all 2024 must resolve to 2024,
    not 2001 (which previously produced a false wrong-period exception).
    """
    body = (
        "Invoice No Customer Date Amount S-2001 Retail One 2024-01-10 10000 "
        "S-2002 Retail Two 2024-01-18 7500 S-2003 Retail Three 2024-02-05 12000"
    )
    # Filename carries no year, so the body heuristic decides.
    assert detect_period("sales_register.xlsx", body)["year"] == 2024
    # Even with no calendar date, a bare year glued to a lettered prefix (an
    # identifier) is ignored; a genuinely free-standing year is still honored.
    assert detect_period("misc.pdf", "Ref MP-2001 MP-2002 totals for 2019")["year"] == 2019


def test_scan_does_not_flag_serial_number_file_as_wrong_period(tmp_path):
    """End-to-end regression through ScanEngine (noop providers, no OCR/LLM)."""
    from engine.scan_engine import ScanEngine
    from engine.providers import (
        DefaultDocumentExtractor, NoOpOCRProvider, NoOpLLMProvider, LocalPathFileSource,
    )

    csv_path = tmp_path / "sales_register.csv"
    csv_path.write_text(
        "Invoice No,Customer,Date,Amount\n"
        "S-2001,Retail One,2024-01-10,10000\n"
        "S-2002,Retail Two,2024-01-18,7500\n",
        encoding="utf-8",
    )
    old_path = tmp_path / "old_invoice.csv"
    old_path.write_text(
        "Invoice No,Customer,Date,Amount\nOLD-5,Retail Five,2019-03-12,900\n",
        encoding="utf-8",
    )
    records = [
        {"id": "r-1", "name": csv_path.name, "ext": "csv", "size": csv_path.stat().st_size, "path": str(csv_path)},
        {"id": "r-2", "name": old_path.name, "ext": "csv", "size": old_path.stat().st_size, "path": str(old_path)},
    ]

    ocr = NoOpOCRProvider()
    engine = ScanEngine(DefaultDocumentExtractor(ocr), ocr, NoOpLLMProvider())
    result = engine.run(LocalPathFileSource(records), checklist_items=[], expected_period=2024)

    wrong = [f for f in result["findings"] if f["category"] == "wrong_period"]
    flagged_names = {w["files"][0]["name"] for w in wrong}
    # The 2024 register must NOT be flagged; the real 2019 invoice must be.
    assert "sales_register.csv" not in flagged_names
    assert flagged_names == {"old_invoice.csv"}
    assert wrong[0]["evidence"]["detected_year"] == 2019


def test_reports_escape_markup_and_spreadsheet_formulas():
    from openpyxl import load_workbook

    findings = [{
        "category": "unreadable",
        "title": '=HYPERLINK("http://example.invalid","synthetic")',
        "files": [{"name": "+synthetic.csv"}],
        "confidence": 90,
        "confidence_level": "high",
        "status": "unreviewed",
        "note": "<synthetic & note",
        "evidence": {"summary": "Synthetic > evidence"},
    }]

    csv_rows = list(csv.reader(io.StringIO(report_engine.to_csv(findings).decode("utf-8-sig"))))
    assert csv_rows[1][1].startswith("'=")
    assert csv_rows[1][2].startswith("'+")

    workbook = load_workbook(io.BytesIO(report_engine.to_xlsx(findings)), data_only=False)
    assert workbook.active["B2"].data_type == "s"
    assert workbook.active["B2"].value.startswith("'=")

    pdf = report_engine.to_pdf(findings, {"client_name": "A < B & C", "expected_period": 2024})
    assert pdf.startswith(b"%PDF")


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_BACKEND", "memory")
    monkeypatch.setenv("AUTH_SECRET_KEY", "test-secret-key-at-least-32-characters-long")
    import server
    import database
    server.db = database.get_database(None, "memory", reset=True)
    with TestClient(server.app) as client:
        # Signup to obtain auth token
        res = client.post("/api/auth/signup", json={
            "email": "qa@example.com",
            "password": "Password123!",
            "firm_name": "QA Firm",
            "user_name": "QA User"
        })
        token = res.json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, server



def test_missing_finding_update_returns_404(api_client):
    client, _server = api_client
    response = client.patch("/api/findings/missing", json={"status": "keep"})
    assert response.status_code == 404


def test_invalid_review_status_is_rejected(api_client):
    client, server = api_client
    import asyncio
    user = asyncio.run(server.db.users.find_one({"email": "qa@example.com"}))
    finding = {"id": "finding-1", "scan_id": "scan-1", "firm_id": user["firm_id"], "status": "unreviewed"}
    asyncio.run(server.db.findings.insert_one(finding))
    response = client.patch("/api/findings/finding-1", json={"status": "delete_files"})
    assert response.status_code == 422

