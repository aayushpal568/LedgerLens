"""Optional local LLM providers.

Used ONLY for hard classification/field-extraction hints — never to make
decisions, and never against a cloud API. Default is a no-op. `OllamaLLMProvider`
targets a LOCAL Ollama server (e.g. a small Qwen model) for the desktop build
and stays inactive (available=False) unless a local server is reachable. Not
yet validated in a Windows environment.
"""
import json
from typing import Iterable, Optional

from ..interfaces import LLMProvider


class NoOpLLMProvider(LLMProvider):
    name = "noop"

    @property
    def available(self) -> bool:
        return False

    def classify_document(self, text: str, candidate_types: Iterable[str]) -> Optional[str]:
        return None

    def extract_field(self, text: str, field: str) -> Optional[str]:
        return None

