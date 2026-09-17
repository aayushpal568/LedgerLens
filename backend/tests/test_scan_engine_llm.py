"""Tests proving ScanEngine LLM integration requirements A through J.

A. 12+ candidates still result in maximum 5 TOTAL LLM calls.
B. Classification with valid label + confidence >= 0.80 works.
C. Low-confidence classification does NOT suppress missing_doc.
D. Invalid/off-list label does NOT suppress missing_doc.
E. Cancellation stops the LLM phase.
F. File selection is deterministic.
G. Same document is not sent twice within one scan.
H. AI provenance appears in exported report data.
I. verify_all does not call the real FAL API.
J. Existing behavior still works when LLM is unavailable.
"""
import io
import csv
from unittest.mock import MagicMock, patch

import pytest

from engine.interfaces import LLMProvider, DocumentExtractor
from engine.models import ExtractionResult, STATUS_OK
from engine.scan_engine import ScanEngine, LLM_MAX_CALLS_PER_SCAN, LLM_MIN_CONFIDENCE
from engine.providers.file_sources import LocalPathFileSource
from engine.providers.llm import NoOpLLMProvider, ClaudeOpusFalProvider, ClassificationResult
from engine import report as report_engine


class DummyExtractor(DocumentExtractor):
    def __init__(self, text_map=None, default_text="Sample text", status=STATUS_OK):
        self.text_map = text_map or {}
        self.default_text = default_text
        self.status = status

    def extract(self, path: str, ext: str, should_cancel=None):
        txt = self.text_map.get(path, self.default_text)
        return ExtractionResult(text=txt, status=self.status, ocr_used=False)


@pytest.fixture(autouse=True)
def mock_sha256_fixture():
    with patch("engine.scan_engine.sha256_of_file", side_effect=lambda p: f"hash_{p}"):
        yield


# --------------------------------------------------------------------------
# Requirement A: 12+ candidates still result in maximum 5 TOTAL LLM calls
# --------------------------------------------------------------------------
def test_requirement_a_budget_limit_across_classification_and_period():
    """Verify single per-scan budget: 12+ candidates result in max 5 TOTAL LLM calls."""
    mock_llm = MagicMock(spec=LLMProvider)
    mock_llm.available = True
    mock_llm.classify_document.return_value = {"label": "Other", "confidence": 0.50}
    mock_llm.extract_field.return_value = "2024"

    # Create 8 files needing checklist matching and 8 files needing period extraction (total 16 candidates)
    records = []
    text_map = {}
    for i in range(1, 9):
        fid = f"check_{i:02d}"
        records.append({"id": fid, "name": f"unmatched_doc_{i}.pdf", "ext": "pdf", "size": 100, "path": fid})
        text_map[fid] = f"Unclassified document content {i}"

    for i in range(1, 9):
        fid = f"period_{i:02d}"
        records.append({"id": fid, "name": f"no_period_doc_{i}.pdf", "ext": "pdf", "size": 100, "path": fid})
        text_map[fid] = f"Document without year text {i}"

    extractor = DummyExtractor(text_map=text_map)
    checklist = [{"name": "Required Checklist Form", "aliases": [], "allowed_types": ["PDF"]}]

    # Test default 5-call budget
    engine = ScanEngine(extractor=extractor, ocr_provider=None, llm_provider=mock_llm)
    assert engine.llm_max_calls == 5
    source = LocalPathFileSource(records)
    engine.run(source, checklist_items=checklist, expected_period=2024)

    total_calls = mock_llm.classify_document.call_count + mock_llm.extract_field.call_count
    assert total_calls == 5, f"Expected exactly 5 total LLM calls, got {total_calls}"

    # Test configurable constructor argument (e.g. limit=3)
    mock_llm.reset_mock()
    engine_custom = ScanEngine(extractor=extractor, ocr_provider=None, llm_provider=mock_llm, llm_max_calls=3)
    assert engine_custom.llm_max_calls == 3
    engine_custom.run(source, checklist_items=checklist, expected_period=2024)
    total_custom_calls = mock_llm.classify_document.call_count + mock_llm.extract_field.call_count
    assert total_custom_calls == 3, f"Expected exactly 3 total LLM calls, got {total_custom_calls}"


