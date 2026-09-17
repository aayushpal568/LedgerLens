"""Concrete adapters for the engine interfaces."""

from .file_sources import LocalPathFileSource, LocalDirectoryFileSource
from .extractors import DefaultDocumentExtractor
from .ocr import NoOpOCRProvider
from .llm import NoOpLLMProvider

__all__ = [
    "LocalPathFileSource",
    "LocalDirectoryFileSource",
    "DefaultDocumentExtractor",
    "NoOpOCRProvider",
    "NoOpLLMProvider",
]

