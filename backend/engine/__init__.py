"""Portable document-processing engine.

Framework- and database-free so it can later be packaged with the desktop
(Tauri) build and run fully locally on a user's Windows computer. Core logic
depends only on abstract ports (FileSource / DocumentExtractor / OCRProvider /
LLMProvider), so the same ScanEngine serves the web upload flow today and a
local Windows folder scan later. No cloud document processing.
"""

from .detect import run_detection
from .checklist import default_templates, SUPPORTED_EXTENSIONS
from .models import FileRef, ExtractionResult, CATEGORIES
from .interfaces import FileSource, DocumentExtractor, OCRProvider, LLMProvider
from .scan_engine import ScanEngine, build_default_engine
from .providers import (
    LocalPathFileSource, LocalDirectoryFileSource, DefaultDocumentExtractor,
    NoOpOCRProvider, PaddleOCRProvider, NoOpLLMProvider, ClaudeOpusFalProvider,
)

__all__ = [
    "run_detection", "default_templates", "SUPPORTED_EXTENSIONS",
    "FileRef", "ExtractionResult", "CATEGORIES",
    "FileSource", "DocumentExtractor", "OCRProvider", "LLMProvider",
    "ScanEngine", "build_default_engine",
    "LocalPathFileSource", "LocalDirectoryFileSource", "DefaultDocumentExtractor",
    "NoOpOCRProvider", "PaddleOCRProvider", "NoOpLLMProvider",
    "ClaudeOpusFalProvider",
]

