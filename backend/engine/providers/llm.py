"""Claude Opus LLM provider via fal.ai.

Integrates Claude Opus through fal.ai's text inference API (`fal-ai/any-llm`)
behind the modular LedgerLens `LLMProvider` interface. Also supports direct
Anthropic API routing as an interchangeable fallback.

Credentials and models are loaded strictly from environment variables:
  - FAL_KEY / FAL_API_KEY: fal.ai API access token
  - FAL_ENDPOINT / FAL_LLM_ENDPOINT: fal.ai endpoint (default: https://fal.run/fal-ai/any-llm)
  - CLAUDE_MODEL: Claude model on fal.ai (default: anthropic/claude-opus-4.6)
  - ANTHROPIC_API_KEY: Optional direct Anthropic API key for interchangeable fallback
  - ANTHROPIC_ENDPOINT: Direct Anthropic messages endpoint (default: https://api.anthropic.com/v1/messages)
  - ANTHROPIC_MODEL: Direct Anthropic model (default: claude-3-opus-20240229)
  - LLM_TIMEOUT / FAL_LLM_TIMEOUT: Per-request timeout in seconds (default: 60)
  - LLM_MAX_TOKENS: Maximum completion tokens (default: 4096)
"""
import json
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Union

import httpx

from ..interfaces import LLMProvider

try:
    from dotenv import load_dotenv
    _backend_env = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
    if os.path.exists(_backend_env):
        load_dotenv(_backend_env)
except Exception:
    pass

logger = logging.getLogger("ledgerlens.llm")

DEFAULT_FAL_ENDPOINT = "https://fal.run/openrouter/router/openai/v1/chat/completions"
DEFAULT_CLAUDE_MODEL = "anthropic/claude-opus-4.6"
DEFAULT_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
DEFAULT_ANTHROPIC_MODEL = "claude-3-opus-20240229"
DEFAULT_TIMEOUT = 60
DEFAULT_MAX_TOKENS = 4096
DEFAULT_SHORT_MAX_TOKENS = 256
DEFAULT_FIELD_MAX_TOKENS = 128
DEFAULT_SHORT_TIMEOUT = int(os.environ.get("LLM_SHORT_TIMEOUT", "15"))
MAX_TEXT_CONTEXT_CHARS = 16000  # Cap input text to avoid token limits


def _mask_secret(val: Optional[str]) -> str:
    """Safely mask API keys for logging and diagnostic summaries."""
    if not val:
        return "none"
    val = val.strip()
    if len(val) <= 6:
        return "***"
    return f"{val[:3]}...{val[-3:]}"


class ClassificationResult(str):
    """String subclass providing label, confidence, and reasoning for backward compatibility."""

    def __new__(cls, label: str, confidence: float = 1.0, reasoning: str = ""):
        obj = super().__new__(cls, label)
        obj.label = label
        obj.confidence = float(confidence)
        obj.reasoning = str(reasoning or "")
        return obj

    def __getitem__(self, item):
        if item in ("label", "classification"):
            return self.label
        if item == "confidence":
            return self.confidence
        if item == "reasoning":
            return self.reasoning
        return super().__getitem__(item)

    def get(self, key: str, default: Any = None) -> Any:
        if key in ("label", "classification"):
            return self.label
        if key == "confidence":
            return self.confidence
        if key == "reasoning":
            return self.reasoning
        return default

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
        }


class NoOpLLMProvider(LLMProvider):
    """Safe default no-op LLM provider when no credentials are configured."""
    name = "noop"

    @property
    def available(self) -> bool:
        return False

    def get_config_summary(self) -> Dict[str, str]:
        return {
            "provider": self.name,
            "available": "False",
            "mode": "noop",
        }

    def classify_document(self, text: str, candidate_types: Iterable[str], **kwargs) -> Optional[Any]:
        return None

    def extract_field(self, text: str, field: str, **kwargs) -> Optional[str]:
        return None

    def analyze_document(self, text: str, prompt: str) -> Optional[dict]:
        return None


