"""ScanEngine: the reusable core.

Orchestrates hashing, extraction (via a DocumentExtractor that may use OCR),
duplicate detection, checklist matching, missing-document, wrong-period and
wrong-type checks. Depends only on abstract ports, so the same engine runs
against uploaded files today and local Windows folders later.

Scales to large folders:
- Concurrent hash+extract pass (thread pool, I/O bound).
- Cooperative cancellation and resume (via a reusable file-state cache).
- Near-duplicate detection uses MinHash LSH when available (O(n) buckets)
  instead of O(n^2) pairwise, with an exact-similarity confirmation step.

No cloud processing. Documents are never sent to any external service.
"""
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import combinations
from typing import Callable, Optional

from .hashing import sha256_of_file
from .checklist import detect_period, match_item
from .similarity import normalize, similarity, token_set
from .models import CATEGORIES, STATUS_OK
from .interfaces import FileSource, DocumentExtractor, OCRProvider, LLMProvider

SIMILARITY_THRESHOLD = 0.82
LSH_MIN_FILES = 60            # below this, exact pairwise is cheap enough
LSH_NUM_PERM = 64
LSH_THRESHOLD = 0.8          # bucket threshold; exact confirm still applies
MAX_TEXT_CHARS = 8000         # cap stored text to bound memory on huge folders
MAX_DUP_COMPARISONS = 200000  # hard bound on pairwise similarity checks
MAX_POSSIBLE_DUP_FINDINGS = 1000  # stop emitting once this many are found
UNREADABLE_STATUSES = {"needs_ocr", "password", "error", "empty"}
UNREADABLE_LABEL = {
    "needs_ocr": "Scanned image — OCR required",
    "password": "Password-protected file",
    "error": "Corrupted / unreadable file",
    "empty": "Empty / no extractable content",
}


def _level(conf: int) -> str:
    if conf >= 90:
        return "high"
    if conf >= 60:
        return "medium"
    return "low"


def _file_view(f: dict) -> dict:
    return {
        "file_id": f["id"],
        "name": f["name"],
        "ext": f["ext"].upper(),
        "size": f["size"],
        "sha256": f.get("sha256", ""),
        "period": f.get("period"),
        "status": f.get("status"),
    }