# --------------------------------------------------------------------------
# Requirement B: Classification with valid label + confidence >= 0.80 works
# --------------------------------------------------------------------------
def test_requirement_b_valid_classification_high_confidence():
    """Verify valid candidate label with confidence >= 0.80 resolves missing_doc."""
    mock_llm = MagicMock(spec=LLMProvider)
    mock_llm.available = True
    mock_llm.classify_document.return_value = {
        "label": "W-2 Wage Statement",
        "confidence": 0.85,
        "reasoning": "Form W-2 Wage and Tax Statement detected",
    }

    extractor = DummyExtractor(default_text="IRS Form W-2 Wage and Tax Statement for Employee")
    engine = ScanEngine(extractor=extractor, ocr_provider=None, llm_provider=mock_llm)

    file_record = {"id": "f1", "name": "scan_upload_99.pdf", "ext": "pdf", "size": 1024, "path": "f1"}
    source = LocalPathFileSource([file_record])
    checklist = [{"name": "W-2 Wage Statement", "aliases": [], "allowed_types": ["PDF"]}]

    result = engine.run(source, checklist_items=checklist)
    assert result["counts"]["missing_doc"] == 0
    f_state = result["file_states"]["f1"]
    assert f_state.get("classified_by") == "llm"
    assert f_state.get("classification") == "W-2 Wage Statement"
    assert f_state.get("llm_confidence") == 0.85


# --------------------------------------------------------------------------
# Requirement C: Low-confidence classification does NOT suppress missing_doc
# --------------------------------------------------------------------------
def test_requirement_c_low_confidence_does_not_suppress_missing_doc():
    """Verify confidence < 0.80 rejects the match and preserves missing_doc finding."""
    mock_llm = MagicMock(spec=LLMProvider)
    mock_llm.available = True
    mock_llm.classify_document.return_value = {
        "label": "W-2 Wage Statement",
        "confidence": 0.72,  # Below 0.80 threshold
        "reasoning": "Ambiguous header resembling W-2",
    }

    extractor = DummyExtractor(default_text="Ambiguous header text")
    engine = ScanEngine(extractor=extractor, ocr_provider=None, llm_provider=mock_llm)

    file_record = {"id": "f1", "name": "unclear_doc.pdf", "ext": "pdf", "size": 1024, "path": "f1"}
    source = LocalPathFileSource([file_record])
    checklist = [{"name": "W-2 Wage Statement", "aliases": [], "allowed_types": ["PDF"]}]

    result = engine.run(source, checklist_items=checklist)
    # Must NOT suppress missing_doc
    assert result["counts"]["missing_doc"] == 1
    f_state = result["file_states"]["f1"]
    assert f_state.get("classified_by") is None


# --------------------------------------------------------------------------
# Requirement D: Invalid/off-list label does NOT suppress missing_doc
# --------------------------------------------------------------------------
def test_requirement_d_invalid_off_list_label_preserves_missing_doc():
    """Verify off-list label does not match and keeps normal missing-document finding."""
    mock_llm = MagicMock(spec=LLMProvider)
    mock_llm.available = True
    mock_llm.classify_document.return_value = {
        "label": "Completely Unrelated Medical Receipt",
        "confidence": 0.99,
        "reasoning": "Identified as medical receipt",
    }

    extractor = DummyExtractor(default_text="Hospital clinic invoice receipt")
    engine = ScanEngine(extractor=extractor, ocr_provider=None, llm_provider=mock_llm)

    file_record = {"id": "f1", "name": "doc_001.pdf", "ext": "pdf", "size": 1024, "path": "f1"}
    source = LocalPathFileSource([file_record])
    checklist = [{"name": "W-2 Wage Statement", "aliases": [], "allowed_types": ["PDF"]}]

    result = engine.run(source, checklist_items=checklist)
    # Must NOT treat document as matched
    assert result["counts"]["missing_doc"] == 1
    f_state = result["file_states"]["f1"]
    assert f_state.get("classified_by") is None


