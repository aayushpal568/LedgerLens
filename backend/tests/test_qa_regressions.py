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
    monkeypatch.setenv("DATA_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "api.db"))
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    with TestClient(server.app) as client:
        yield client, server


def test_missing_finding_update_returns_404(api_client):
    client, _server = api_client
    response = client.patch("/api/findings/missing", json={"status": "keep"})
    assert response.status_code == 404


def test_invalid_review_status_is_rejected(api_client):
    client, server = api_client
    finding = {"id": "finding-1", "scan_id": "scan-1", "status": "unreviewed"}
    import asyncio
    asyncio.run(server.db.findings.insert_one(finding))
    response = client.patch("/api/findings/finding-1", json={"status": "delete_files"})
    assert response.status_code == 422


def test_local_mode_upload_cannot_egress(api_client, monkeypatch):
    client, server = api_client
    called = {"value": False}

    def forbidden_upload(*_args, **_kwargs):
        called["value"] = True
        raise AssertionError("external storage must not be called in local mode")

    monkeypatch.setattr(server.storage, "put_object", forbidden_upload)
    response = client.post(
        "/api/clients/client-1/files",
        files={"files": ("synthetic.csv", b"date,amount\n2024-01-01,1\n", "text/csv")},
    )
    assert response.status_code == 400
    assert called["value"] is False


def test_resume_rejects_scan_from_another_client(api_client, tmp_path):
    client, server = api_client
    import asyncio
    asyncio.run(server.db.clients.insert_one({"id": "client-a", "name": "A"}))
    asyncio.run(server.db.clients.insert_one({"id": "client-b", "name": "B"}))
    asyncio.run(server.db.scans.insert_one({
        "id": "prior", "client_id": "client-a", "source_type": "local",
        "folder_path": str(tmp_path), "file_states": {},
    }))

    response = client.post(
        "/api/clients/client-b/scan-local",
        json={"folder_path": str(tmp_path), "resume_scan_id": "prior"},
    )
    assert response.status_code == 400