class ScanEngine:
    def __init__(self, extractor: DocumentExtractor,
                 ocr_provider: Optional[OCRProvider] = None,
                 llm_provider: Optional[LLMProvider] = None,
                 max_workers: Optional[int] = None):
        self.extractor = extractor
        self.ocr = ocr_provider
        self.llm = llm_provider  # held for future hard-case hints; not decision-making
        self.max_workers = max_workers or min(16, (os.cpu_count() or 4) * 2)

    # ------------------------------ per-file ------------------------------
    def _process_file(self, source: FileSource, ref, should_cancel=None):
        """Hash + extract a single file. Returns (file_dict, skip_or_None)."""
        f = {"id": ref.id, "name": ref.name, "ext": (ref.ext or "").lower(), "size": ref.size}
        try:
            local_path = source.open(ref)
            f["sha256"] = sha256_of_file(local_path)
        except Exception:  # noqa: BLE001 - locked/inaccessible; skip safely
            f.update({"sha256": "", "status": "error", "text": "",
                      "norm_name": normalize(f["name"].rsplit(".", 1)[0]), "period": {"year": None, "month": None}})
            return f, {"name": f["name"], "reason": "Locked or inaccessible file"}

        try:
            res = self.extractor.extract(local_path, f["ext"], should_cancel=should_cancel)
            text = (res.text or "")[:MAX_TEXT_CHARS]
            f.update({"text": text, "status": res.status, "reason": res.reason, "ocr_used": res.ocr_used})
        except Exception:  # noqa: BLE001 - extraction must never crash the scan
            f.update({"text": "", "status": "error", "reason": "Extraction failed", "ocr_used": False})
        f["norm_name"] = normalize(f["name"].rsplit(".", 1)[0])
        f["period"] = detect_period(f["name"], f.get("text", ""))
        return f, None

    # -------------------------------- run --------------------------------
    def run(self, source: FileSource, checklist_items=None, expected_period=None,
            on_progress: Optional[Callable] = None,
            should_cancel: Optional[Callable[[], bool]] = None,
            resume_state: Optional[dict] = None,
            max_workers: Optional[int] = None) -> dict:
        refs = source.list_files()
        total = len(refs)
        resume_state = resume_state or {}
        files = []
        skipped = []
        cancelled = False

        # Reuse already-processed files (resume) and figure out what's left.
        pending = []
        for ref in refs:
            cached = resume_state.get(ref.id)
            if cached:
                files.append(cached)
                if cached.get("status") == "error":
                    skipped.append({"name": cached["name"], "reason": cached.get("reason", "Skipped")})
            else:
                pending.append(ref)

        processed = len(files)
        if on_progress and processed:
            on_progress(processed, total, None)

        workers = max_workers or self.max_workers
        if pending:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                should_cancel_fn = should_cancel
                futures = {pool.submit(self._process_file, source, ref, should_cancel_fn): ref for ref in pending}
                try:
                    for fut in as_completed(futures):
                        f, skip = fut.result()
                        files.append(f)
                        processed += 1
                        if skip:
                            skipped.append(skip)
                        if on_progress:
                            on_progress(processed, total, skip)
                        if should_cancel and should_cancel():
                            cancelled = True
                            break
                finally:
                    if cancelled:
                        for fut in futures:
                            fut.cancel()

        findings = []
        findings += self._exact_duplicates(files)
        findings += self._possible_duplicates(files)
        findings += self._checklist_checks(files, checklist_items)
        findings += self._wrong_period(files, expected_period)
        findings += self._unreadable(files)

        counts = {c: 0 for c in CATEGORIES}
        for fnd in findings:
            counts[fnd["category"]] += 1

        # file_states enables true resume by the caller (persist + pass back).
        file_states = {f["id"]: f for f in files}
        return {
            "findings": findings, "counts": counts, "skipped": skipped,
            "processed": processed, "total": total, "cancelled": cancelled,
            "file_states": file_states,
        }

    # ------------------------------ checks ------------------------------
    def _exact_duplicates(self, files):
        out = []
        by_hash = {}
        for f in files:
            if f.get("sha256"):
                by_hash.setdefault(f["sha256"], []).append(f)
        for h, group in by_hash.items():
            if len(group) > 1:
                out.append({
                    "category": "exact_duplicate",
                    "title": f"{len(group)} identical copies of \"{group[0]['name']}\"",
                    "confidence": 100, "confidence_level": "high",
                    "files": [_file_view(f) for f in group],
                    "evidence": {"summary": "Files are byte-for-byte identical (matching SHA-256 hash).", "sha256": h},
                })
        return out

    def _possible_duplicates(self, files):
        textual = [f for f in files if f.get("status") == STATUS_OK and f.get("text")]
        if len(textual) >= LSH_MIN_FILES:
            candidate_pairs = self._lsh_candidate_pairs(textual)
            if candidate_pairs is not None:
                return self._confirm_pairs(candidate_pairs)
        # Small folders (or LSH unavailable): exact pairwise
        pairs = combinations(textual, 2)
        return self._confirm_pairs(pairs)

    def _confirm_pairs(self, pairs):
        out = []
        seen = set()
        comparisons = 0
        for a, b in pairs:
            if comparisons >= MAX_DUP_COMPARISONS or len(out) >= MAX_POSSIBLE_DUP_FINDINGS:
                break
            if a.get("sha256") and a["sha256"] == b.get("sha256"):
                continue
            key = tuple(sorted((a["id"], b["id"])))
            if key in seen:
                continue
            seen.add(key)
            comparisons += 1
            sim = similarity(a["text"], b["text"])
            if sim >= SIMILARITY_THRESHOLD:
                conf = int(round(sim * 100))
                out.append({
                    "category": "possible_duplicate",
                    "title": f"\"{a['name']}\" looks similar to \"{b['name']}\"",
                    "confidence": conf, "confidence_level": _level(conf),
                    "files": [_file_view(a), _file_view(b)],
                    "evidence": {"summary": f"Document contents are {conf}% similar but the files are not identical.", "similarity": conf},
                })
        return out

    def _lsh_candidate_pairs(self, textual):
        """Return an iterable of (a, b) candidate pairs via MinHash LSH, or None."""
        try:
            from datasketch import MinHash, MinHashLSH
        except Exception:  # noqa: BLE001
            return None
        lsh = MinHashLSH(threshold=LSH_THRESHOLD, num_perm=LSH_NUM_PERM)
        store = {}
        for f in textual:
            m = MinHash(num_perm=LSH_NUM_PERM)
            for tok in token_set(f["text"]):
                m.update(tok.encode("utf-8"))
            key = str(f["id"])
            lsh.insert(key, m)
            store[key] = (f, m)
        pairs = []
        seen = set()
        for key, (f, m) in store.items():
            if len(pairs) >= MAX_DUP_COMPARISONS:
                break
            for cand in lsh.query(m):
                if cand == key:
                    continue
                pk = tuple(sorted((key, cand)))
                if pk in seen:
                    continue
                seen.add(pk)
                pairs.append((f, store[cand][0]))
                if len(pairs) >= MAX_DUP_COMPARISONS:
                    break
        return pairs

    def _checklist_checks(self, files, checklist_items):
        out = []
        for item in (checklist_items or []):
            matched = [f for f in files if match_item(item, f.get("norm_name", ""), f.get("text", ""))]
            if not matched:
                out.append({
                    "category": "missing_doc",
                    "title": f"Possibly missing: {item['name']}",
                    "confidence": 80, "confidence_level": "medium", "files": [],
                    "evidence": {
                        "summary": f"No uploaded file matched the checklist item \"{item['name']}\".",
                        "aliases": item.get("aliases", []), "checklist_item": item["name"],
                    },
                })
                continue
            allowed = [t.upper() for t in item.get("allowed_types", [])]
            if allowed:
                for f in matched:
                    if f["ext"].upper() not in allowed:
                        out.append({
                            "category": "wrong_type",
                            "title": f"\"{f['name']}\" may be the wrong file type for {item['name']}",
                            "confidence": 70, "confidence_level": "medium",
                            "files": [_file_view(f)],
                            "evidence": {
                                "summary": f"Matched checklist item \"{item['name']}\" expects {', '.join(allowed)} but file is .{f['ext'].upper()}.",
                                "expected_types": allowed, "actual_type": f["ext"].upper(), "checklist_item": item["name"],
                            },
                        })
        return out

    def _wrong_period(self, files, expected_period):
        out = []
        if not expected_period:
            return out
        for f in files:
            yr = (f.get("period") or {}).get("year")
            if yr and int(yr) != int(expected_period):
                out.append({
                    "category": "wrong_period",
                    "title": f"\"{f['name']}\" appears to be from {yr}, not {expected_period}",
                    "confidence": 75, "confidence_level": "medium",
                    "files": [_file_view(f)],
                    "evidence": {
                        "summary": f"Detected period {yr} does not match the expected period {expected_period}.",
                        "detected_year": yr, "expected_year": int(expected_period),
                    },
                })
        return out

    def _unreadable(self, files):
        out = []
        for f in files:
            if f.get("status") in UNREADABLE_STATUSES:
                reason = f.get("reason") or UNREADABLE_LABEL.get(f.get("status"), "Unreadable")
                out.append({
                    "category": "unreadable",
                    "title": f"\"{f['name']}\" could not be read",
                    "confidence": 90, "confidence_level": "high",
                    "files": [_file_view(f)],
                    "evidence": {"summary": reason, "status": f.get("status")},
                })
        return out