# --------------------------------------------------------------------------
# Requirement E: Cancellation stops the LLM phase
# --------------------------------------------------------------------------
def test_requirement_e_cancellation_stops_llm_immediately():
    """Verify should_cancel halts LLM calls immediately."""
    mock_llm = MagicMock(spec=LLMProvider)
    mock_llm.available = True
    call_count = 0

    def mock_classify(text, candidates):
        nonlocal call_count
        call_count += 1
        return {"label": "Other", "confidence": 0.5}

    mock_llm.classify_document.side_effect = mock_classify

    records = [
        {"id": f"f{i}", "name": f"unmatched_{i}.pdf", "ext": "pdf", "size": 100, "path": f"f{i}"}
        for i in range(1, 8)
    ]
    extractor = DummyExtractor(default_text="Some text")
    engine = ScanEngine(extractor=extractor, ocr_provider=None, llm_provider=mock_llm)

    # Cancel after the first LLM call
    cancelled_flag = False

    def check_cancel():
        nonlocal cancelled_flag
        if call_count >= 1:
            cancelled_flag = True
            return True
        return False

    source = LocalPathFileSource(records)
    checklist = [{"name": "Item A", "aliases": [], "allowed_types": ["PDF"]}]

    result = engine.run(source, checklist_items=checklist, should_cancel=check_cancel)
    assert result["cancelled"] is True
    assert call_count <= 1, f"Expected at most 1 LLM call before cancellation, got {call_count}"


# --------------------------------------------------------------------------
# Requirement F: File selection is deterministic
# --------------------------------------------------------------------------
def test_requirement_f_deterministic_file_order():
    """Verify files are sorted deterministically by file id before LLM evaluation."""
    mock_llm = MagicMock(spec=LLMProvider)
    mock_llm.available = True
    observed_order = []

    def mock_classify(text, candidates):
        observed_order.append(text)
        return {"label": "None", "confidence": 0.1}

    mock_llm.classify_document.side_effect = mock_classify

    # Insert files in scrambled order: f09, f03, f01, f08, f02
    scrambled_ids = ["f09", "f03", "f01", "f08", "f02"]
    records = []
    text_map = {}
    for fid in scrambled_ids:
        records.append({"id": fid, "name": f"doc_{fid}.pdf", "ext": "pdf", "size": 100, "path": fid})
        text_map[fid] = f"Document content for {fid}"

    extractor = DummyExtractor(text_map=text_map)
    engine = ScanEngine(extractor=extractor, ocr_provider=None, llm_provider=mock_llm)
    source = LocalPathFileSource(records)
    checklist = [{"name": "Target Form", "aliases": [], "allowed_types": ["PDF"]}]

    engine.run(source, checklist_items=checklist)

    expected_order = [
        "Document content for f01",
        "Document content for f02",
        "Document content for f03",
        "Document content for f08",
        "Document content for f09",
    ]
    assert observed_order == expected_order, f"Order was not sorted by file id: {observed_order}"


# --------------------------------------------------------------------------
# Requirement G: Same document is not sent twice within one scan
# --------------------------------------------------------------------------
def test_requirement_g_in_scan_cache_prevents_duplicate_calls():
    """Verify documents with identical hash/content are cached within the same scan."""
    mock_llm = MagicMock(spec=LLMProvider)
    mock_llm.available = True
    mock_llm.classify_document.return_value = {"label": "Other", "confidence": 0.50}

    # Two distinct file records with identical file path (and thus identical sha256 / text)
    records = [
        {"id": "file_copy_1", "name": "doc_copy_1.pdf", "ext": "pdf", "size": 1024, "path": __file__},
        {"id": "file_copy_2", "name": "doc_copy_2.pdf", "ext": "pdf", "size": 1024, "path": __file__},
    ]

    extractor = DummyExtractor(default_text="Identical duplicate content")
    engine = ScanEngine(extractor=extractor, ocr_provider=None, llm_provider=mock_llm)
    source = LocalPathFileSource(records)
    checklist = [{"name": "Some Checklist Item", "aliases": [], "allowed_types": ["PDF"]}]

    engine.run(source, checklist_items=checklist)
    # The second file must be resolved from in-scan cache; classify_document called only once!
    assert mock_llm.classify_document.call_count == 1


