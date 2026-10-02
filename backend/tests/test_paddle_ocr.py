import io
import os
import sys
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import pytest
from unittest.mock import patch, MagicMock
from PIL import Image, ImageDraw
import cv2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engine.interfaces import OCRProvider  # noqa: E402
from engine.models import STATUS_OK, STATUS_NEEDS_OCR  # noqa: E402
from engine.providers import (  # noqa: E402
    PaddleOCRProvider,
    NoOpOCRProvider,
    DefaultDocumentExtractor,
    LocalPathFileSource,
)
from engine.scan_engine import build_default_engine, ScanEngine  # noqa: E402


@pytest.fixture
def sample_image(tmp_path):
    """Generate a real image with simulated invoice content."""
    img_path = tmp_path / "scanned_invoice.png"
    img = Image.new("RGB", (400, 200), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((20, 30), "INVOICE #INV-2024-9988", fill=(0, 0, 0))
    draw.text((20, 60), "Date: 2024-06-15", fill=(0, 0, 0))
    draw.text((20, 90), "Total: $3,450.00", fill=(0, 0, 0))
    img.save(img_path, format="PNG")
    return str(img_path)


@pytest.fixture
def sample_scanned_pdf(tmp_path, sample_image):
    """Generate a real scanned (image-only) 2-page PDF."""
    import fitz
    pdf_path = tmp_path / "scanned_document.pdf"
    doc = fitz.open()
    page1 = doc.new_page(width=400, height=200)
    page1.insert_image(fitz.Rect(0, 0, 400, 200), filename=sample_image)
    page2 = doc.new_page(width=400, height=200)
    page2.insert_image(fitz.Rect(0, 0, 400, 200), filename=sample_image)
    doc.save(str(pdf_path))
    doc.close()
    return str(pdf_path)


# ---------------- 1. Initialization & Config Handling ----------------
def test_paddle_ocr_initialization_defaults():
    """Verify provider initialization and safe CPU defaults."""
    provider = PaddleOCRProvider()
    assert provider.name == "paddle_ocr"
    assert provider.available is True
    summary = provider.get_config_summary()
    assert summary["provider"] == "paddle_ocr"
    assert summary["available"] == "True"
    assert summary["mode"] == "local"
    assert summary["use_gpu"] == "False"
    assert summary["lang"] == "en"
    assert summary["use_angle_cls"] == "True"


def test_paddle_ocr_configuration_from_env():
    """Verify provider discovers GPU and language configuration from env."""
    env = {
        "PADDLE_OCR_USE_GPU": "true",
        "PADDLE_OCR_LANG": "ch",
        "PADDLE_OCR_USE_ANGLE_CLS": "false",
    }
    with patch.dict(os.environ, env, clear=False):
        provider = PaddleOCRProvider()
        summary = provider.get_config_summary()
        assert summary["use_gpu"] == "True"
        assert summary["lang"] == "ch"
        assert summary["use_angle_cls"] == "False"


# ---------------- 2. Graceful Failure When Unavailable ----------------
def test_paddle_ocr_graceful_when_unavailable(sample_image):
    """If PaddleOCR is unavailable, extract returns None without raising."""
    provider = PaddleOCRProvider()
    provider._available_cache = False
    assert provider.available is False

    res = provider.extract(sample_image, "png")
    assert res is None

    extractor = DefaultDocumentExtractor(provider)
    extracted = extractor.extract(sample_image, "png")
    assert extracted.status == STATUS_NEEDS_OCR
    assert extracted.ocr_used is False
    assert "OCR is not configured" in extracted.reason
    assert "paddle_ocr" in extracted.reason


# ---------------- 3. Mocked Image & PDF Extraction ----------------
def test_paddle_ocr_extract_image_mocked(sample_image):
    """Test OCR extraction on an image with mocked OCR engine."""
    mock_ocr = MagicMock()
    mock_ocr.ocr.return_value = [
        [
            [[[20, 30], [200, 30], [200, 50], [20, 50]], ("INVOICE #INV-2024-9988", 0.99)],
            [[[20, 90], [200, 90], [200, 110], [20, 110]], ("Total: $3,450.00", 0.98)],
        ]
    ]

    provider = PaddleOCRProvider()
    with patch.object(provider, "_get_ocr", return_value=mock_ocr):
        text = provider.extract(sample_image, "png")
        assert text is not None
        assert "INVOICE #INV-2024-9988" in text
        assert "Total: $3,450.00" in text


def test_paddle_ocr_extract_scanned_pdf_sequential(sample_scanned_pdf):
    """Test sequential one-page-at-a-time PDF processing preserving page numbers."""
    mock_ocr = MagicMock()
    mock_ocr.ocr.side_effect = [
        [[[[[0, 0], [10, 0], [10, 10], [0, 10]], ("Page 1 Content", 0.95)]]],
        [[[[[0, 0], [10, 0], [10, 10], [0, 10]], ("Page 2 Content", 0.96)]]],
    ]

    provider = PaddleOCRProvider()
    with patch.object(provider, "_get_ocr", return_value=mock_ocr):
        text = provider.extract(sample_scanned_pdf, "pdf")
        assert text is not None
        assert "--- Page 1 ---" in text
        assert "Page 1 Content" in text
        assert "--- Page 2 ---" in text
        assert "Page 2 Content" in text
        assert mock_ocr.ocr.call_count == 2


def test_paddle_ocr_pdf_preserves_earlier_pages_on_failure(sample_scanned_pdf):
    """If page 2 fails, page 1 text is preserved rather than failing the whole PDF."""
    mock_ocr = MagicMock()
    mock_ocr.ocr.side_effect = [
        [[[[[0, 0], [10, 0], [10, 10], [0, 10]], ("Page 1 Content", 0.95)]]],
        RuntimeError("Simulated failure on page 2"),
    ]

    provider = PaddleOCRProvider()
    with patch.object(provider, "_get_ocr", return_value=mock_ocr):
        text = provider.extract(sample_scanned_pdf, "pdf")
        assert text is not None
        assert "--- Page 1 ---" in text
        assert "Page 1 Content" in text
        assert "--- Page 2 ---" not in text


def test_paddle_ocr_cancellation(sample_scanned_pdf):
    """Cancellation stops processing before next page."""
    mock_ocr = MagicMock()
    provider = PaddleOCRProvider()
    with patch.object(provider, "_get_ocr", return_value=mock_ocr):
        text = provider.extract(sample_scanned_pdf, "pdf", should_cancel=lambda: True)
        assert text is None
        assert mock_ocr.ocr.call_count == 0


# ---------------- 4. Pipeline Integration ----------------
def test_ocr_text_reaches_pipeline_and_triggers_detections(sample_image):
    """Verify OCR text flows into ScanEngine and triggers wrong_period detection."""
    mock_ocr = MagicMock()
    mock_ocr.ocr.return_value = [
        [
            [[[0, 0], [10, 0], [10, 10], [0, 10]], ("Profit and Loss Statement 2021", 0.98)],
            [[[0, 0], [10, 0], [10, 10], [0, 10]], ("Gross Revenue: $500,000", 0.95)],
        ]
    ]

    provider = PaddleOCRProvider()
    with patch.object(provider, "_get_ocr", return_value=mock_ocr):
        engine = build_default_engine(ocr_provider=provider)
        file_records = [
            {
                "id": "file-ocr-1",
                "name": "receipt_scan.png",
                "ext": "png",
                "size": os.path.getsize(sample_image),
                "path": sample_image,
            }
        ]
        source = LocalPathFileSource(file_records)
        result = engine.run(source, checklist_items=[], expected_period=2024)

        assert result["processed"] == 1
        assert result["counts"]["wrong_period"] == 1
        finding = next(f for f in result["findings"] if f["category"] == "wrong_period")
        assert finding["evidence"]["detected_year"] == 2021


# ---------------- 6. Regression: thread-safety & BGR input ----------------
class _ConcurrencyProbeOCR:
    """Fake predictor that detects overlapping ocr() calls and returns distinct document text."""

    def __init__(self):
        self._inner = threading.Lock()
        self.active = 0
        self.max_active = 0

    def ocr(self, img, cls=True):
        with self._inner:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            import time
            time.sleep(0.02)  # widen race window so overlap would be observed if unguarded
            if isinstance(img, str):
                tag = os.path.basename(img)
            else:
                tag = f"arr_{img.shape[0]}x{img.shape[1]}"
            return [[[[[0, 0], [10, 0], [10, 10], [0, 10]], (f"DISTINCT_CONTENT_{tag}", 0.99)]]]
        finally:
            with self._inner:
                self.active -= 1


def test_paddle_ocr_serializes_concurrent_inference(tmp_path):
    """Regression: concurrent extract() calls must serialize on the shared predictor.

    Before the fix, the scan engine's thread pool ran ocr.ocr() concurrently on one
    non-thread-safe PaddleOCR predictor, cross-contaminating results between files.
    The provider now guards every inference call with an internal lock.
    Verifies: 12 documents across 8 workers -> max_active == 1 -> zero cross-contamination.
    """
    num_docs = 12
    files = []
    for i in range(num_docs):
        p = tmp_path / f"doc_{i}.png"
        Image.new("RGB", (200, 80), (255, 255, 255)).save(p)
        files.append(str(p))

    provider = PaddleOCRProvider()
    probe = _ConcurrencyProbeOCR()
    with patch.object(provider, "_get_ocr", return_value=probe):
        with ThreadPoolExecutor(max_workers=8) as pool:
            texts = list(pool.map(lambda fp: provider.extract(fp, "png"), files))

    assert probe.max_active == 1, f"OCR inference calls overlapped (max_active={probe.max_active}): predictor is not thread-safe"
    for i in range(num_docs):
        expected_tag = f"DISTINCT_CONTENT_doc_{i}.png"
        assert texts[i] is not None, f"doc_{i} OCR returned None"
        assert expected_tag in texts[i], f"doc_{i} did not receive its own text; got: {texts[i]}"
        # Ensure zero cross-contamination from any other document
        for j in range(num_docs):
            if j != i:
                other_tag = f"DISTINCT_CONTENT_doc_{j}.png"
                assert other_tag not in texts[i], f"doc_{i} contaminated with {other_tag}"


def test_paddle_ocr_pdf_passes_writable_bgr_array(tmp_path):
    """Regression: PDF pages must be handed to PaddleOCR as a contiguous, writable BGR
    array (matching the image path / PaddleOCR's own renderer), not a read-only RGB view.
    """
    import fitz

    # Build a real 1-page scanned PDF from a generated PNG (colored to make channel order matter).
    png = tmp_path / "page.png"
    img = Image.new("RGB", (240, 120), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((10, 20), "COLORED DOC", fill=(0, 0, 200))  # strong blue
    d.text((10, 60), "RED 999.00", fill=(200, 0, 0))    # strong red
    img.save(png)

    pdf = tmp_path / "doc.pdf"
    doc = fitz.open()
    pg = doc.new_page(width=240, height=120)
    pg.insert_image(fitz.Rect(0, 0, 240, 120), filename=str(png))
    doc.save(str(pdf))
    doc.close()

    captured = {}

    class _CapturingOCR:
        def ocr(self, arr, cls=True):
            captured["arr"] = arr
            return [[[[[0, 0], [10, 0], [10, 10], [0, 10]], ("X", 0.9)]]]

    provider = PaddleOCRProvider()
    with patch.object(provider, "_get_ocr", return_value=_CapturingOCR()):
        provider.extract(str(pdf), "pdf")

    arr = captured["arr"]
    assert isinstance(arr, np.ndarray)
    assert arr.ndim == 3 and arr.shape[2] == 3, "expected a 3-channel image"
    assert arr.flags["WRITEABLE"], "array passed to PaddleOCR must be writable, not a read-only view"
    assert arr.flags["C_CONTIGUOUS"], "array passed to PaddleOCR must be C-contiguous"

    # Recompute expected RGB/BGR from an independent render of the same page.
    doc2 = fitz.open(str(pdf))
    pix = doc2.load_page(0).get_pixmap(dpi=150)
    rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.h, pix.w, pix.n))
    doc2.close()
    expected_bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

    assert np.array_equal(arr, expected_bgr), "page should be converted RGB->BGR before OCR"
    assert not np.array_equal(arr, rgb), "raw RGB must not be passed straight to PaddleOCR"


