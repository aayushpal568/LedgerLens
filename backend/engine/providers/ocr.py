"""PaddleOCR local optical character recognition provider.

Provides local optical character recognition for scanned PDFs and document images
using PaddleOCR running on the user's machine without external cloud API dependencies.
Scanned PDFs are processed sequentially one page at a time with explicit memory release.
"""
import logging
import os
import threading
from typing import Callable, Dict, List, Optional

from ..interfaces import OCRProvider

logger = logging.getLogger("ledgerlens.ocr")

IMAGE_EXTS = {"jpg", "jpeg", "png", "tiff", "tif", "bmp", "webp"}


class NoOpOCRProvider(OCRProvider):
    name = "noop"

    @property
    def available(self) -> bool:
        return False

    def extract(self, path: str, ext: str, should_cancel: Optional[Callable] = None) -> Optional[str]:
        return None


class PaddleOCRProvider(OCRProvider):
    """Local PaddleOCR implementation for document images and scanned PDFs.

    Features:
      - Fully local execution; no external paid or cloud OCR APIs.
      - Defaults to CPU (no GPU required); optional GPU can be enabled via config.
      - Scanned PDFs are processed sequentially one page at a time:
          Page 1 -> OCR -> release page memory
          Page 2 -> OCR -> release page memory
          ...until the final page.
      - Preserves page order, page numbers, and partial progress if a subsequent page fails.
      - Cooperative cancellation support via `should_cancel`.

    Configuration (via environment variables or constructor args):
      - PADDLE_OCR_USE_GPU: Set to 'true'/'1' to enable GPU acceleration if CUDA is available (default: False).
      - PADDLE_OCR_LANG: Language code, e.g. 'en', 'ch' (default: 'en').
      - PADDLE_OCR_USE_ANGLE_CLS: Set to 'true'/'1' to enable text orientation classifier (default: True).
    """

    name = "paddle_ocr"

    def __init__(
        self,
        use_gpu: Optional[bool] = None,
        lang: Optional[str] = None,
        use_angle_cls: Optional[bool] = None,
        show_log: bool = False,
        enabled: Optional[bool] = None,
    ):
        if enabled is not None:
            self._enabled = enabled
        else:
            en_env = os.environ.get("PADDLE_OCR_ENABLED", "true").strip().lower()
            self._enabled = en_env not in ("0", "false", "no", "off")

        if use_gpu is not None:
            self._use_gpu = use_gpu
        else:
            gpu_env = os.environ.get("PADDLE_OCR_USE_GPU", "").strip().lower()
            self._use_gpu = gpu_env in ("1", "true", "yes", "on")

        self._lang = lang or os.environ.get("PADDLE_OCR_LANG", "en")

        if use_angle_cls is not None:
            self._use_angle_cls = use_angle_cls
        else:
            cls_env = os.environ.get("PADDLE_OCR_USE_ANGLE_CLS", "true").strip().lower()
            self._use_angle_cls = cls_env in ("1", "true", "yes", "on")

        self._show_log = show_log
        self._ocr = None
        self._available_cache: Optional[bool] = None
        # The PaddleOCR predictor is NOT thread-safe: concurrent ocr() calls on a
        # shared instance produce cross-contaminated / aliased results. The scan
        # engine runs per-file extraction on a thread pool, so we serialize every
        # inference call through this lock and guard lazy initialization separately.
        self._lock = threading.Lock()
        self._init_lock = threading.Lock()

    @property
    def available(self) -> bool:
        """Returns True if paddleocr and paddle are installed and importable."""
        if not self._enabled:
            return False
        if self._available_cache is not None:
            return self._available_cache
        try:
            import paddleocr
            import paddle  # noqa: F401
            self._available_cache = True
            return True
        except Exception as e:
            logger.debug("PaddleOCR not available: %s", e)
            self._available_cache = False
            return False

    def _get_ocr(self):
        """Lazily initialize a single shared PaddleOCR instance (thread-safe)."""
        if self._ocr is None:
            with self._init_lock:
                if self._ocr is None:
                    try:
                        from paddleocr import PaddleOCR
                        self._ocr = PaddleOCR(
                            use_angle_cls=self._use_angle_cls,
                            lang=self._lang,
                            use_gpu=self._use_gpu,
                            show_log=self._show_log,
                        )
                    except Exception as e:
                        logger.error("Failed to initialize PaddleOCR engine: %s", e)
                        return None
        return self._ocr

    def get_config_summary(self) -> Dict[str, str]:
        """Return a safe summary of the current OCR configuration."""
        return {
            "provider": self.name,
            "available": str(self.available),
            "mode": "local",
            "use_gpu": str(self._use_gpu),
            "lang": self._lang,
            "use_angle_cls": str(self._use_angle_cls),
        }

    def _parse_ocr_result(self, result) -> Optional[str]:
        """Parse raw PaddleOCR detection and recognition results into clean text."""
        if not result:
            return None
        lines: List[str] = []
        for page_res in result:
            if not page_res:
                continue
            for line in page_res:
                if line and len(line) >= 2 and isinstance(line[1], (list, tuple)):
                    text = line[1][0]
                    if text and str(text).strip():
                        lines.append(str(text).strip())
        if not lines:
            return None
        return "\n".join(lines)

    def extract(self, path: str, ext: str, should_cancel: Optional[Callable] = None) -> Optional[str]:
        """Recognize text from an image or scanned PDF using local PaddleOCR."""
        if not self.available:
            logger.warning(
                "PaddleOCR requested for %s, but PaddleOCR is not installed or available.",
                path,
            )
            return None

        if not os.path.isfile(path):
            logger.error("OCR file not found: %s", path)
            return None

        clean_ext = (ext or "").lower().lstrip(".")
        try:
            if clean_ext == "pdf":
                return self._extract_pdf(path, should_cancel=should_cancel)
            if clean_ext in IMAGE_EXTS:
                return self._extract_image(path)
            logger.warning("Unsupported file format for OCR: .%s", clean_ext)
            return None
        except Exception as e:
            logger.error("PaddleOCR failed on %s: %s", path, e, exc_info=True)
            return None

    def _extract_image(self, path: str) -> Optional[str]:
        """Extract text from a standalone image file."""
        ocr = self._get_ocr()
        if ocr is None:
            return None
        try:
            from PIL import Image
            with Image.open(path) as img:
                img.verify()
        except Exception as img_err:
            logger.warning("Corrupted or unreadable image file %s: %s", path, img_err)
            return None

        try:
            with self._lock:
                res = ocr.ocr(path, cls=self._use_angle_cls)
            return self._parse_ocr_result(res)
        except Exception as e:
            logger.error("PaddleOCR image extraction error for %s: %s", path, e)
            return None

    def _extract_pdf(self, path: str, should_cancel: Optional[Callable] = None) -> Optional[str]:
        """Process scanned PDFs strictly one page at a time.

        Page 1 -> OCR -> release page memory
        Page 2 -> OCR -> release page memory
        ...until the final page.

        Preserves page order and page numbers.
        Preserves successfully processed pages if a later page fails.
        """
        try:
            import fitz  # PyMuPDF
        except ImportError:
            logger.error("PyMuPDF (fitz) is required for PDF OCR page rendering.")
            return None

        import gc
        import numpy as np

        ocr = self._get_ocr()
        if ocr is None:
            return None

        doc = None
        pages_text: List[str] = []
        try:
            doc = fitz.open(path)
            total_pages = len(doc)
            for page_idx in range(total_pages):
                if should_cancel and should_cancel():
                    logger.info(
                        "PaddleOCR extraction cancelled by caller at page %d/%d.",
                        page_idx + 1,
                        total_pages,
                    )
                    break

                pix = None
                page = None
                img_np = None
                img_arr = None
                try:
                    import cv2
                    page = doc.load_page(page_idx)
                    pix = page.get_pixmap(dpi=150)
                    # PyMuPDF renders in RGB; PaddleOCR consumes BGR (matches both the
                    # cv2.imread image path and PaddleOCR's own PDF renderer). Build a
                    # contiguous, writable BGR array rather than passing the read-only
                    # RGB frombuffer view straight through.
                    img_arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.h, pix.w, pix.n))
                    if pix.n == 4:
                        img_np = np.ascontiguousarray(cv2.cvtColor(img_arr, cv2.COLOR_RGBA2BGR))
                    elif pix.n == 1:
                        img_np = np.ascontiguousarray(cv2.cvtColor(img_arr, cv2.COLOR_GRAY2BGR))
                    else:  # pix.n == 3 (RGB)
                        img_np = np.ascontiguousarray(cv2.cvtColor(img_arr, cv2.COLOR_RGB2BGR))

                    if not img_np.flags["WRITEABLE"]:
                        img_np = img_np.copy()

                    with self._lock:
                        ocr_res = ocr.ocr(img_np, cls=self._use_angle_cls)
                    page_str = self._parse_ocr_result(ocr_res)
                    if page_str and page_str.strip():
                        pages_text.append(f"--- Page {page_idx + 1} ---\n{page_str.strip()}")
                except Exception as page_err:
                    logger.warning("PaddleOCR error on page %d of %s: %s", page_idx + 1, path, page_err)
                    # Crucial: preserve already extracted pages even if later pages fail
                finally:
                    del pix
                    del page
                    del img_np
                    del img_arr
                    gc.collect()
        except Exception as pdf_err:
            logger.error("PaddleOCR failed to open/process PDF %s: %s", path, pdf_err)
        finally:
            if doc is not None:
                doc.close()

        if not pages_text:
            return None
        return "\n\n".join(pages_text)


