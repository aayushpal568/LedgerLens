"""Unit tests for Claude Opus LLM provider (fal.ai inference & Anthropic fallback).

Validates:
1. Provider initialization and safe unconfigured state
2. Credential loading and secret masking
3. Prompt construction with extracted/OCR document text
4. Structured JSON response parsing (including markdown fences and embedded objects)
5. Document classification across candidate types
6. Field extraction (invoice numbers, tax periods, vendors)
7. Ambiguous case semantic analysis
8. Error handling: 401, 429, 500, network timeouts, malformed JSON
9. Direct Anthropic API fallback routing
10. Engine integration via build_default_engine
"""
import json
import os
from unittest.mock import patch, MagicMock

import httpx
import pytest

from engine.providers.llm import (
    ClaudeOpusFalProvider,
    NoOpLLMProvider,
    DEFAULT_FAL_ENDPOINT,
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_ANTHROPIC_ENDPOINT,
    DEFAULT_ANTHROPIC_MODEL,
)
from engine.scan_engine import build_default_engine


def test_provider_initialization_defaults_unconfigured():
    """Ensure provider initializes safely with no keys and returns available=False."""
    with patch.dict(os.environ, {}, clear=True):
        provider = ClaudeOpusFalProvider()
        assert provider.available is False
        assert provider.backend_mode == "unconfigured"
        assert provider._endpoint == DEFAULT_FAL_ENDPOINT
        assert provider._model == DEFAULT_CLAUDE_MODEL

        # Methods must fail gracefully without throwing
        assert provider.generate("test prompt") is None
        assert provider.query_json("test json") is None
        assert provider.classify_document("Some invoice text", ["Invoice", "Bank Statement"]) is None
        assert provider.extract_field("Some invoice text", "tax_year") is None
        assert provider.analyze_document("Ambiguous document text", "What is the period?") is None


def test_provider_configuration_from_env_and_masking():
    """Ensure configuration is picked up from env and secrets are masked."""
    env = {
        "FAL_KEY": "fal_sec_live_998877665544332211aabbcc",
        "FAL_ENDPOINT": "https://fal.run/fal-ai/any-llm",
        "CLAUDE_MODEL": "anthropic/claude-3-opus",
        "ANTHROPIC_API_KEY": "sk-ant-api03-abcdef1234567890abcdef",
        "FAL_LLM_TIMEOUT": "45",
        "FAL_LLM_MAX_TOKENS": "2048",
    }
    with patch.dict(os.environ, env, clear=True):
        provider = ClaudeOpusFalProvider()
        assert provider.available is True
        assert provider.backend_mode == "fal_ai"

        summary = provider.get_config_summary()
        assert summary["available"] == "True"
        assert summary["backend_mode"] == "fal_ai"
        assert summary["fal_endpoint"] == "https://fal.run/fal-ai/any-llm"
        assert summary["fal_model"] == "anthropic/claude-3-opus"
        assert summary["timeout"] == "45"
        assert summary["max_tokens"] == "2048"

        # Check secret masking: full keys must NOT be present
        assert "fal_sec_live_998877665544332211aabbcc" not in summary["fal_key_masked"]
        assert summary["fal_key_masked"] == "fal...bcc"
        assert "sk-ant-api03-abcdef1234567890abcdef" not in summary["anthropic_key_masked"]
        assert summary["anthropic_key_masked"] == "sk-...def"


def test_classify_document_success():
    """Verify document classification with extracted text and structured JSON."""
    provider = ClaudeOpusFalProvider(
        api_key="fal_test_key_12345",
        model="anthropic/claude-3-opus",
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "output": json.dumps({
            "classification": "Form 1040 Tax Return",
            "confidence": 0.98,
            "reasoning": "Document contains U.S. Individual Income Tax Return Form 1040 for year 2024",
        }),
    }

    candidates = ["Bank Statement", "Form 1040 Tax Return", "Vendor Invoice", "W-2 Form"]
    sample_text = "Department of the Treasury - Internal Revenue Service\nU.S. Individual Income Tax Return 2024\nForm 1040"

    with patch("httpx.Client.post", return_value=mock_response) as mock_post:
        result = provider.classify_document(sample_text, candidates)
        assert result == "Form 1040 Tax Return"

        # Verify request structure
        call_args = mock_post.call_args
        assert call_args is not None
        headers = call_args.kwargs["headers"]
        assert headers["Authorization"] == "Key fal_test_key_12345"
        payload = call_args.kwargs["json"]
        content = payload.get("prompt") or payload.get("messages", [{}])[-1].get("content", "")
        assert "Form 1040" in content


def test_classify_document_markdown_fenced_json():
    """Verify handling of responses wrapped in markdown code fences."""
    provider = ClaudeOpusFalProvider(api_key="fal_test_key_12345")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "output": "```json\n{\n  \"classification\": \"Vendor Invoice\",\n  \"confidence\": 0.95,\n  \"reasoning\": \"Invoice number and balance due detected\"\n}\n```",
    }

    with patch("httpx.Client.post", return_value=mock_response):
        result = provider.classify_document("INVOICE #9821\nAmount Due: $450.00", ["Vendor Invoice", "Receipt"])
        assert result == "Vendor Invoice"


