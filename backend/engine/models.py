"""Framework-free data models shared across the engine.

Plain dataclasses so the same objects flow through the current web upload
adapter and the future local Windows (Tauri) adapter without change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


# Extraction status values
STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_NEEDS_OCR = "needs_ocr"
STATUS_PASSWORD = "password"
STATUS_ERROR = "error"

# Exception categories (stable ids used by API + UI)
CATEGORIES = [
    "exact_duplicate",
    "possible_duplicate",
    "missing_doc",
    "wrong_period",
    "wrong_type",
    "unreadable",
]


@dataclass
class FileRef:
    """A reference to a document, independent of where the bytes live."""
    id: str
    name: str
    ext: str
    size: int = 0
    path: Optional[str] = None  # local filesystem path once materialized

    @staticmethod
    def from_path(path: str, file_id: Optional[str] = None) -> "FileRef":
        name = os.path.basename(path)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        return FileRef(id=file_id or path, name=name, ext=ext, size=size, path=path)


@dataclass
class ExtractionResult:
    text: str = ""
    status: str = STATUS_OK
    reason: str = ""
    ocr_used: bool = False
    meta: dict = field(default_factory=dict)
