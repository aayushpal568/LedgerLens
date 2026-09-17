"""Synthetic large-folder benchmark for the ScanEngine.

Generates N small files (with a controlled fraction of exact duplicates and
near-duplicates) into a temp folder, then runs the default engine over them via
LocalDirectoryFileSource and reports wall-time, throughput and peak memory.

Usage:
    python scripts/benchmark_scan.py --files 10000
    python scripts/benchmark_scan.py --files 50000
    python scripts/benchmark_scan.py --files 100000 --workers 16

Everything runs locally; no network, no cloud.
"""
import argparse
import os
import random
import shutil
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))

from engine import build_default_engine, LocalDirectoryFileSource  # noqa: E402

VENDORS = ["Chase", "Wells Fargo", "Amex", "PayPal", "Stripe", "Acme", "Globex", "Initech"]
DOCS = ["Invoice", "Bank Statement", "Profit and Loss", "Balance Sheet", "Receipt", "Payroll"]
WORDS = ("account ledger journal debit credit balance asset liability equity revenue expense "
         "depreciation amortization payable receivable invoice statement reconciliation audit "
         "quarter fiscal tax deduction payroll vendor client transaction deposit withdrawal "
         "interest principal mortgage lease dividend capital inventory cost margin gross net").split()


def _rand_body(rng, doc, vendor, year, i):
    lines = [f"{doc} {year}", f"Vendor,{vendor}", f"Reference,{i}-{rng.randint(1000,9999)}"]
    for _ in range(rng.randint(8, 20)):
        chunk = " ".join(rng.sample(WORDS, rng.randint(4, 8)))
        lines.append(f"{chunk},{rng.randint(10, 99999)}.{rng.randint(0,99):02d}")
    return "\n".join(lines) + "\n"


def make_files(root: str, n: int, dup_ratio: float = 0.02, near_ratio: float = 0.01, subdirs: int = 50):
    for i in range(subdirs):
        os.makedirs(os.path.join(root, f"folder_{i:03d}"), exist_ok=True)
    n_dup = int(n * dup_ratio)
    n_near = int(n * near_ratio)
    dup_seed = "Date,Amount,Vendor\n2024-01-01,100.00,Chase Bank Statement\n"
    for i in range(n):
        sub = f"folder_{i % subdirs:03d}"
        year = random.choice([2021, 2022, 2023, 2024])
        vendor = random.choice(VENDORS)
        doc = random.choice(DOCS)
        name = f"{doc.replace(' ', '_').lower()}_{vendor.lower()}_{year}_{i}.csv"
        path = os.path.join(root, sub, name)
        if i < n_dup:
            content = dup_seed  # exact duplicates
        elif i < n_dup + n_near:
            content = dup_seed + f"note,{i}\n"  # near duplicates
        else:
            content = _rand_body(random, doc, vendor, year, i)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=10000)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--dup", type=float, default=0.02)
    ap.add_argument("--near", type=float, default=0.01)
    ap.add_argument("--keep", action="store_true", help="keep generated files")
    args = ap.parse_args()

    root = tempfile.mkdtemp(prefix=f"bench_{args.files}_")
    print(f"Generating {args.files:,} files in {root} ...")
    t0 = time.time()
    make_files(root, args.files, dup_ratio=args.dup, near_ratio=args.near)
    print(f"  generated in {time.time() - t0:.1f}s")

    engine = build_default_engine()
    source = LocalDirectoryFileSource(root, supported_only=True)

    processed = {"n": 0}
    def on_progress(done, total, skip):
        processed["n"] = done
        if done % 10000 == 0:
            print(f"  ... {done:,}/{total:,}")

    tracemalloc.start()
    t1 = time.time()
    result = engine.run(source, checklist_items=[], expected_period=2024,
                        on_progress=on_progress, max_workers=args.workers)
    elapsed = time.time() - t1
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print("\n=== RESULT ===")
    print(f"files processed : {result['processed']:,}")
    print(f"wall time       : {elapsed:.1f}s  ({result['processed'] / max(elapsed, 0.001):,.0f} files/s)")
    print(f"peak memory     : {peak / 1e6:.1f} MB")
    print(f"skipped         : {len(result['skipped'])}")
    print(f"counts          : {result['counts']}")

    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
