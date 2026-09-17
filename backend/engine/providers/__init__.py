"""Concrete adapters for the engine interfaces."""

from .file_sources import LocalPathFileSource, LocalDirectoryFileSource
from .extractors import DefaultDocumentExtractor
from .ocr import NoOpOCRProvider, BaiduUnlimitedOCRProvider
from .llm import NoOpLLMProvider, ClaudeOpusFalProvider

__all__ = [
    "LocalPathFileSource",
    "LocalDirectoryFileSource",
    "DefaultDocumentExtractor",
    "NoOpOCRProvider",
    "BaiduUnlimitedOCRProvider",
    "NoOpLLMProvider",
    "ClaudeOpusFalProvider",
]

