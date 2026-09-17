"""Default document extractor.

Wraps the low-level text extraction and, when an OCRProvider is available,
routes image / image-only-PDF files through it. Swapping in PaddleOCR later
requires no change here or in the detection engine.
"""
from typing import Optional

from ..interfaces import DocumentExtractor, OCRProvider
from ..models import ExtractionResult, STATUS_OK, STATUS_NEEDS_OCR
from ..extract import extract_text


class DefaultDocumentExtractor(DocumentExtractor):
    def __init__(self, ocr_provider=None):
        self.ocr = ocr_provider

    def extract(self, path, ext, should_cancel=None):
        text, status, reason = extract_text(path, ext)
        result = ExtractionResult(text=text, status=status, reason=reason)

        if status == STATUS_NEEDS_OCR:
            if self.ocr is not None and self.ocr.available:
                try:
                    import inspect
                    sig = inspect.signature(self.ocr.extract)
                    if "should_cancel" in sig.parameters:
                        ocr_text = self.ocr.extract(path, ext, should_cancel=should_cancel)
                    else:
                        ocr_text = self.ocr.extract(path, ext)
                except Exception:  # noqa: BLE001 - OCR must never crash the scan
                    ocr_text = None
                if ocr_text and ocr_text.strip():
                    return ExtractionResult(
                        text=ocr_text.strip(),
                        status=STATUS_OK,
                        reason="",
                        ocr_used=True,
                        meta={"ocr": self.ocr.name},
                    )
                return ExtractionResult(
                    text="",
                    status=STATUS_NEEDS_OCR,
                    reason=f"OCR processing failed or returned no text ({self.ocr.name})",
                    ocr_used=False,
                    meta={"ocr": self.ocr.name},
                )
            # OCR is not configured
            provider_name = self.ocr.name if self.ocr else "none"
            return ExtractionResult(
                text="",
                status=STATUS_NEEDS_OCR,
                reason=f"Scanned document requires OCR, but OCR is not configured ({provider_name})",
                ocr_used=False,
            )
        return result
