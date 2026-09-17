"""Abstract interfaces / ports for the document-processing engine.

Concrete adapters live in `engine.providers`. Keeping these as ABCs means the
web build (uploaded files, no OCR/LLM) and a future local Windows build
(real file paths, PaddleOCR, Ollama/Qwen) share one ScanEngine without any
change to the detection logic.
"""
from abc import ABC, abstractmethod
from typing import Iterable, List, Optional

from .models import FileRef, ExtractionResult


class FileSource(ABC):
    """Where documents come from (uploads today, local Windows folders later)."""

    @abstractmethod
    def list_files(self) -> List[FileRef]:
        """Return the files to scan. Must not raise on individual bad entries."""

    @abstractmethod
    def open(self, ref: FileRef) -> str:
        """Return a readable LOCAL filesystem path for the given file.

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

    Default (web) build uses a no-op. The local desktop build can plug in
    PaddleOCR here without touching the detection engine.
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
    """Optional local LLM for hard classification/extraction cases only.

    Never used to make decisions and never sends documents to a cloud service.
    Default is a no-op; a local Ollama/Qwen adapter can be plugged in later.
    """

    name: str = "base"

    @property
    @abstractmethod
    def available(self) -> bool:
        ...

    @abstractmethod
    def classify_document(self, text: str, candidate_types: Iterable[str]) -> Optional[str]:
        ...

    @abstractmethod
    def extract_field(self, text: str, field: str) -> Optional[str]:
        ...
