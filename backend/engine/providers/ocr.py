"""OCR providers."""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Callable, List, Optional

from ..interfaces import OCRProvider

IMAGE_EXTS = {"jpg", "jpeg", "png", "tiff", "tif", "bmp"}
OCR_PAGE_TIMEOUT = int(os.environ.get("LEDGERLENS_OCR_PAGE_TIMEOUT", "30"))


class NoOpOCRProvider(OCRProvider):
    name = "noop"

    @property
    def available(self):
        return False

    def extract(self, path, ext, should_cancel=None):
        return None


class PaddleOCRProvider(OCRProvider):
    name = "paddleocr"

    def __init__(self, lang="en", use_gpu=False, pdf_dpi=200, max_pdf_pages=30):
        self.lang = lang
        self.use_gpu = use_gpu
        self.pdf_dpi = pdf_dpi
        self.max_pdf_pages = max_pdf_pages
        self._engine = None
        self._ocr_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr")

    def _load(self):
        if self._engine is not None:
            return self._engine
        candidates = [
            os.environ.get("PADDLEX_MODELS_DIR"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "resources", "paddlex_models"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "resources", "paddlex_models"),
            os.path.join(os.getcwd(), "resources", "paddlex_models"),
            os.path.join(os.getcwd(), "paddlex_models"),
        ]
        import sys
        if getattr(sys, "frozen", False):
            candidates.insert(0, os.path.join(sys._MEIPASS, "resources", "paddlex_models"))
            exe_dir = os.path.dirname(sys.executable)
            candidates.insert(0, os.path.join(exe_dir, "resources", "paddlex_models"))
            candidates.insert(0, os.path.join(exe_dir, "paddlex_models"))
            candidates.insert(0, os.path.join(os.path.dirname(exe_dir), "resources", "paddlex_models"))
            candidates.insert(0, os.path.join(os.path.dirname(exe_dir), "resources", "resources", "paddlex_models"))
        target_models_dir = os.path.expanduser(r"~\.paddlex\official_models")
        for c in candidates:
            if c and os.path.isdir(c):
                try:
                    import shutil
                    os.makedirs(target_models_dir, exist_ok=True)
                    for item in os.listdir(c):
                        src = os.path.join(c, item)
                        dst = os.path.join(target_models_dir, item)
                        if not os.path.exists(dst):
                            if os.path.isdir(src):
                                shutil.copytree(src, dst)
                            else:
                                shutil.copy2(src, dst)
                except Exception:
                    pass
                break
        from paddleocr import PaddleOCR
        try:
            self._engine = PaddleOCR(lang=self.lang, enable_mkldnn=False)
        except Exception:
            try:
                self._engine = PaddleOCR(lang=self.lang, use_gpu=self.use_gpu, enable_mkldnn=False)
            except Exception:
                self._engine = PaddleOCR(lang=self.lang)
        return self._engine

    @property
    def available(self):
        try:
            import importlib.util
            return (importlib.util.find_spec("paddleocr") is not None
                    and importlib.util.find_spec("paddle") is not None)
        except Exception:
            return False

    def _ocr_image_sync(self, image_path):
        engine = self._load()
        lines = []
        if hasattr(engine, "predict"):
            try:
                res = engine.predict(image_path)
                for item in (res or []):
                    if isinstance(item, dict) and "rec_texts" in item:
                        lines.extend(item["rec_texts"])
                    elif hasattr(item, "get") and item.get("rec_texts"):
                        lines.extend(item.get("rec_texts"))
                if lines:
                    return "\n".join(lines)
            except Exception:
                pass
        try:
            result = engine.ocr(image_path)
            for block in (result or []):
                for line in (block or []):
                    try:
                        lines.append(line[1][0])
                    except (IndexError, TypeError):
                        continue
        except Exception:
            pass
        return "\n".join(lines)

    def _ocr_image_with_timeout(self, image_path):
        try:
            fut = self._ocr_pool.submit(self._ocr_image_sync, image_path)
            return fut.result(timeout=OCR_PAGE_TIMEOUT)
        except FutureTimeoutError:
            return ""
        except Exception:
            return ""

    def _ocr_pdf(self, pdf_path, should_cancel=None):
        import pymupdf
        text_parts = []
        doc = pymupdf.open(pdf_path)
        try:
            for i, page in enumerate(doc):
                if should_cancel and should_cancel():
                    break
                if i >= self.max_pdf_pages:
                    break
                pix = page.get_pixmap(dpi=self.pdf_dpi)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp_path = tmp.name
                    pix.save(tmp_path)
                try:
                    text_parts.append(self._ocr_image_with_timeout(tmp_path))
                finally:
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
                if should_cancel and should_cancel():
                    break
        finally:
            doc.close()
        return "\n".join(p for p in text_parts if p)

    def extract(self, path, ext, should_cancel=None):
        if not self.available:
            return None
        ext = (ext or "").lower().lstrip(".")
        try:
            if ext in IMAGE_EXTS:
                text = self._ocr_image_with_timeout(path)
            elif ext == "pdf":
                text = self._ocr_pdf(path, should_cancel=should_cancel)
            else:
                return None
        except Exception:
            return None
        return text or None
