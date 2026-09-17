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

