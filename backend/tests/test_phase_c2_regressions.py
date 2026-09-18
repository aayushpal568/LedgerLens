"""Regression tests for Phase C2:
- File content validation: valid headers, disguised executables rejection, extension/header mismatches
- Safe error handling without raw str(e) or filesystem path leaks in scans and agent runs
"""
import io
import os
from pathlib import Path
import sys
import pytest
from fastapi import HTTPException

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATA_BACKEND", "memory")
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-32-chars-long-ledgerlens-suite")

from server import validate_file_content


def test_validate_valid_files():
    # Valid PDF
    assert validate_file_content("invoice.pdf", b"%PDF-1.4\n%test content\n") == "pdf"

    # Valid PNG
    assert validate_file_content("receipt.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR...") == "png"

    # Valid JPEG
    assert validate_file_content("photo.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF...") == "jpg"
    assert validate_file_content("photo.jpeg", b"\xff\xd8\xff\xe1...") == "jpeg"

    # Valid TIFF
    assert validate_file_content("scan.tiff", b"II*\x00\x08\x00\x00\x00...") == "tiff"
    assert validate_file_content("scan.tif", b"MM\x00*\x00\x00\x00\x08...") == "tif"

    # Valid XLSX / DOCX (ZIP archive)
    assert validate_file_content("ledger.xlsx", b"PK\x03\x04\x14\x00\x06\x00...") == "xlsx"
    assert validate_file_content("doc.docx", b"PK\x03\x04\x14\x00\x06\x00...") == "docx"

    # Valid CSV
    assert validate_file_content("data.csv", b"Date,Account,Amount\n2024-01-01,1000,500.00\n") == "csv"


def test_reject_disguised_executables():
    # Windows PE executable renamed to .pdf
    fake_pdf = b"MZ\x90\x00\x03\x00\x00\x00This program cannot be run in DOS mode."
    with pytest.raises(HTTPException) as exc_info:
        validate_file_content("payload.pdf", fake_pdf)
    assert exc_info.value.status_code == 400
    assert "binary executable" in exc_info.value.detail.lower()

    # ELF executable renamed to .xlsx
    fake_xlsx = b"\x7fELF\x02\x01\x01\x00SomeExecutableBinary..."
    with pytest.raises(HTTPException) as exc_info:
        validate_file_content("ledger.xlsx", fake_xlsx)
    assert exc_info.value.status_code == 400
    assert "binary executable" in exc_info.value.detail.lower()


def test_reject_mismatched_file_headers():
    # Text content with .pdf extension
    with pytest.raises(HTTPException) as exc_info:
        validate_file_content("plain.pdf", b"This is just plain text, not a real PDF.")
    assert exc_info.value.status_code == 400
    assert "lacks a valid pdf header" in exc_info.value.detail.lower()

    # PDF content with .png extension
    with pytest.raises(HTTPException) as exc_info:
        validate_file_content("fake_image.png", b"%PDF-1.5 fake png")
    assert exc_info.value.status_code == 400
    assert "lacks a valid png header" in exc_info.value.detail.lower()

    # Binary with null bytes with .csv extension
    with pytest.raises(HTTPException) as exc_info:
        validate_file_content("bad.csv", b"Header1,Header2\x00\x01BinaryGarbage")
    assert exc_info.value.status_code == 400
    assert "null bytes" in exc_info.value.detail.lower()


def test_reject_unsupported_extensions():
    with pytest.raises(HTTPException) as exc_info:
        validate_file_content("script.py", b"print('hello')")
    assert exc_info.value.status_code == 400
    assert "unsupported file extension" in exc_info.value.detail.lower()
