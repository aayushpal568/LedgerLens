"""Backward-compatible detection entry point.

Thin wrapper over the ScanEngine so existing callers (the web backend) keep the
same `run_detection(file_records, ...)` call. New code should prefer building a
ScanEngine with explicit adapters. Extra optional kwargs (should_cancel,
resume_state, max_workers) are accepted for large-folder / desktop use.
"""
from .models import CATEGORIES  # re-exported for compatibility
from .scan_engine import build_default_engine, ScanEngine  # noqa: F401

__all__ = ["run_detection", "CATEGORIES"]


def run_detection(file_records, checklist_items, expected_period=None, on_progress=None,
                  should_cancel=None, resume_state=None, max_workers=None):
    from .providers import LocalPathFileSource
    engine = build_default_engine()
    source = LocalPathFileSource(file_records)
    return engine.run(source, checklist_items, expected_period, on_progress,
                      should_cancel=should_cancel, resume_state=resume_state, max_workers=max_workers)