def test_extract_field_success():
    """Verify extracting specific accounting fields."""
    provider = ClaudeOpusFalProvider(api_key="fal_test_key_12345")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "output": json.dumps({
            "field": "tax_year",
            "value": "2024",
            "confidence": 0.99,
        }),
    }

    with patch("httpx.Client.post", return_value=mock_response):
        val = provider.extract_field("Client financial statement for tax year ended December 31, 2024", "tax_year")
        assert val == "2024"


def test_analyze_document_ambiguous_case():
    """Verify semantic document analysis for ambiguous cases."""
    provider = ClaudeOpusFalProvider(api_key="fal_test_key_12345")

    analysis_payload = {
        "summary": "Ambiguous utility payment receipt with two conflicting year stamps (2023 and 2024).",
        "analysis": "The service period covers Nov 2023 to Jan 2024, but billing was executed in Feb 2024.",
        "confidence": "high",
        "entities": {
            "vendor_or_client": "Pacific Electric",
            "period_year": 2024,
            "document_type": "Utility Bill",
        },
        "findings": [
            {
                "issue": "Multi-year period crossing fiscal year boundary",
                "severity": "medium",
                "recommendation": "Accrue expenses proportionately between FY2023 and FY2024.",
            }
        ],
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"output": json.dumps(analysis_payload)}

    with patch("httpx.Client.post", return_value=mock_response):
        result = provider.analyze_document(
            text="Pacific Electric Bill\nService: 11/15/2023 - 01/15/2024\nBill Date: 02/01/2024",
            prompt="Determine which fiscal year this utility expense belongs to.",
        )
        assert result is not None
        assert result["entities"]["period_year"] == 2024
        assert len(result["findings"]) == 1
        assert result["findings"][0]["severity"] == "medium"


def test_direct_anthropic_fallback_routing():
    """Verify routing to direct Anthropic Messages API when only Anthropic key is configured."""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test-999"}, clear=True):
        provider = ClaudeOpusFalProvider()
        assert provider.available is True
        assert provider.backend_mode == "anthropic_direct"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"classification": "Bank Statement", "confidence": 0.99}),
                }
            ]
        }

        with patch("httpx.Client.post", return_value=mock_response) as mock_post:
            result = provider.classify_document("Bank of America monthly statement", ["Bank Statement", "Invoice"])
            assert result == "Bank Statement"

            # Check Anthropic headers
            call_args = mock_post.call_args
            assert call_args.kwargs["headers"]["x-api-key"] == "sk-ant-test-999"
            assert call_args.kwargs["headers"]["anthropic-version"] == "2023-06-01"


def test_http_401_authentication_failure():
    """Verify HTTP 401 error is handled gracefully without crashing."""
    provider = ClaudeOpusFalProvider(api_key="invalid_fal_key")

    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = '{"detail": "Unauthorized: invalid API key"}'

    with patch("httpx.Client.post", return_value=mock_response):
        res = provider.generate("Test prompt")
        assert res is None


def test_http_429_rate_limit_handled():
    """Verify HTTP 429 rate limit is handled gracefully."""
    provider = ClaudeOpusFalProvider(api_key="fal_test_key")

    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.text = '{"detail": "Rate limit exceeded"}'

    with patch("httpx.Client.post", return_value=mock_response):
        res = provider.generate("Test prompt")
        assert res is None


def test_http_500_server_error_handled():
    """Verify HTTP 500 server error is handled cleanly."""
    provider = ClaudeOpusFalProvider(api_key="fal_test_key")

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"

    with patch("httpx.Client.post", return_value=mock_response):
        res = provider.generate("Test prompt")
        assert res is None


def test_network_timeout_handled_cleanly():
    """Verify network timeout does not crash the application."""
    provider = ClaudeOpusFalProvider(api_key="fal_test_key", timeout=1)

    with patch("httpx.Client.post", side_effect=httpx.TimeoutException("Read timed out")):
        res = provider.generate("Test prompt")
        assert res is None


def test_malformed_json_response_handled_cleanly():
    """Verify invalid/unparseable JSON from model returns None cleanly."""
    provider = ClaudeOpusFalProvider(api_key="fal_test_key")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "output": "This is plain conversational text without any valid JSON.",
    }

    with patch("httpx.Client.post", return_value=mock_response):
        res = provider.query_json("Extract tax details")
        assert res is None


def test_build_default_engine_wires_claude_provider():
    """Verify build_default_engine initializes ClaudeOpusFalProvider as default llm."""
    with patch.dict(os.environ, {}, clear=True):
        engine = build_default_engine()
        assert isinstance(engine.llm, ClaudeOpusFalProvider)
        # Without credentials, it safely reports available=False
        assert engine.llm.available is False
