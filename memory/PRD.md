# LedgerLens — Accounting Document Checker (V1)

## Original Problem Statement
Local-first desktop-style app for small accounting firms to scan client document folders and surface only exception files for accountant review. Privacy-first; never deletes/renames/moves/emails or makes autonomous decisions. Windows desktop is the long-term target; V1 built here as a web app on Emergent (React + FastAPI + MongoDB) with a modular, portable document-processing engine so core logic can later be packaged with Tauri to run locally.

## User Choices (confirmed)
- Web-based version for V1.
- Detection = rule-based + SHA-256 hashing + real text extraction. Heavy OCR & LLM intentionally deferred (images flagged as "unreadable / needs OCR").
- Full workflow end-to-end (firm setup, clients, upload workspace, checklist templates, scan, review center, reports).
- Upload-based workspace.
- Architecture: core business logic separated from UI, modular, Tauri-ready for future local desktop packaging.

## Architecture
- **Frontend**: React (CRA) + Tailwind, desktop-style 3-pane shell. Pages: Overview/Firm, Clients, Scan Workspace, Checklist Templates, Review Center, File Diff Inspector, Reports. Theme: teal/slate, Outfit/Inter/JetBrains Mono, light+dark.
- **Backend**: FastAPI (`/app/backend/server.py`), all routes under `/api`.
- **Engine** (portable, framework-free `/app/backend/engine/`), refactored into clean ports + adapters for the future Windows/Tauri local build:
  - `models.py` — FileRef, ExtractionResult, category/status constants.
  - `interfaces.py` — abstract ports: **FileSource, DocumentExtractor, OCRProvider, LLMProvider**.
  - `scan_engine.py` — **ScanEngine** core (hashing, dup/possible-dup, checklist/missing, wrong-period, wrong-type, unreadable). Reusable locally. `build_default_engine()` = web defaults (NoOp OCR/LLM).
  - `providers/file_sources.py` — `LocalPathFileSource` (web temp files) + `LocalDirectoryFileSource` (walks a real Windows folder tree; nested; safe skipping).
  - `providers/extractors.py` — `DefaultDocumentExtractor` (pdf/docx/xlsx/csv; routes images to OCR when a provider is available).
  - `providers/ocr.py` — `NoOpOCRProvider` (default) + `PaddleOCRProvider` (inactive local-only stub, lazy import).
  - `providers/llm.py` — `NoOpLLMProvider` (default) + `OllamaLLMProvider` (inactive local-only stub, talks to localhost Ollama only).
  - `detect.py` — `run_detection(...)` kept backward-compatible (delegates to ScanEngine via LocalPathFileSource); `extract.py`, `hashing.py`, `similarity.py`, `checklist.py` unchanged low-level helpers; `report.py` (CSV/XLSX/PDF).
  - Tests: `tests/test_engine.py` (11 passing) cover FileSource, extractor, pluggable OCR, NoOp providers, ScanEngine detections, backward-compat, safe skip.
  - No cloud document processing; documents never sent to external AI services.
  - NOTE: PaddleOCR/Ollama adapters are scaffolding only — NOT validated in a real Windows environment.
- **Storage**: MongoDB for metadata; Emergent object storage for file bytes (`storage.py`). Scan downloads objects to a temp path so the engine reads local files (keeps it portable for Tauri).

## Detection Categories
exact_duplicate (hash), possible_duplicate (text similarity ≥0.82), missing_doc (checklist unmatched), wrong_period (year mismatch vs expected), wrong_type (ext not in item allowed types), unreadable (needs_ocr / password / corrupted / empty).

## Implemented (2026-06)
- Firm setup, Client CRUD, upload/list/soft-delete files (object storage).
- 3 seeded checklist templates + full template CRUD with alias/rule/type editor.
- Background scan with live progress polling, safe skipping, partial-scan messaging.
- Review Center: category cards, filters, evidence inspector, review actions + notes.
- File Diff Inspector (side-by-side metadata diff).
- Report export CSV / Excel / PDF.
- Tested: backend 100%, frontend 100% (iteration_1).

## Boundaries (never)
Never deletes/renames/moves/reorganizes files; never emails/contacts clients; never makes tax/accounting/compliance decisions; never acts on AI confidence alone.

## Local / Windows-readiness (2026-06, iteration 3)
- **ScanEngine scaled**: concurrent hash+extract (thread pool), cooperative cancellation (`should_cancel`), resume (`resume_state`/`file_states`), MinHash-LSH near-duplicate detection with hard caps (`MAX_DUP_COMPARISONS=200k`, `MAX_POSSIBLE_DUP_FINDINGS=1000`), text truncation for memory. Benchmarked 10k/50k/100k — linear time, <800MB at 100k (see `desktop/BENCHMARKS.md`).
- **LocalDirectoryFileSource**: recursive real-folder scan, supported-file filtering, safe skip of unreadable entries — wired to ScanEngine and unit-tested.
- **Web cancel**: `POST /api/scans/{id}/cancel` (in-memory CANCEL_REQUESTS) → scan ends as `cancelled` with partial results.
- **PaddleOCRProvider**: real CPU-first adapter (images + scanned-PDF rasterization via PyMuPDF), lazy imports, optional. Code complete; **NOT validated on Windows / paddle not installed here**.
- **OllamaLLMProvider**: real localhost Ollama adapter with availability check, timeout, safe fallback. Fallback path tested; **real model run NOT validated**.
- **Tauri scaffold** in `/app/desktop/` (tauri.conf.json, Cargo.toml, main.rs sidecar, capabilities, README). **Not built** — needs Windows + Rust/Tauri toolchain + PyInstaller.
- Tests: `backend/tests/test_engine.py` + `test_engine_scale.py` (18 passing).

## Local data store + folder picker (2026-06, iteration 4)
- **SQLite store** (`backend/sqlite_store.py`): Mongo-compatible async adapter (find/insert/update/delete/count, projections, sort, `$ne`). Selected via env `DATA_BACKEND=sqlite` + `SQLITE_PATH`; default stays `mongo` so the web build is unchanged. Unit-tested (9) and verified running the full FastAPI app end-to-end.
- **Local folder scan**: `POST /api/clients/{id}/scan-local` scans a real folder in place via `LocalDirectoryFileSource` + `build_local_engine()` — no upload/copy. Shared `_finalize_scan`/`_make_progress` helpers (refactor; `_run_scan` behavior unchanged).
- **Desktop folder-picker UI**: Tauri-gated "Scan a Local Folder" card in Scan Workspace (`window.__TAURI__.dialog`), hidden in the web build. `withGlobalTauri` enabled in tauri.conf.
- Still Windows-only-pending: Tauri build/packaging, real PaddleOCR/Ollama, WebView2/PyInstaller.

## Backlog / Next (P1/P2)
