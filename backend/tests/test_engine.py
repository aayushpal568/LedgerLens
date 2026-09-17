"""Engine unit tests using sample documents.

Covers the reusable core and the pluggable adapters (FileSource, extractor,
OCRProvider, LLMProvider, ScanEngine) so the future local Windows build can
rely on the same logic. No web/DB dependency here.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engine import (  # noqa: E402
    FileRef, ExtractionResult, ScanEngine, build_default_engine, run_detection,
    LocalPathFileSource, LocalDirectoryFileSource, DefaultDocumentExtractor,
    NoOpOCRProvider, NoOpLLMProvider, default_templates,
)
from engine.interfaces import OCRProvider  # noqa: E402
from engine.models import STATUS_OK, STATUS_NEEDS_OCR  # noqa: E402


# ----------------------------- fixtures -----------------------------
@pytest.fixture
def sample_dir(tmp_path):
    """Build a small nested folder of sample accounting documents."""
    (tmp_path / "sub").mkdir()

    csv1 = tmp_path / "bank_statement_2024.csv"
    csv1.write_text("Date,Amount,Vendor\n2024-01-01,100,Chase Bank Statement\n")
    # byte-identical copy -> exact duplicate
    (tmp_path / "sub" / "bank_copy.csv").write_bytes(csv1.read_bytes())

    # wrong-period doc (2022 vs expected 2024), also matches "Profit and Loss"
    (tmp_path / "profit_and_loss_2022.csv").write_text("Profit and Loss 2022\nRevenue,5000\nExpenses,3000\n")

    # xlsx balance sheet
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["Balance Sheet 2024"]); ws.append(["Assets", 1000]); ws.append(["Liabilities", 400])
    wb.save(tmp_path / "balance_sheet.xlsx")

    # fake image -> needs OCR / unreadable
    (tmp_path / "scanned_receipt.png").write_bytes(b"\x89PNG\r\n\x1a\n" + os.urandom(64))

    # unsupported file (should be filtered when supported_only=True)
    (tmp_path / "notes.txt").write_text("just a note")

    return tmp_path


def _sb_template():
    return [t for t in default_templates() if t["client_type"] == "Small Business"][0]


# --------------------------- FileSource ----------------------------
def test_local_directory_source_lists_nested(sample_dir):
    src = LocalDirectoryFileSource(str(sample_dir))
    names = {r.name for r in src.list_files()}
    assert "bank_statement_2024.csv" in names
    assert "bank_copy.csv" in names  # nested subfolder found
    assert "notes.txt" in names      # unsupported included by default


def test_local_directory_source_supported_only(sample_dir):
    src = LocalDirectoryFileSource(str(sample_dir), supported_only=True)
    exts = {r.ext for r in src.list_files()}
    assert "txt" not in exts
    assert "csv" in exts and "xlsx" in exts


def test_file_source_open_returns_path(sample_dir):
    src = LocalDirectoryFileSource(str(sample_dir))
    ref = next(r for r in src.list_files() if r.name == "bank_statement_2024.csv")
    assert os.path.isfile(src.open(ref))


# ------------------------ DocumentExtractor ------------------------
def test_extractor_reads_csv(sample_dir):
    ex = DefaultDocumentExtractor(NoOpOCRProvider())
    res = ex.extract(str(sample_dir / "bank_statement_2024.csv"), "csv")
    assert res.status == STATUS_OK and "Chase" in res.text


def test_extractor_image_needs_ocr_without_provider(sample_dir):
    ex = DefaultDocumentExtractor(NoOpOCRProvider())
    res = ex.extract(str(sample_dir / "scanned_receipt.png"), "png")
    assert res.status == STATUS_NEEDS_OCR and res.ocr_used is False


def test_pluggable_ocr_provider_activates(sample_dir):
    class FakeOCR(OCRProvider):
        name = "fake"
        @property
        def available(self):
            return True
        def extract(self, path, ext):
            return "RECEIPT TOTAL 42.00"

    ex = DefaultDocumentExtractor(FakeOCR())
    res = ex.extract(str(sample_dir / "scanned_receipt.png"), "png")
    assert res.status == STATUS_OK and res.ocr_used is True and "RECEIPT" in res.text


# ----------------------------- OCR/LLM -----------------------------
def test_noop_providers_inactive():
    assert NoOpOCRProvider().available is False
    llm = NoOpLLMProvider()
    assert llm.available is False
    assert llm.classify_document("x", ["a", "b"]) is None
    assert llm.extract_field("x", "date") is None


# ---------------------------- ScanEngine ---------------------------
def test_scan_engine_over_local_directory(sample_dir):
    engine = build_default_engine()
    src = LocalDirectoryFileSource(str(sample_dir), supported_only=True)
    result = engine.run(src, _sb_template()["items"], expected_period=2024)
    counts = result["counts"]
    assert counts["exact_duplicate"] >= 1
    assert counts["wrong_period"] >= 1     # profit_and_loss_2022
    assert counts["missing_doc"] >= 1      # e.g. Payroll Report not present
    assert counts["unreadable"] >= 1       # scanned_receipt.png


def test_scan_engine_individual_checks_reusable(sample_dir):
    engine = build_default_engine()
    src = LocalDirectoryFileSource(str(sample_dir), supported_only=True)
    # Access a single check directly to prove reusability
    refs = src.list_files()
    files = []
    from engine.hashing import sha256_of_file
    for r in refs:
        files.append({"id": r.id, "name": r.name, "ext": r.ext, "size": r.size,
                      "sha256": sha256_of_file(r.path)})
    dup = engine._exact_duplicates(files)
    assert any(f["category"] == "exact_duplicate" for f in dup)


# ------------------- backward-compat run_detection -----------------
def test_run_detection_backward_compatible(sample_dir):
    records = []
    for name in ["bank_statement_2024.csv", "profit_and_loss_2022.csv", "balance_sheet.xlsx"]:
        p = str(sample_dir / name)
        records.append({"id": name, "name": name, "ext": name.rsplit(".", 1)[-1],
                        "size": os.path.getsize(p), "path": p})
    out = run_detection(records, _sb_template()["items"], expected_period=2024)
    assert {"findings", "counts", "skipped", "processed"}.issubset(out.keys())
    assert out["processed"] == 3
    assert out["counts"]["wrong_period"] >= 1


def test_locked_file_is_skipped_safely():
    engine = build_default_engine()
    # point at a non-existent path -> open() raises -> safely skipped
    src = LocalPathFileSource([{"id": "x", "name": "missing.pdf", "ext": "pdf", "size": 10, "path": "/no/such/file.pdf"}])
    out = engine.run(src, [], None)
    assert out["processed"] == 1
    assert len(out["skipped"]) == 1
    assert out["skipped"][0]["reason"] == "Locked or inaccessible file"
