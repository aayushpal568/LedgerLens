"""F3 regression tests for services.recover_orphaned_scans — startup orphan reconciliation.

Deterministic (memory backend, no threads/HTTP). A fixed boot_ts + grace drive the cutoff;
each scan is asserted individually. Global result counts are asserted with >= (the shared
session memory-db may hold other active rows) to avoid order coupling.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATA_BACKEND", "memory")
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-32-chars-long-ledgerlens-suite")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
os.environ["AGENT_WORKER_ENABLED"] = "false"

import server  # noqa: E402
import services  # noqa: E402

BOOT = datetime.now(timezone.utc)
GRACE = 60


def _iso(dt):
    return dt.isoformat()


def _insert(sids):
    async def _t():
        for doc in sids:
            await server.db.scans.insert_one(doc)
    asyncio.run(_t())


def _status(sid):
    async def _t():
        s = await server.db.scans.find_one({"id": sid})
        return s.get("status") if s else None
    return asyncio.run(_t())


def _run_recovery():
    return asyncio.run(services.recover_orphaned_scans(db=server.db, boot_ts=BOOT, grace_seconds=GRACE))


def test_old_scanning_scan_is_reconciled():
    sid = "f3-scan-old"
    _insert([{"id": sid, "firm_id": "firmA", "status": "scanning", "started_at": _iso(BOOT - timedelta(seconds=120))}])
    res = _run_recovery()
    assert res["reconciled"] >= 1
    assert _status(sid) == "error"


def test_old_queued_and_cancelling_are_reconciled():
    q = "f3-q-old"
    c = "f3-c-old"
    old = _iso(BOOT - timedelta(seconds=300))
    _insert([
        {"id": q, "firm_id": "firmA", "status": "queued", "started_at": old},
        {"id": c, "firm_id": "firmA", "status": "cancelling", "started_at": old},
    ])
    _run_recovery()
    assert _status(q) == "error"
    assert _status(c) == "error"


def test_fresh_scan_within_grace_is_untouched():
    sid = "f3-fresh"
    _insert([{"id": sid, "firm_id": "firmA", "status": "scanning", "started_at": _iso(BOOT - timedelta(seconds=5))}])
    _run_recovery()
    assert _status(sid) == "scanning", "a scan started within grace must not be failed"


def test_completed_and_cancelled_are_untouched():
    done = "f3-done"
    canc = "f3-cancelled"
    old = _iso(BOOT - timedelta(seconds=999))
    _insert([
        {"id": done, "firm_id": "firmA", "status": "completed", "started_at": old},
        {"id": canc, "firm_id": "firmA", "status": "cancelled", "started_at": old},
    ])
    _run_recovery()
    assert _status(done) == "completed"
    assert _status(canc) == "cancelled"


def test_z_and_naive_timestamps_parse_correctly():
    zsid = "f3-z"
    nsid = "f3-naive"
    old = BOOT - timedelta(seconds=200)
    _insert([
        {"id": zsid, "firm_id": "firmA", "status": "scanning",
         "started_at": old.strftime("%Y-%m-%dT%H:%M:%S.%fZ")},  # Z form
        {"id": nsid, "firm_id": "firmA", "status": "scanning",
         "started_at": old.replace(tzinfo=None).isoformat()},     # naive
    ])
    _run_recovery()
    assert _status(zsid) == "error"
    assert _status(nsid) == "error"


def test_unparseable_timestamp_is_skipped_not_failed():
    sid = "f3-badts"
    _insert([{"id": sid, "firm_id": "firmA", "status": "scanning", "started_at": "not-a-real-date"}])
    res = _run_recovery()
    assert _status(sid) == "scanning", "unparseable started_at must be skipped, never failed"
    assert res["skipped"] >= 1


def test_one_update_failure_does_not_stop_others(monkeypatch):
    poison = "f3-poison"
    good = "f3-good"
    old = _iso(BOOT - timedelta(seconds=120))
    _insert([
        {"id": poison, "firm_id": "firmA", "status": "scanning", "started_at": old},
        {"id": good, "firm_id": "firmA", "status": "scanning", "started_at": old},
    ])

    orig = server.db.scans.update_one

    async def flaky(filt, upd, *a, **k):
        if (filt or {}).get("id") == poison:
            raise RuntimeError("simulated write failure")
        return await orig(filt, upd, *a, **k)

    monkeypatch.setattr(server.db.scans, "update_one", flaky)
    res = _run_recovery()
    assert _status(good) == "error", "the other scan must still be reconciled"
    assert _status(poison) == "scanning", "the failing one is skipped, batch continues"
    assert res["reconciled"] >= 1 and res["skipped"] >= 1


def test_cross_tenant_reconciles_all_firms():
    a = "f3-x-a"
    b = "f3-x-b"
    old = _iso(BOOT - timedelta(seconds=150))
    _insert([
        {"id": a, "firm_id": "firmX", "status": "scanning", "started_at": old},
        {"id": b, "firm_id": "firmY", "status": "queued", "started_at": old},
    ])
    _run_recovery()
    assert _status(a) == "error"
    assert _status(b) == "error"


def test_fresh_scan_from_another_firm_untouched():
    a_old = "f3-y-old"
    b_fresh = "f3-y-fresh"
    _insert([
        {"id": a_old, "firm_id": "firmP", "status": "scanning", "started_at": _iso(BOOT - timedelta(seconds=120))},
        {"id": b_fresh, "firm_id": "firmQ", "status": "scanning", "started_at": _iso(BOOT - timedelta(seconds=3))},
    ])
    _run_recovery()
    assert _status(a_old) == "error", "firm P orphan reconciled"
    assert _status(b_fresh) == "scanning", "firm Q's just-started scan must be untouched"