def build_default_engine() -> ScanEngine:
    """Web build: text extraction only, OCR/LLM disabled (no-op providers)."""
    from .providers import DefaultDocumentExtractor, NoOpOCRProvider, NoOpLLMProvider
    ocr = NoOpOCRProvider()
    return ScanEngine(DefaultDocumentExtractor(ocr), ocr, NoOpLLMProvider())


def build_local_engine(enable_ocr: bool = True, enable_llm: bool = False,
                       ocr_lang: str = "en", ollama_model: str = "qwen2:0.5b") -> ScanEngine:
    """Local desktop build: activates PaddleOCR / Ollama when available.

    Falls back to no-op providers automatically if the local dependency/server
    is not present, so this is always safe to call. Real OCR/LLM execution has
    NOT been validated on Windows yet.
    """
    from .providers import (
        DefaultDocumentExtractor, NoOpOCRProvider, PaddleOCRProvider,
        NoOpLLMProvider, OllamaLLMProvider,
    )
    ocr = PaddleOCRProvider(lang=ocr_lang) if enable_ocr else NoOpOCRProvider()
    if not ocr.available:
        ocr = NoOpOCRProvider()
    llm = OllamaLLMProvider(model=ollama_model) if enable_llm else NoOpLLMProvider()
    if enable_llm and not llm.available:
        llm = NoOpLLMProvider()
    return ScanEngine(DefaultDocumentExtractor(ocr), ocr, llm)
