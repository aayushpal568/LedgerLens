import io
import os
import json
import pytest
from unittest.mock import patch, MagicMock
from PIL import Image, ImageDraw

from engine.interfaces import OCRProvider
from engine.models import STATUS_OK, STATUS_NEEDS_OCR
from engine.providers import (
    BaiduUnlimitedOCRProvider,
    NoOpOCRProvider,
    DefaultDocumentExtractor,
    LocalPathFileSource,
)
from engine.scan_engine import build_default_engine, ScanEngine


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
    """Generate a real scanned (image-only) PDF."""
    import fitz
    pdf_path = tmp_path / "scanned_document.pdf"
    doc = fitz.open()
    page = doc.new_page(width=400, height=200)
    page.insert_image(fitz.Rect(0, 0, 400, 200), filename=sample_image)
    doc.save(str(pdf_path))
    doc.close()
    return str(pdf_path)


# ---------------- 1. Initialization & Config Handling ----------------
def test_baidu_ocr_initialization_defaults():
    """Verify provider initialization and defaults when unconfigured."""
    with patch.dict(os.environ, {}, clear=True):
        provider = BaiduUnlimitedOCRProvider()
        assert provider.name == "baidu_unlimited_ocr"
        assert provider.available is False
        summary = provider.get_config_summary()
        assert summary["available"] == "False"
        assert summary["mode"] == "not_configured"
        assert summary["api_key_masked"] == "none"


def test_baidu_ocr_configuration_from_env():
    """Verify provider discovers endpoint and key from environment variables."""
    env = {
        "BAIDU_UNLIMITED_OCR_ENDPOINT": "https://ocr.example.com/v1",
        "BAIDU_UNLIMITED_OCR_API_KEY": "secret_key_12345678",
        "BAIDU_UNLIMITED_OCR_MODEL": "baidu/Unlimited-OCR",
    }
    with patch.dict(os.environ, env, clear=True):
        provider = BaiduUnlimitedOCRProvider()
        assert provider.available is True
        summary = provider.get_config_summary()
        assert summary["available"] == "True"
        assert summary["mode"] == "openai_compatible"
        assert summary["endpoint"] == "https://ocr.example.com/v1"
        assert summary["model"] == "baidu/Unlimited-OCR"
        # Secret key must be masked
        assert "secret_key_12345678" not in summary["api_key_masked"]
        assert summary["api_key_masked"].startswith("sec...")


def test_baidu_ocr_bce_configuration_from_env():
    """Verify provider recognizes Baidu BCE AK/SK configuration."""
    env = {
        "BAIDU_API_KEY": "ak_1234567890",
        "BAIDU_SECRET_KEY": "sk_0987654321",
    }
    with patch.dict(os.environ, env, clear=True):
        provider = BaiduUnlimitedOCRProvider()
        assert provider.available is True
        summary = provider.get_config_summary()
        assert summary["mode"] == "baidu_bce"


# ---------------- 2. Graceful Failure When Not Configured ----------------
def test_baidu_ocr_graceful_when_not_configured(sample_image):
    """If credentials are missing, extract returns None without raising."""
    with patch.dict(os.environ, {}, clear=True):
        provider = BaiduUnlimitedOCRProvider()
        res = provider.extract(sample_image, "png")
        assert res is None

        extractor = DefaultDocumentExtractor(provider)
        extracted = extractor.extract(sample_image, "png")
        assert extracted.status == STATUS_NEEDS_OCR
        assert extracted.ocr_used is False
        assert "OCR is not configured" in extracted.reason
        assert "baidu_unlimited_ocr" in extracted.reason


# ---------------- 3. Real Image & Scanned PDF OCR with Mocked API ----------------
def test_baidu_ocr_extract_image_success(sample_image):
    """Test OCR extraction on an image via OpenAI-compatible vision endpoint."""
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "# INVOICE\n\n**Invoice Number**: INV-2024-9988\n**Date**: 2024-06-15\n**Total**: $3,450.00"
                }
            }
        ]
    }

    with patch("httpx.Client.post", return_value=fake_response) as mock_post:
        provider = BaiduUnlimitedOCRProvider(
            endpoint="http://127.0.0.1:8000/v1",
            api_key="test_token",
        )
        assert provider.available is True
        text = provider.extract(sample_image, "png")

        assert text is not None
        assert "INV-2024-9988" in text
        assert "3,450.00" in text

        # Verify request format conforms to Baidu Unlimited-OCR specs
        assert mock_post.called
        call_args = mock_post.call_args
        target_url = call_args[0][0]
        assert target_url == "http://127.0.0.1:8000/v1/chat/completions"
        payload = call_args[1]["json"]
        assert payload["model"] == "baidu/Unlimited-OCR"
        assert payload["messages"][0]["content"][0]["text"] == "<image>document parsing."
        assert payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_baidu_ocr_extract_scanned_pdf_success(sample_scanned_pdf):
    """Test OCR extraction on an image-only PDF with PyMuPDF rendering."""
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "Page 1 Content\nINVOICE #INV-2024-9988\nTotal: $3,450.00"
                }
            }
        ]
    }

    with patch("httpx.Client.post", return_value=fake_response):
        provider = BaiduUnlimitedOCRProvider(endpoint="http://localhost:8000/v1")
        text = provider.extract(sample_scanned_pdf, "pdf")

        assert text is not None
        assert "INVOICE #INV-2024-9988" in text


# ---------------- 4. Integration with Document Pipeline ----------------
def test_ocr_text_reaches_pipeline_and_triggers_detections(sample_image):
    """Verify that OCR text flows into ScanEngine and triggers period & duplicate detection."""
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "Profit and Loss Statement 2021\nGross Revenue: $500,000\nExpenses: $350,000"
                }
            }
        ]
    }

    with patch("httpx.Client.post", return_value=fake_response):
        provider = BaiduUnlimitedOCRProvider(endpoint="http://localhost:8000/v1")
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
        # Expected period is 2024, but OCR text has 2021 -> should produce wrong_period finding!
        result = engine.run(source, checklist_items=[], expected_period=2024)

        assert result["processed"] == 1
        assert result["counts"]["wrong_period"] == 1
        finding = next(f for f in result["findings"] if f["category"] == "wrong_period")
        assert finding["evidence"]["detected_year"] == 2021


# ---------------- 5. OCR Network / Server Failure Handled Cleanly ----------------
def test_ocr_server_http_error_handled_cleanly(sample_image):
    """When OCR server returns HTTP 500, scan does not crash and marks file needs_ocr."""
    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.text = "Internal Server Error"

    with patch("httpx.Client.post", return_value=fake_response):
        provider = BaiduUnlimitedOCRProvider(endpoint="http://localhost:8000/v1")
        extractor = DefaultDocumentExtractor(provider)
        res = extractor.extract(sample_image, "png")

        assert res.status == STATUS_NEEDS_OCR
        assert res.ocr_used is False
        assert "OCR processing failed" in res.reason


def test_ocr_network_timeout_handled_cleanly(sample_image):
    """When OCR server times out or fails to connect, scan continues safely."""
    import httpx

    with patch("httpx.Client.post", side_effect=httpx.ConnectTimeout("Connection timed out")):
        provider = BaiduUnlimitedOCRProvider(endpoint="http://localhost:8000/v1")
        extractor = DefaultDocumentExtractor(provider)
        res = extractor.extract(sample_image, "png")

        assert res.status == STATUS_NEEDS_OCR
        assert res.ocr_used is False
        assert "OCR processing failed" in res.reason
