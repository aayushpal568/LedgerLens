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

Deterministic processing stays local. Ambiguous cases may send document text
to the configured external LLM provider (e.g. Claude Opus via fal.ai).
"""
import hashlib
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import combinations
from typing import Callable, Optional, Dict, Any, List

from .hashing import sha256_of_file
from .checklist import detect_period, match_item
from .similarity import normalize, similarity, token_set
from .models import CATEGORIES, STATUS_OK
from .interfaces import FileSource, DocumentExtractor, OCRProvider, LLMProvider

logger = logging.getLogger("ledgerlens.scan_engine")

SIMILARITY_THRESHOLD = 0.82
LSH_MIN_FILES = 60            # below this, exact pairwise is cheap enough
LSH_NUM_PERM = 64
LSH_THRESHOLD = 0.8          # bucket threshold; exact confirm still applies
MAX_TEXT_CHARS = 8000         # cap stored text to bound memory on huge folders
MAX_DUP_COMPARISONS = 200000  # hard bound on pairwise similarity checks
MAX_POSSIBLE_DUP_FINDINGS = 1000  # stop emitting once this many are found
LLM_MAX_CALLS_PER_SCAN = 5
LLM_MIN_CONFIDENCE = 0.80
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
        "classified_by": f.get("classified_by"),
        "classification": f.get("classification"),
    }


class ScanEngine:
    def __init__(self, extractor: DocumentExtractor,
                 ocr_provider: Optional[OCRProvider] = None,
                 llm_provider: Optional[LLMProvider] = None,
                 max_workers: Optional[int] = None,
                 llm_max_calls: Optional[int] = None,
                 llm_min_confidence: Optional[float] = None):
        self.extractor = extractor
        self.ocr = ocr_provider
        self.llm = llm_provider  # LLM provider for ambiguous checklist classification and period extraction
        self.max_workers = max_workers or min(16, (os.cpu_count() or 4) * 2)

        if llm_max_calls is not None:
            self.llm_max_calls = int(llm_max_calls)
        else:
            env_calls = os.environ.get("LLM_MAX_CALLS_PER_SCAN")
            self.llm_max_calls = int(env_calls) if env_calls is not None and env_calls != "" else LLM_MAX_CALLS_PER_SCAN

        if llm_min_confidence is not None:
            self.llm_min_confidence = float(llm_min_confidence)
        else:
            env_conf = os.environ.get("LLM_MIN_CONFIDENCE")
            self.llm_min_confidence = float(env_conf) if env_conf is not None and env_conf != "" else LLM_MIN_CONFIDENCE

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

        llm_budget = {"calls_made": 0, "max_calls": self.llm_max_calls}
        scan_llm_cache = {}

        if not cancelled:
            findings += self._checklist_checks(
                files, checklist_items,
                llm_budget=llm_budget, scan_cache=scan_llm_cache, should_cancel=should_cancel,
            )
            if should_cancel and should_cancel():
                cancelled = True

        if not cancelled:
            findings += self._wrong_period(
                files, expected_period,
                llm_budget=llm_budget, scan_cache=scan_llm_cache, should_cancel=should_cancel,
            )
            if should_cancel and should_cancel():
                cancelled = True

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
                pct = int(round(sim * 100))
                out.append({
                    "category": "possible_duplicate",
                    "title": f"\"{a['name']}\" and \"{b['name']}\" are {pct}% similar",
                    "confidence": pct, "confidence_level": _level(pct),
                    "files": [_file_view(a), _file_view(b)],
                    "evidence": {
                        "summary": f"Text similarity is {pct}% (exceeds {int(SIMILARITY_THRESHOLD*100)}% threshold).",
                        "similarity_score": round(sim, 3),
                    },
                })
        return out

    def _lsh_candidate_pairs(self, files):
        try:
            from datasketch import MinHash, MinHashLSH
        except ImportError:
            return None

        lsh = MinHashLSH(threshold=LSH_THRESHOLD, num_perm=LSH_NUM_PERM)
        store = {}
        for f in files:
            toks = token_set(f["text"])
            if not toks:
                continue
            m = MinHash(num_perm=LSH_NUM_PERM)
            for t in toks:
                m.update(t.encode("utf8"))
            lsh.insert(f["id"], m)
            store[f["id"]] = (f, m)

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

    def _checklist_checks(self, files, checklist_items, llm_budget=None, scan_cache=None, should_cancel=None):
        out = []
        if not checklist_items:
            return out

        if llm_budget is None:
            llm_budget = {"calls_made": 0, "max_calls": self.llm_max_calls}
        if scan_cache is None:
            scan_cache = {}

        # 1. Deterministic keyword and alias matching
        matched_map = {}
        assigned_file_ids = set()
        for item in checklist_items:
            matched = [f for f in files if match_item(item, f.get("norm_name", ""), f.get("text", ""))]
            matched_map[item["name"]] = matched
            for f in matched:
                assigned_file_ids.add(f["id"])

        # 2. Hard classification via LLM for items missing deterministic matches
        unmatched_items = [it for it in checklist_items if not matched_map.get(it["name"])]
        if unmatched_items and self.llm and getattr(self.llm, "available", False):
            # Sort candidates deterministically by file id
            unassigned_files = [
                f for f in files
                if f["id"] not in assigned_file_ids
                and f.get("status") == STATUS_OK
                and f.get("text")
            ]
            unassigned_files.sort(key=lambda x: str(x.get("id", "")))

            for f in unassigned_files:
                if should_cancel and should_cancel():
                    break
                if llm_budget["calls_made"] >= llm_budget["max_calls"]:
                    break

                f_ext = f["ext"].upper()
                # Only test against unmatched items that allow this file's extension
                viable_items = [
                    it for it in unmatched_items
                    if not it.get("allowed_types") or f_ext in [t.upper() for t in it.get("allowed_types", [])]
                ]
                if not viable_items:
                    continue

                candidate_types = [it["name"] for it in viable_items]
                file_hash = f.get("sha256") or hashlib.sha256(f["text"].encode("utf-8")).hexdigest()
                cache_key = ("classify", file_hash, tuple(sorted(candidate_types)))

                if cache_key in scan_cache:
                    cls_res = scan_cache[cache_key]
                else:
                    if should_cancel and should_cancel():
                        break
                    if llm_budget["calls_made"] >= llm_budget["max_calls"]:
                        break
                    llm_budget["calls_made"] += 1
                    try:
                        cls_res = self.llm.classify_document(f["text"], candidate_types)
                        scan_cache[cache_key] = cls_res
                    except Exception as e:
                        logger.warning("LLM classification skipped on error for file %s: %s", f.get("id"), str(e))
                        cls_res = None

                label = None
                conf = 0.0
                reasoning = ""
                if isinstance(cls_res, dict):
                    label = cls_res.get("label") or cls_res.get("classification")
                    try:
                        conf = float(cls_res.get("confidence", 0.0))
                    except (ValueError, TypeError):
                        conf = 0.0
                    reasoning = str(cls_res.get("reasoning") or "")
                elif isinstance(cls_res, str):
                    label = str(cls_res)
                    conf = float(getattr(cls_res, "confidence", 1.0))
                    reasoning = str(getattr(cls_res, "reasoning", ""))

                if label and conf >= self.llm_min_confidence:
                    target_item = next(
                        (it for it in viable_items if it["name"].lower() == label.strip().lower()),
                        None,
                    )
                    if target_item:
                        matched_map.setdefault(target_item["name"], []).append(f)
                        assigned_file_ids.add(f["id"])
                        f["classified_by"] = "llm"
                        f["classification"] = target_item["name"]
                        f["llm_confidence"] = conf
                        f["llm_reasoning"] = reasoning
                        if target_item in unmatched_items:
                            unmatched_items.remove(target_item)
                        if not unmatched_items:
                            break

        # 3. Compile checklist findings
        for item in checklist_items:
            matched = matched_map.get(item["name"], [])
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
                        evidence = {
                            "summary": f"Matched checklist item \"{item['name']}\" expects {', '.join(allowed)} but file is .{f['ext'].upper()}.",
                            "expected_types": allowed, "actual_type": f["ext"].upper(), "checklist_item": item["name"],
                        }
                        out.append({
                            "category": "wrong_type",
                            "title": f"\"{f['name']}\" may be the wrong file type for {item['name']}",
                            "confidence": 70, "confidence_level": "medium",
                            "files": [_file_view(f)],
                            "evidence": evidence,
                        })
        return out

    def _wrong_period(self, files, expected_period, llm_budget=None, scan_cache=None, should_cancel=None):
        out = []
        if not expected_period:
            return out

        if llm_budget is None:
            llm_budget = {"calls_made": 0, "max_calls": self.llm_max_calls}
        if scan_cache is None:
            scan_cache = {}

        # Sort candidate files deterministically by file id
        period_candidates = [
            f for f in files
            if (f.get("period") or {}).get("year") is None
            and f.get("status") == STATUS_OK
            and f.get("text")
        ]
        period_candidates.sort(key=lambda x: str(x.get("id", "")))

        for f in period_candidates:
            if not self.llm or not getattr(self.llm, "available", False):
                break
            if should_cancel and should_cancel():
                break
            if llm_budget["calls_made"] >= llm_budget["max_calls"]:
                break

            file_hash = f.get("sha256") or hashlib.sha256(f["text"].encode("utf-8")).hexdigest()
            cache_key = ("extract_field", file_hash, "tax_year")

            if cache_key in scan_cache:
                raw_yr = scan_cache[cache_key]
            else:
                if should_cancel and should_cancel():
                    break
                if llm_budget["calls_made"] >= llm_budget["max_calls"]:
                    break
                llm_budget["calls_made"] += 1
                try:
                    raw_yr = self.llm.extract_field(f["text"], "tax_year")
                    scan_cache[cache_key] = raw_yr
                except Exception as e:
                    logger.warning("LLM period extraction skipped on error for file %s: %s", f.get("id"), str(e))
                    raw_yr = None

            if raw_yr:
                m = re.search(r"\b(19|20)\d{2}\b", str(raw_yr))
                if m:
                    yr = int(m.group(0))
                    f.setdefault("period", {})["year"] = yr
                    f["period"]["detected_by"] = "llm"

        sorted_files = sorted(files, key=lambda x: str(x.get("id", "")))
        for f in sorted_files:
            yr = (f.get("period") or {}).get("year")
            if yr and int(yr) != int(expected_period):
                evidence = {
                    "summary": f"Detected period {yr} does not match the expected period {expected_period}.",
                    "detected_year": yr, "expected_year": int(expected_period),
                }
                detected_by_llm = (f.get("period") or {}).get("detected_by") == "llm"
                classified_by_llm = f.get("classified_by") == "llm"
                if detected_by_llm:
                    evidence["detected_by"] = "llm"
                if classified_by_llm:
                    evidence["classified_by"] = "llm"

                finding = {
                    "category": "wrong_period",
                    "title": f"\"{f['name']}\" appears to be from {yr}, not {expected_period}",
                    "confidence": 75, "confidence_level": "medium",
                    "files": [_file_view(f)],
                    "evidence": evidence,
                }
                if detected_by_llm or classified_by_llm:
                    finding["ai_assisted"] = True
                    finding["provenance"] = "llm"
                out.append(finding)
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


def build_default_engine(ocr_provider=None, llm_provider=None, llm_max_calls=None, llm_min_confidence=None) -> ScanEngine:
    """Default engine: text extraction with modular Baidu Unlimited-OCR and Claude Opus (fal.ai)."""
    from .providers import DefaultDocumentExtractor, BaiduUnlimitedOCRProvider, ClaudeOpusFalProvider
    ocr = ocr_provider or BaiduUnlimitedOCRProvider()
    llm = llm_provider or ClaudeOpusFalProvider()
    return ScanEngine(
        DefaultDocumentExtractor(ocr), ocr, llm,
        llm_max_calls=llm_max_calls, llm_min_confidence=llm_min_confidence,
    )
