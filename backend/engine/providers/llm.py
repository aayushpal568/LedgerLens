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


class OllamaLLMProvider(LLMProvider):
    """Talks to a LOCAL Ollama server only (default http://localhost:11434).

    Local-first by design: nothing leaves the machine. Inactive unless a local
    server responds. Not wired into detection by default.
    """
    name = "ollama"

    def __init__(self, model: str = "qwen2:0.5b", host: str = "http://localhost:11434", timeout: int = 30):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout

    @property
    def available(self) -> bool:
        try:
            import requests
            r = requests.get(f"{self.host}/api/tags", timeout=2)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def _generate(self, prompt: str) -> Optional[str]:
        if not self.available:
            return None
        try:
            import requests
            r = requests.post(
                f"{self.host}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return (r.json() or {}).get("response", "").strip() or None
        except Exception:  # noqa: BLE001
            return None

    def classify_document(self, text: str, candidate_types: Iterable[str]) -> Optional[str]:
        types = list(candidate_types)
        prompt = (
            "You classify an accounting document. Choose exactly one type from this list "
            f"or reply 'unknown'. Types: {types}. Reply with only the type.\n\n"
            f"Document excerpt:\n{text[:1500]}"
        )
        out = self._generate(prompt)
        if not out:
            return None
        out = out.strip().strip('"').lower()
        for t in types:
            if t.lower() in out:
                return t
        return None

    def extract_field(self, text: str, field: str) -> Optional[str]:
        prompt = (
            f"Extract the '{field}' from the accounting document excerpt. "
            "Reply as JSON like {\"value\": \"...\"} or {\"value\": null}.\n\n"
            f"{text[:1500]}"
        )
        out = self._generate(prompt)
        if not out:
            return None
        try:
            return (json.loads(out) or {}).get("value")
        except Exception:  # noqa: BLE001
            return None
