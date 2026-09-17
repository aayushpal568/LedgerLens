"""Claude Opus LLM provider via fal.ai.

Integrates Claude Opus through fal.ai's text inference API (`fal-ai/any-llm`)
behind the modular LedgerLens `LLMProvider` interface. Also supports direct
Anthropic API routing as an interchangeable fallback.

Credentials and models are loaded strictly from environment variables:
  - FAL_KEY / FAL_API_KEY: fal.ai API access token
  - FAL_ENDPOINT / FAL_LLM_ENDPOINT: fal.ai endpoint (default: https://fal.run/fal-ai/any-llm)
  - CLAUDE_MODEL: Claude model on fal.ai (default: anthropic/claude-3-opus)
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

logger = logging.getLogger("ledgerlens.llm")

DEFAULT_FAL_ENDPOINT = "https://fal.run/fal-ai/any-llm"
DEFAULT_CLAUDE_MODEL = "anthropic/claude-3-opus"
DEFAULT_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
DEFAULT_ANTHROPIC_MODEL = "claude-3-opus-20240229"
DEFAULT_TIMEOUT = 60
DEFAULT_MAX_TOKENS = 4096
MAX_TEXT_CONTEXT_CHARS = 16000  # Cap input text to avoid token limits


def _mask_secret(val: Optional[str]) -> str:
    """Safely mask API keys for logging and diagnostic summaries."""
    if not val:
        return "none"
    val = val.strip()
    if len(val) <= 6:
        return "***"
    return f"{val[:3]}...{val[-3:]}"


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

    def classify_document(self, text: str, candidate_types: Iterable[str]) -> Optional[str]:
        return None

    def extract_field(self, text: str, field: str) -> Optional[str]:
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
            return self._call_fal(prompt, system_prompt=system_prompt, temperature=temperature)
        elif self.backend_mode == "anthropic_direct":
            return self._call_anthropic(prompt, system_prompt=system_prompt, temperature=temperature)
        return None

    def _call_fal(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.0,
    ) -> Optional[str]:
        """Invoke fal.ai Any-LLM inference endpoint."""
        headers = {
            "Authorization": f"Key {self._fal_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "prompt": prompt,
            "model": self._model,
            "temperature": temperature,
            "max_tokens": self._max_tokens,
            "priority": "latency",
        }
        if system_prompt:
            payload["system_prompt"] = system_prompt

        logger.info(
            "Calling fal.ai Claude Opus endpoint %s with model %s (max_tokens=%d)",
            self._endpoint, self._model, self._max_tokens,
        )

        try:
            with httpx.Client(timeout=self._timeout) as client:
                res = client.post(self._endpoint, json=payload, headers=headers)
                if res.status_code == 401 or res.status_code == 403:
                    logger.error("fal.ai authentication failed (HTTP %d). Check FAL_KEY.", res.status_code)
                    return None
                if res.status_code == 429:
                    logger.warning("fal.ai rate limit exceeded (HTTP 429).")
                    return None
                if res.status_code >= 500:
                    logger.error("fal.ai server error (HTTP %d): %s", res.status_code, res.text[:200])
                    return None

                res.raise_for_status()
                data = res.json()

                if data.get("error"):
                    logger.error("fal.ai error reported in response: %s", data["error"])
                    return None

                output = data.get("output")
                if output is not None:
                    return str(output).strip()

                logger.warning("fal.ai response did not contain 'output' field: %s", list(data.keys()))
                return None

        except httpx.TimeoutException:
            logger.warning("fal.ai request timed out after %ds", self._timeout)
            return None
        except httpx.HTTPStatusError as e:
            logger.error("fal.ai HTTP error %s: %s", e.response.status_code, e.response.text[:200])
            return None
        except Exception as e:
            logger.error("fal.ai unexpected request failure: %s", str(e))
            return None

    def _call_anthropic(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.0,
    ) -> Optional[str]:
        """Invoke Anthropic Messages API directly as an interchangeable fallback."""
        headers = {
            "x-api-key": self._anthropic_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": self._anthropic_model,
            "max_tokens": self._max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            payload["system"] = system_prompt

        logger.info(
            "Calling Anthropic API directly for model %s (max_tokens=%d)",
            self._anthropic_model, self._max_tokens,
        )

        try:
            with httpx.Client(timeout=self._timeout) as client:
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
            logger.warning("Anthropic request timed out after %ds", self._timeout)
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

        raw_output = self.generate(prompt, system_prompt=json_system, temperature=0.0)
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
    def classify_document(self, text: str, candidate_types: Iterable[str]) -> Optional[str]:
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
        )
        if not res or not isinstance(res, dict):
            return None

        classification = res.get("classification")
        if not classification or not isinstance(classification, str):
            return None

        # Validate against candidate list (case-insensitive)
        for c in candidates:
            if c.lower() == classification.strip().lower():
                return c

        return None

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
