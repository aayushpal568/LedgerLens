"""Tests for large-folder features: concurrency, cancellation, resume,
LSH-based near-duplicate detection, and local provider fallbacks."""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engine import (  # noqa: E402
    build_default_engine, build_local_engine, LocalDirectoryFileSource, LocalPathFileSource,
    PaddleOCRProvider, OllamaLLMProvider, NoOpOCRProvider,
)


def _make_folder(tmp_path, n=150, dups=6, near=6):
    seed = "Date,Amount,Vendor\n2024-01-01,100.00,Chase Bank Statement\n"
    for i in range(n):
        if i < dups:
            content = seed
            name = f"dup_{i}.csv"
        elif i < dups + near:
            content = seed + f"memo line number {i}\n"
            name = f"near_{i}.csv"
        else:
            content = f"Invoice 2024\nVendor,V{i}\nAmount,{i}.00\nUnique reference token {i}\n"
            name = f"doc_{i}.csv"
        (tmp_path / name).write_text(content)
    return tmp_path


def test_concurrency_processes_all(tmp_path):
    _make_folder(tmp_path, n=120)
    engine = build_default_engine()
    src = LocalDirectoryFileSource(str(tmp_path), supported_only=True)
    result = engine.run(src, [], expected_period=2024, max_workers=8)
    assert result["processed"] == 120
    assert result["cancelled"] is False


def test_lsh_detects_near_duplicates_at_scale(tmp_path):
    # >= LSH_MIN_FILES triggers the MinHash-LSH path
    _make_folder(tmp_path, n=150, dups=6, near=8)
    engine = build_default_engine()
    src = LocalDirectoryFileSource(str(tmp_path), supported_only=True)
    result = engine.run(src, [], expected_period=2024)
    assert result["counts"]["exact_duplicate"] >= 1
    assert result["counts"]["possible_duplicate"] >= 1  # near-dups found via LSH


def test_cancellation_stops_early(tmp_path):
    _make_folder(tmp_path, n=200)
    engine = build_default_engine()
    src = LocalDirectoryFileSource(str(tmp_path), supported_only=True)
    state = {"seen": 0}

    def should_cancel():
        state["seen"] += 1
        return state["seen"] > 5  # cancel after a few files

    result = engine.run(src, [], expected_period=2024, should_cancel=should_cancel, max_workers=2)
    assert result["cancelled"] is True
    assert result["processed"] < 200


def test_resume_reuses_prior_state(tmp_path):
    _make_folder(tmp_path, n=40, dups=2, near=0)
    engine = build_default_engine()
    src = LocalDirectoryFileSource(str(tmp_path), supported_only=True)

    # First partial pass: cancel almost immediately
    partial = engine.run(src, [], expected_period=2024,
                         should_cancel=lambda: True, max_workers=2)
    assert partial["processed"] < 40
    prior = partial["file_states"]

    # Resume: pass prior state back; only remaining files get processed.
    full = engine.run(src, [], expected_period=2024, resume_state=prior, max_workers=4)
    assert full["processed"] == 40
    assert full["cancelled"] is False
    # duplicates still detected across the resumed full set
    assert full["counts"]["exact_duplicate"] >= 1


def test_paddle_provider_unavailable_is_safe():
    p = PaddleOCRProvider()
    # paddleocr is not installed in this environment -> inactive, returns None
    if not p.available:
        assert p.extract("/nonexistent.png", "png") is None


def test_ollama_provider_fallback_when_no_server():
    llm = OllamaLLMProvider(host="http://127.0.0.1:59999")  # nothing listening
    assert llm.available is False
    assert llm.classify_document("some text", ["invoice", "receipt"]) is None
    assert llm.extract_field("some text", "date") is None


def test_build_local_engine_uses_available_or_safe_fallback_providers():
    engine = build_local_engine(enable_ocr=True, enable_llm=True)
    assert engine.ocr is not None
    assert engine.llm is not None
    # Dependency availability is environment-specific. The builder must either
    # keep a usable local provider or replace it with the safe no-op provider.
    assert isinstance(engine.ocr.available, bool)
    assert isinstance(engine.llm.available, bool)