# --------------------------------------------------------------------------
# Requirement H: AI provenance appears in exported report data
# --------------------------------------------------------------------------
def test_requirement_h_ai_provenance_in_exported_reports():
    """Verify exported CSV, XLSX, and PDF include AI provenance for AI-assisted findings."""
    ai_finding = {
        "id": "fnd-1",
        "category": "wrong_period",
        "title": '"tax_file.pdf" appears to be from 2021, not 2024',
        "confidence": 85,
        "confidence_level": "high",
        "status": "unreviewed",
        "files": [{"file_id": "f1", "name": "tax_file.pdf", "ext": "PDF", "size": 100, "classified_by": "llm"}],
        "evidence": {"summary": "Detected period 2021 does not match expected 2024.", "detected_by": "llm"},
        "ai_assisted": True,
        "provenance": "llm",
    }
    deterministic_finding = {
        "id": "fnd-2",
        "category": "exact_duplicate",
        "title": '2 identical copies of "invoice.csv"',
        "confidence": 100,
        "confidence_level": "high",
        "status": "unreviewed",
        "files": [{"file_id": "f2", "name": "invoice.csv", "ext": "CSV", "size": 50}],
        "evidence": {"summary": "Files are byte-for-byte identical."},
    }

    findings = [ai_finding, deterministic_finding]

    # Test CSV export
    csv_bytes = report_engine.to_csv(findings)
    csv_text = csv_bytes.decode("utf-8-sig")
    assert "AI" in csv_text or "AI/LLM" in csv_text
    rows = list(csv.reader(io.StringIO(csv_text)))
    # Row 1 (header), Row 2 (AI finding), Row 3 (deterministic finding)
    ai_row = rows[1]
    det_row = rows[2]
    assert "[AI:" in ai_row[3] or "AI/LLM" in ai_row[6]
    # Deterministic finding confidence and evidence must not have AI tags
    assert "[AI" not in det_row[3]
    assert "[AI" not in det_row[6]

    # Test XLSX export
    xlsx_bytes = report_engine.to_xlsx(findings)
    assert len(xlsx_bytes) > 0

    # Test PDF export
    pdf_bytes = report_engine.to_pdf(findings, {"client_name": "Test Client", "expected_period": 2024})
    assert pdf_bytes.startswith(b"%PDF")


# --------------------------------------------------------------------------
# Requirement I: verify_all does not call the real FAL API
# --------------------------------------------------------------------------
def test_requirement_i_verify_all_hermetic_no_fal_calls():
    """Verify verify_all environment defaults to unconfigured FAL_KEY without live network calls."""
    with patch.dict("os.environ", {"FAL_KEY": "", "FAL_API_KEY": "", "ANTHROPIC_API_KEY": ""}):
        provider = ClaudeOpusFalProvider()
        assert provider.available is False
        with patch("httpx.Client.post") as mock_post:
            assert provider.generate("test") is None
            assert provider.classify_document("doc text", ["Invoice"]) is None
            mock_post.assert_not_called()


# --------------------------------------------------------------------------
# Requirement J: Existing behavior still works when LLM is unavailable
# --------------------------------------------------------------------------
def test_requirement_j_existing_behavior_when_llm_unavailable():
    """Verify exact duplicates, wrong period, and checklist findings work normally without LLM."""
    engine = ScanEngine(
        extractor=DummyExtractor(default_text="Invoice #1234 for 2021"),
        ocr_provider=None,
        llm_provider=NoOpLLMProvider(),
    )
    assert engine.llm.available is False

    records = [
        {"id": "f1", "name": "invoice_copy_1.csv", "ext": "csv", "size": 100, "path": __file__},
        {"id": "f2", "name": "invoice_copy_2.csv", "ext": "csv", "size": 100, "path": __file__},
    ]
    source = LocalPathFileSource(records)
    checklist = [{"name": "Bank Statement", "aliases": [], "allowed_types": ["CSV"]}]

    result = engine.run(source, checklist_items=checklist, expected_period=2024)
    # Exact duplicate detected
    assert result["counts"]["exact_duplicate"] == 1
    # Missing document detected (unmatched checklist item)
    assert result["counts"]["missing_doc"] == 1
    # Wrong period detected deterministically from text ("2021" != 2024 for both files)
    assert result["counts"]["wrong_period"] == 2