def test_paddle_ocr_serializes_concurrent_pdf_inference(tmp_path):
    """Regression: concurrent PDF extract() calls must also serialize on the shared predictor."""
    import fitz

    pdfs = []
    for i in range(4):
        pdf_path = tmp_path / f"doc_{i}.pdf"
        doc = fitz.open()
        p = doc.new_page(width=200, height=100)
        doc.save(str(pdf_path))
        doc.close()
        pdfs.append(str(pdf_path))

    class _PdfProbeOCR:
        def __init__(self):
            self._inner = threading.Lock()
            self.active = 0
            self.max_active = 0

        def ocr(self, arr, cls=True):
            with self._inner:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                import time
                time.sleep(0.02)
                return [[[[[0, 0], [10, 0], [10, 10], [0, 10]], (f"PDF_PAGE_{arr.shape}", 0.99)]]]
            finally:
                with self._inner:
                    self.active -= 1

    provider = PaddleOCRProvider()
    probe = _PdfProbeOCR()
    with patch.object(provider, "_get_ocr", return_value=probe):
        with ThreadPoolExecutor(max_workers=4) as pool:
            texts = list(pool.map(lambda fp: provider.extract(fp, "pdf"), pdfs))

    assert probe.max_active == 1, f"PDF OCR inference calls overlapped (max_active={probe.max_active})"
    assert all(t and "--- Page 1 ---" in t for t in texts)
