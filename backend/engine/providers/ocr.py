"""Baidu Unlimited-OCR provider.

Provides optical character recognition for scanned PDFs and document images
using Baidu Unlimited-OCR (an open-source, long-horizon document parsing model
designed for multi-page documents via Reference Sliding Window Attention,
served via vLLM / SGLang OpenAI-compatible endpoint or Baidu Cloud BCE API).
"""
import base64
import logging
import os
from typing import Callable, Dict, List, Optional

import httpx

from ..interfaces import OCRProvider

logger = logging.getLogger("ledgerlens.ocr")

IMAGE_EXTS = {"jpg", "jpeg", "png", "tiff", "tif", "bmp", "webp"}
DEFAULT_TIMEOUT = int(os.environ.get("BAIDU_OCR_TIMEOUT") or os.environ.get("LEDGERLENS_OCR_PAGE_TIMEOUT") or "30")


class NoOpOCRProvider(OCRProvider):
    name = "noop"

    @property
    def available(self) -> bool:
        return False

    def extract(self, path: str, ext: str, should_cancel: Optional[Callable] = None) -> Optional[str]:
        return None


class BaiduUnlimitedOCRProvider(OCRProvider):
    """Baidu Unlimited-OCR implementation for document images and scanned PDFs.

    Configuration (via environment variables or constructor args):
      - BAIDU_UNLIMITED_OCR_ENDPOINT / BAIDU_OCR_API_URL / BAIDU_OCR_ENDPOINT:
          Base URL of the Baidu Unlimited-OCR OpenAI-compatible server
          (e.g., 'http://localhost:8000/v1' or 'https://api.baidu.com/...').
      - BAIDU_UNLIMITED_OCR_API_KEY / BAIDU_OCR_API_KEY:
          API key / bearer token (optional if self-hosted without auth, defaults to 'EMPTY').
      - BAIDU_UNLIMITED_OCR_MODEL / BAIDU_OCR_MODEL:
          Model name (default: 'baidu/Unlimited-OCR').
      - BAIDU_API_KEY + BAIDU_SECRET_KEY:
          Optional Baidu Cloud BCE API Key & Secret Key for Baidu AIP General OCR fallback.
      - BAIDU_OCR_TIMEOUT / LEDGERLENS_OCR_PAGE_TIMEOUT:
          Per-request timeout in seconds (default: 30).
    """

    name = "baidu_unlimited_ocr"

    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        baidu_ak: Optional[str] = None,
        baidu_sk: Optional[str] = None,
        timeout: Optional[int] = None,
    ):
        self._endpoint = (
            endpoint
            if endpoint is not None
            else (
                os.environ.get("BAIDU_UNLIMITED_OCR_ENDPOINT")
                or os.environ.get("BAIDU_OCR_API_URL")
                or os.environ.get("BAIDU_OCR_ENDPOINT")
                or ""
            )
        ).rstrip("/")

        self._api_key = (
            api_key
            if api_key is not None
            else (os.environ.get("BAIDU_UNLIMITED_OCR_API_KEY") or os.environ.get("BAIDU_OCR_API_KEY") or "")
        )

        self._model = (
            model
            or os.environ.get("BAIDU_UNLIMITED_OCR_MODEL")
            or os.environ.get("BAIDU_OCR_MODEL")
            or "baidu/Unlimited-OCR"
        )

        self._baidu_ak = (
            baidu_ak
            if baidu_ak is not None
            else (os.environ.get("BAIDU_API_KEY") or os.environ.get("BAIDU_OCR_AK") or "")
        )
        self._baidu_sk = (
            baidu_sk
            if baidu_sk is not None
            else (os.environ.get("BAIDU_SECRET_KEY") or os.environ.get("BAIDU_OCR_SK") or "")
        )

        self._timeout = timeout or DEFAULT_TIMEOUT
        self._bce_token: Optional[str] = None

    @property
    def available(self) -> bool:
        """Available if a valid endpoint is set, an API key is set, or Baidu BCE AK/SK are provided."""
        return bool(self._endpoint or (self._baidu_ak and self._baidu_sk))

    def get_config_summary(self) -> Dict[str, str]:
        """Return a safe summary of the current OCR configuration without revealing secrets."""
        masked_key = (
            (self._api_key[:3] + "..." + self._api_key[-3:])
            if len(self._api_key) > 6
            else ("***" if self._api_key else "none")
        )
        masked_ak = (
            (self._baidu_ak[:3] + "..." + self._baidu_ak[-3:])
            if len(self._baidu_ak) > 6
            else ("***" if self._baidu_ak else "none")
        )
        mode = (
            "openai_compatible"
            if self._endpoint
            else ("baidu_bce" if (self._baidu_ak and self._baidu_sk) else "not_configured")
        )
        return {
            "provider": self.name,
            "available": str(self.available),
            "mode": mode,
            "endpoint": self._endpoint or "none",
            "model": self._model,
            "api_key_configured": str(bool(self._api_key)),
            "api_key_masked": masked_key,
            "baidu_ak_configured": str(bool(self._baidu_ak)),
            "baidu_ak_masked": masked_ak,
            "timeout": str(self._timeout),
        }

    def extract(self, path: str, ext: str, should_cancel: Optional[Callable] = None) -> Optional[str]:
        """Recognize text from an image or scanned PDF using Baidu Unlimited-OCR."""
        if not self.available:
            logger.warning(
                "Baidu Unlimited-OCR requested for %s, but provider is not configured. "
                "Set BAIDU_UNLIMITED_OCR_ENDPOINT or BAIDU_OCR_API_URL in environment.",
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
            logger.error("Baidu Unlimited-OCR failed on %s: %s", path, e, exc_info=True)
            return None

    def _extract_image(self, path: str) -> Optional[str]:
        with open(path, "rb") as f:
            img_bytes = f.read()
        if not img_bytes:
            return None
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else "png"
        return self._send_ocr_request(img_bytes, fmt=ext)

    def _extract_pdf(self, path: str, should_cancel: Optional[Callable] = None) -> Optional[str]:
        try:
            import fitz  # PyMuPDF
        except ImportError:
            logger.error("PyMuPDF (fitz) is required for PDF OCR page rendering.")
            return None

        doc = fitz.open(path)
        pages_text: List[str] = []
        try:
            for page_idx in range(len(doc)):
                if should_cancel and should_cancel():
                    logger.info("OCR extraction cancelled by caller.")
                    break
                page = doc[page_idx]
                pix = page.get_pixmap(dpi=150)
                img_bytes = pix.tobytes("png")
                page_text = self._send_ocr_request(img_bytes, fmt="png")
                if page_text and page_text.strip():
                    pages_text.append(page_text.strip())
        finally:
            doc.close()

        if not pages_text:
            return None
        return "\n\n".join(pages_text)

    def _send_ocr_request(self, img_bytes: bytes, fmt: str) -> Optional[str]:
        if self._endpoint:
            return self._call_openai_compatible(img_bytes, fmt)
        if self._baidu_ak and self._baidu_sk:
            return self._call_baidu_bce(img_bytes)
        return None

    def _call_openai_compatible(self, img_bytes: bytes, fmt: str) -> Optional[str]:
        target_url = self._endpoint
        if not target_url.endswith("/chat/completions"):
            target_url = f"{target_url}/chat/completions"

        mime = "jpeg" if fmt in ("jpg", "jpeg") else ("png" if fmt == "png" else "octet-stream")
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        data_uri = f"data:image/{mime};base64,{b64}"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key or 'EMPTY'}",
        }

        # Baidu Unlimited-OCR expects prompt with <image>
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "<image>document parsing."},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            "max_tokens": 4096,
            "temperature": 0.0,
        }

        try:
            with httpx.Client(timeout=float(self._timeout)) as client:
                resp = client.post(target_url, json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices") or []
                    if choices:
                        msg = choices[0].get("message") or {}
                        content = msg.get("content") or ""
                        return content.strip() or None
                else:
                    logger.error("Baidu Unlimited-OCR server returned HTTP %d: %s", resp.status_code, resp.text)
                    return None
        except Exception as e:
            logger.error("HTTP error calling Baidu Unlimited-OCR: %s", e)
            return None

    def _call_baidu_bce(self, img_bytes: bytes) -> Optional[str]:
        token = self._get_bce_access_token()
        if not token:
            return None
        url = f"https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic?access_token={token}"
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {"image": b64}

        try:
            with httpx.Client(timeout=float(self._timeout)) as client:
                resp = client.post(url, data=data, headers=headers)
                if resp.status_code == 200:
                    res_json = resp.json()
                    words = [w.get("words", "") for w in res_json.get("words_result", []) if w.get("words")]
                    return "\n".join(words).strip() or None
                else:
                    logger.error("Baidu BCE OCR returned HTTP %d: %s", resp.status_code, resp.text)
                    return None
        except Exception as e:
            logger.error("HTTP error calling Baidu BCE OCR: %s", e)
            return None

    def _get_bce_access_token(self) -> Optional[str]:
        if self._bce_token:
            return self._bce_token
        url = (
            f"https://aip.baidubce.com/oauth/2.0/token"
            f"?grant_type=client_credentials&client_id={self._baidu_ak}&client_secret={self._baidu_sk}"
        )
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url)
                if resp.status_code == 200:
                    token = resp.json().get("access_token")
                    self._bce_token = token
                    return token
                logger.error("Failed to acquire Baidu BCE token: %s", resp.text)
                return None
        except Exception as e:
            logger.error("Baidu BCE token request failed: %s", e)
            return None


