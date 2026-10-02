"""Abstract interfaces / ports for the document-processing engine.

Concrete adapters live in `engine.providers`. Keeping these as ABCs ensures the
engine supports interchangeable storage sources, OCR providers (e.g. local PaddleOCR),
and LLM providers (e.g. Claude Opus) without modifying detection logic.
"""
from abc import ABC, abstractmethod
from typing import Any, Iterable, List, Optional

from .models import FileRef, ExtractionResult


class FileSource(ABC):
    """Source of documents to be processed by the engine."""

    @abstractmethod
    def list_files(self) -> List[FileRef]:
        """Return the files to scan. Must not raise on individual bad entries."""

    @abstractmethod
    def open(self, ref: FileRef) -> str:
        """Return a readable filesystem path for the given file.

        May raise (locked/inaccessible) — the ScanEngine will skip safely.
        """

    def cleanup(self) -> None:
        """Optional: release any temp resources."""
        return None


class DocumentExtractor(ABC):
    """Turns a local file into text + a status. May consult an OCRProvider."""

    @abstractmethod
    def extract(self, path: str, ext: str) -> ExtractionResult:
        ...


class OCRProvider(ABC):
    """Optical character recognition for images / scanned PDFs.

    Uses local PaddleOCR on the user machine without external API dependencies,
    or a graceful no-op when unconfigured.
    """

    name: str = "base"

    @property
    @abstractmethod
    def available(self) -> bool:
        ...

    @abstractmethod
    def extract(self, path: str, ext: str) -> Optional[str]:
        """Return recognized text, or None if OCR is unavailable/failed."""


class LLMProvider(ABC):
    """LLM provider for ambiguous classification and extraction cases.

    Deterministic processing stays local. Ambiguous cases may send document text
    to the configured external LLM provider (e.g. Claude Opus via fal.ai).
    """

    name: str = "base"

    @property
    @abstractmethod
    def available(self) -> bool:
        ...

    @abstractmethod
    def classify_document(self, text: str, candidate_types: Iterable[str]) -> Optional[Any]:
        ...

    @abstractmethod
    def extract_field(self, text: str, field: str) -> Optional[str]:
        ...

    def analyze_document(self, text: str, prompt: str) -> Optional[dict]:
        """Analyze ambiguous document cases or accounting semantics."""
        return None