class ClaudeOpusFalProvider(LLMProvider):
    """Claude Opus LLM provider hosted on fal.ai (with Anthropic direct fallback).

    Designed for accounting document classification, field extraction, and
    ambiguous-case semantic analysis behind the modular `LLMProvider` ABC.
    """
    name = "claude_opus_fal"

    def __init__(
        self,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        model: Optional[str] = None,
        anthropic_key: Optional[str] = None,
        anthropic_endpoint: Optional[str] = None,
        anthropic_model: Optional[str] = None,
        timeout: Optional[int] = None,
        max_tokens: Optional[int] = None,
        short_timeout: Optional[int] = None,
    ):
        self._fal_key = (
            api_key
            if api_key is not None
            else (os.environ.get("FAL_KEY") or os.environ.get("FAL_API_KEY") or "")
        ).strip()

        self._endpoint = (
            endpoint
            if endpoint is not None
            else (os.environ.get("FAL_ENDPOINT") or os.environ.get("FAL_LLM_ENDPOINT") or DEFAULT_FAL_ENDPOINT)
        ).strip().rstrip("/")

        self._model = (
            model
            if model is not None
            else (os.environ.get("CLAUDE_MODEL") or os.environ.get("FAL_CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL)
        ).strip()

        self._anthropic_key = (
            anthropic_key
            if anthropic_key is not None
            else (os.environ.get("ANTHROPIC_API_KEY") or "")
        ).strip()

        self._anthropic_endpoint = (
            anthropic_endpoint
            if anthropic_endpoint is not None
            else (os.environ.get("ANTHROPIC_ENDPOINT") or DEFAULT_ANTHROPIC_ENDPOINT)
        ).strip()

        self._anthropic_model = (
            anthropic_model
            if anthropic_model is not None
            else (os.environ.get("ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL)
        ).strip()

        timeout_env = os.environ.get("FAL_LLM_TIMEOUT") or os.environ.get("LLM_TIMEOUT")
        self._timeout = timeout if timeout is not None else (int(timeout_env) if timeout_env else DEFAULT_TIMEOUT)

        max_tokens_env = os.environ.get("FAL_LLM_MAX_TOKENS") or os.environ.get("LLM_MAX_TOKENS")
        self._max_tokens = max_tokens if max_tokens is not None else (int(max_tokens_env) if max_tokens_env else DEFAULT_MAX_TOKENS)

        short_timeout_env = os.environ.get("FAL_LLM_SHORT_TIMEOUT") or os.environ.get("LLM_SHORT_TIMEOUT")
        self._short_timeout = short_timeout if short_timeout is not None else (int(short_timeout_env) if short_timeout_env else DEFAULT_SHORT_TIMEOUT)

    @property
    def available(self) -> bool:
        """Available if either fal.ai key or direct Anthropic key is configured."""
        return bool(self._fal_key or self._anthropic_key)

    @property
    def backend_mode(self) -> str:
        """Indicates whether active routing uses fal.ai or Anthropic direct."""
        if self._fal_key:
            return "fal_ai"
        if self._anthropic_key:
            return "anthropic_direct"
        return "unconfigured"

    def get_config_summary(self) -> Dict[str, str]:
        """Return safe diagnostic summary with masked credentials."""
        return {
            "provider": self.name,
            "available": str(self.available),
            "backend_mode": self.backend_mode,
            "fal_endpoint": self._endpoint,
            "fal_model": self._model,
            "fal_key_configured": str(bool(self._fal_key)),
            "fal_key_masked": _mask_secret(self._fal_key),
            "anthropic_model": self._anthropic_model,
            "anthropic_key_configured": str(bool(self._anthropic_key)),
            "anthropic_key_masked": _mask_secret(self._anthropic_key),
            "timeout": str(self._timeout),
            "short_timeout": str(self._short_timeout),
            "max_tokens": str(self._max_tokens),
        }

    # ------------------------------------------------------------------
    # Raw API invocation
    # ------------------------------------------------------------------
    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> Optional[str]:
        """Send prompt to Claude Opus via fal.ai (or Anthropic fallback).

        Returns completion text on success, or None on error/timeout.
        Credentials and secrets are strictly masked in all logs.
        """
        if not self.available:
            logger.warning(
                "Claude Opus LLM requested, but no API credentials configured. "
                "Set FAL_KEY in your environment to enable."
            )
            return None

        if self.backend_mode == "fal_ai":
            return self._call_fal(prompt, system_prompt=system_prompt, temperature=temperature, max_tokens=max_tokens, timeout=timeout)
        elif self.backend_mode == "anthropic_direct":
            return self._call_anthropic(prompt, system_prompt=system_prompt, temperature=temperature, max_tokens=max_tokens, timeout=timeout)
        return None

    def _fal_openai_request(self, payload: Dict[str, Any], is_openai_compat: bool, target_url: str, timeout: Optional[int] = None) -> "tuple[Optional[str], bool]":
        """Perform ONE fal request. Returns (content, empty_flag):
        - (text, False) on success with content
        - (None, True)  only when HTTP succeeded but content is empty/blank (retryable)
        - (None, False) on any real error (auth/429/5xx/http error/timeout/parse) -> NOT retried
        """
        eff_timeout = timeout if timeout is not None else self._timeout
        headers = {"Authorization": f"Key {self._fal_key}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=eff_timeout) as client:
                res = client.post(target_url, json=payload, headers=headers)
                if res.status_code in (401, 403):
                    logger.error("fal.ai authentication failed (HTTP %d). Check FAL_KEY.", res.status_code)
                    return None, False
                if res.status_code == 429:
                    logger.warning("fal.ai rate limit exceeded (HTTP 429).")
                    return None, False
                if res.status_code >= 500:
                    logger.error("fal.ai server error (HTTP %d): %s", res.status_code, res.text[:200])
                    return None, False
                res.raise_for_status()
                data = res.json()
                if data.get("error"):
                    logger.error("fal.ai error reported in response: %s", data["error"])
                    return None, False
                output = None
                if is_openai_compat:
                    choices = data.get("choices") or []
                    if choices:
                        output = (choices[0].get("message") or {}).get("content")
                if output is None:
                    output = data.get("output")
                if output is not None and str(output).strip():
                    return str(output).strip(), False
                logger.warning("fal.ai returned HTTP success with empty content; will retry once")
                return None, True
        except httpx.TimeoutException:
            logger.warning("fal.ai request timed out after %ds", eff_timeout)
            return None, False
        except httpx.HTTPStatusError as e:
            logger.error("fal.ai HTTP error %s: %s", e.response.status_code, e.response.text[:200])
            return None, False
        except Exception as e:
            logger.error("fal.ai unexpected request failure: %s", str(e))
            return None, False

    def _call_fal(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> Optional[str]:
        """Invoke fal.ai (OpenAI-compatible or Any-LLM), retrying once only on an empty HTTP-200."""
        is_openai_compat = ("openai" in self._endpoint) or ("openrouter" in self._endpoint) or ("chat/completions" in self._endpoint)
        target_url = self._endpoint
        if is_openai_compat and not target_url.endswith("/chat/completions"):
            target_url = f"{target_url}/chat/completions"

        eff_tokens = max_tokens if max_tokens is not None else self._max_tokens
        if is_openai_compat:
            messages: List[Dict[str, str]] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            payload: Dict[str, Any] = {"model": self._model, "messages": messages, "temperature": temperature, "max_tokens": eff_tokens}
        else:
            payload = {"prompt": prompt, "model": self._model, "temperature": temperature, "max_tokens": eff_tokens, "priority": "latency"}
            if system_prompt:
                payload["system_prompt"] = system_prompt

        logger.info("Calling fal.ai Claude Opus endpoint %s with model %s (max_tokens=%d)", target_url, self._model, eff_tokens)
        eff_timeout = timeout if timeout is not None else self._timeout
        content, empty = self._fal_openai_request(payload, is_openai_compat, target_url, timeout=eff_timeout)
        if empty:
            content, _ = self._fal_openai_request(payload, is_openai_compat, target_url, timeout=eff_timeout)  # single retry, same request
        return content

    def generate_multimodal(
        self,
        prompt: str,
        images: List["tuple[str, str]"],
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> Optional[str]:
        """Send a real document (as image data-URL parts) to Claude over the OpenAI-compatible route.

        `images` is a list of (mime, base64) tuples. Only supported when a fal key is configured and
        the endpoint is OpenAI-compatible (which is the path that accepts image_url content). Applies
        the same single retry on an empty HTTP-200. Never logs credentials.
        """
        if not self.available or not self._fal_key or not images:
            return None
        is_openai_compat = ("openai" in self._endpoint) or ("openrouter" in self._endpoint) or ("chat/completions" in self._endpoint)
        if not is_openai_compat:
            logger.warning("generate_multimodal requires an OpenAI-compatible endpoint; current endpoint does not accept image input.")
            return None
        target_url = self._endpoint
        if not target_url.endswith("/chat/completions"):
            target_url = f"{target_url}/chat/completions"
        content_parts: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for mime, b64 in images:
            content_parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        messages: List[Dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": content_parts})
        eff_tokens = max_tokens if max_tokens is not None else self._max_tokens
        payload: Dict[str, Any] = {"model": self._model, "messages": messages, "temperature": 0.0, "max_tokens": eff_tokens}
        logger.info("Sending multimodal document request to fal.ai (%d image part(s)), model %s", len(images), self._model)
        eff_timeout = timeout if timeout is not None else self._timeout
        content, empty = self._fal_openai_request(payload, True, target_url, timeout=eff_timeout)
        if empty:
            content, _ = self._fal_openai_request(payload, True, target_url, timeout=eff_timeout)
        return content

    def _call_anthropic(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> Optional[str]:
        """Invoke Anthropic Messages API directly as an interchangeable fallback."""
        headers = {
            "x-api-key": self._anthropic_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        eff_tokens = max_tokens if max_tokens is not None else self._max_tokens
        eff_timeout = timeout if timeout is not None else self._timeout

        payload: Dict[str, Any] = {
            "model": self._anthropic_model,
            "max_tokens": eff_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            payload["system"] = system_prompt

        logger.info(
            "Calling Anthropic API directly for model %s (max_tokens=%d)",
            self._anthropic_model, eff_tokens,
        )

        try:
            with httpx.Client(timeout=eff_timeout) as client:
                res = client.post(self._anthropic_endpoint, json=payload, headers=headers)
                if res.status_code in (401, 403):
                    logger.error("Anthropic auth failed (HTTP %d). Check ANTHROPIC_API_KEY.", res.status_code)
                    return None
                if res.status_code == 429:
                    logger.warning("Anthropic rate limit exceeded (HTTP 429).")
                    return None
                res.raise_for_status()
                data = res.json()

                content_blocks = data.get("content") or []
                for block in content_blocks:
                    if block.get("type") == "text":
                        return str(block.get("text", "")).strip()

                return None

        except httpx.TimeoutException:
            logger.warning("Anthropic request timed out after %ds", eff_timeout)
            return None
        except Exception as e:
            logger.error("Anthropic request failure: %s", str(e))
            return None

    # ------------------------------------------------------------------
    # Structured JSON query
    # ------------------------------------------------------------------
    def query_json(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        schema_hint: Optional[str] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> Optional[dict]:
        """Query Claude Opus and return parsed structured JSON.

        Handles stripped markdown fences (```json ... ```) and malformed responses.
        """
        json_system = (
            (system_prompt or "You are an expert CPA accountant and financial document analyst.")
            + "\nCRITICAL: Respond STRICTLY with a single valid JSON object. "
            "Do not include any conversational preamble, notes, or explanations outside the JSON."
        )
        if schema_hint:
            prompt = f"{prompt}\n\nRequired JSON Schema / Fields:\n{schema_hint}"

        raw_output = self.generate(
            prompt,
            system_prompt=json_system,
            temperature=0.0,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        if not raw_output:
            return None

        return self._extract_json_object(raw_output)

    @staticmethod
    def _extract_json_object(text: str) -> Optional[dict]:
        """Extract and parse a JSON object from LLM response text."""
        cleaned = text.strip()

        # Strip markdown code blocks ```json ... ```
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        # Regex fallback to extract outermost { ... }
        match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        logger.warning("Failed to parse valid JSON from LLM output: %s", text[:200])
        return None

    # ------------------------------------------------------------------
    # LLMProvider interface implementations
    # ------------------------------------------------------------------
    def classify_document(
        self,
        text: str,
        candidate_types: Iterable[str],
        return_dict: bool = False,
    ) -> Optional[Union[ClassificationResult, Dict[str, Any], str]]:
        """Classify document text into one of the candidate types using Claude Opus."""
        candidates = list(candidate_types)
        if not candidates:
            return None

        truncated_text = text[:MAX_TEXT_CONTEXT_CHARS]
        prompt = (
            "Analyze the following accounting / financial document text and determine its document type.\n\n"
            f"Candidate Types: {json.dumps(candidates)}\n\n"
            f"Document Text:\n\"\"\"\n{truncated_text}\n\"\"\"\n\n"
            "Return a JSON object with this structure:\n"
            "{\n"
            '  "classification": "<exact matching candidate from Candidate Types, or null if uncertain>",\n'
            '  "confidence": <float between 0.0 and 1.0>,\n'
            '  "reasoning": "<brief explanation>"\n'
            "}"
        )

        res = self.query_json(
            prompt=prompt,
            system_prompt="You are an expert CPA auditor and document categorization engine.",
            max_tokens=DEFAULT_SHORT_MAX_TOKENS,
            timeout=self._short_timeout,
        )
        if not res or not isinstance(res, dict):
            return None

        classification = res.get("classification") or res.get("label")
        if not classification or not isinstance(classification, str):
            return None

        try:
            confidence = float(res.get("confidence", 0.0))
        except (ValueError, TypeError):
            confidence = 0.0
        reasoning = str(res.get("reasoning") or "")

        # Validate against candidate list (case-insensitive)
        matched_candidate = None
        for c in candidates:
            if c.lower() == classification.strip().lower():
                matched_candidate = c
                break

        if not matched_candidate:
            return None

        if return_dict:
            return {
                "label": matched_candidate,
                "confidence": confidence,
                "reasoning": reasoning,
            }

        return ClassificationResult(
            label=matched_candidate,
            confidence=confidence,
            reasoning=reasoning,
        )

    def extract_field(self, text: str, field: str) -> Optional[str]:
        """Extract a specific accounting field (e.g. 'tax_year', 'vendor', 'invoice_no')."""
        if not field:
            return None

        truncated_text = text[:MAX_TEXT_CONTEXT_CHARS]
        prompt = (
            f"Extract the exact value of the field '{field}' from the following document text.\n\n"
            f"Document Text:\n\"\"\"\n{truncated_text}\n\"\"\"\n\n"
            "Return a JSON object with this structure:\n"
            "{\n"
            f'  "field": "{field}",\n'
            '  "value": "<extracted value or null if not found>",\n'
            '  "confidence": <float between 0.0 and 1.0>\n'
            "}"
        )

        res = self.query_json(
            prompt=prompt,
            system_prompt="You are an accounting data extraction model. Extract accurate values.",
            max_tokens=DEFAULT_FIELD_MAX_TOKENS,
            timeout=self._short_timeout,
        )
        if not res or not isinstance(res, dict):
            return None

        val = res.get("value")
        if val is None:
            return None
        return str(val).strip()

    def analyze_document(self, text: str, prompt: str) -> Optional[dict]:
        """Analyze ambiguous document cases or accounting semantics.

        Used for period ambiguity, multi-jurisdiction tax issues, missing checklist
        reasons, and near-duplicate resolution.
        """
        truncated_text = text[:MAX_TEXT_CONTEXT_CHARS]
        full_prompt = (
            f"Document Text:\n\"\"\"\n{truncated_text}\n\"\"\"\n\n"
            f"Analysis Request:\n{prompt}\n\n"
            "Return a JSON object with this structure:\n"
            "{\n"
            '  "summary": "<one sentence summary of the document>",\n'
            '  "analysis": "<detailed accounting analysis>",\n'
            '  "confidence": "<high|medium|low>",\n'
            '  "entities": {\n'
            '    "vendor_or_client": "<name or null>",\n'
            '    "period_year": <integer or null>,\n'
            '    "document_type": "<type or null>"\n'
            '  },\n'
            '  "findings": [\n'
            '    {"issue": "<description>", "severity": "<high|medium|low>", "recommendation": "<action>"}\n'
            '  ]\n'
            "}"
        )

        return self.query_json(
            prompt=full_prompt,
            system_prompt="You are an expert CPA auditor specializing in document compliance and anomaly detection.",
        )
