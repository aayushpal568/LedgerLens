"""O-scan F1/F2 regression tests: scan concurrency caps, task hygiene, and the
scan maximum-duration (timeout) path. All hermetic (memory backend), no threads/OCR.

Concurrency gating is exercised by stubbing _run_scan_inner so the semaphore gate is tested
directly and deterministically; the timeout is exercised via the real _run_scan body with a
fake storage/run_detection that crosses a short deadline.
"""
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATA_BACKEND", "memory")
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-32-chars-long-ledgerlens-suite")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
os.environ["AGENT_WORKER_ENABLED"] = "false"

import server  # noqa: E402
import services  # noqa: E402
import storage  # noqa: E402
from auth_dep import AuthedUser  # noqa: E402


def _user(firm_id=None):
    return AuthedUser(user_id=f"u-{uuid.uuid4().hex[:6]}", firm_id=firm_id or f"firm-{uuid.uuid4().hex[:6]}", token_version=1)


# --------------------------- F1: concurrency gate --------------------------------
def test_per_firm_cap_limits_concurrent_scans(monkeypatch):
    async def _t():
        state = {"cur": 0, "peak": 0}
        gate = asyncio.Event()

        async def fake_inner(*a, **k):
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
            await gate.wait()
            state["cur"] -= 1

        monkeypatch.setattr(services, "_run_scan_inner", fake_inner)
        monkeypatch.setattr(services, "SCAN_MAX_CONCURRENCY", 50)   # global generous
        monkeypatch.setattr(services, "SCAN_MAX_CONCURRENCY_PER_FIRM", 1)

        fid = _user().firm_id
        tasks = [asyncio.create_task(services._run_scan(f"s{i}", fid, "c", None, 2024, server.db)) for i in range(5)]
        await asyncio.sleep(0.05)
        assert state["peak"] == 1, "per-firm cap of 1 must allow only one concurrent scan"
        gate.set()
        await asyncio.gather(*tasks)
        assert state["peak"] == 1
    asyncio.run(_t())


def test_global_cap_limits_concurrent_scans_across_firms(monkeypatch):
    async def _t():
        state = {"cur": 0, "peak": 0}
        gate = asyncio.Event()

        async def fake_inner(*a, **k):
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
            await gate.wait()
            state["cur"] -= 1

        monkeypatch.setattr(services, "_run_scan_inner", fake_inner)
        monkeypatch.setattr(services, "SCAN_MAX_CONCURRENCY", 2)
        monkeypatch.setattr(services, "SCAN_MAX_CONCURRENCY_PER_FIRM", 10)  # per-firm not limiting

        firms = [_user().firm_id for _ in range(6)]
        tasks = [asyncio.create_task(services._run_scan(f"g{i}", firms[i], "c", None, 2024, server.db)) for i in range(6)]
        await asyncio.sleep(0.05)
        assert state["peak"] == 2, "global cap of 2 must bound concurrency across firms"
        gate.set()
        await asyncio.gather(*tasks)
    asyncio.run(_t())


def test_different_firms_run_concurrently_when_capacity_exists(monkeypatch):
    async def _t():
        entered = {"count": 0}
        hold = asyncio.Event()

        async def fake_inner(scan_id, firm_id, *a, **k):
            entered["count"] += 1
            # block until BOTH firms have entered (proves they can overlap)
            if entered["count"] >= 2:
                hold.set()
            await hold.wait()

        monkeypatch.setattr(services, "_run_scan_inner", fake_inner)
        monkeypatch.setattr(services, "SCAN_MAX_CONCURRENCY", 4)
        monkeypatch.setattr(services, "SCAN_MAX_CONCURRENCY_PER_FIRM", 2)

        a, b = _user().firm_id, _user().firm_id
        t1 = asyncio.create_task(services._run_scan("a", a, "c", None, 2024, server.db))
        t2 = asyncio.create_task(services._run_scan("b", b, "c", None, 2024, server.db))
        await asyncio.wait_for(hold.wait(), timeout=2)
        assert entered["count"] == 2, "two distinct firms must be able to scan concurrently"
        hold.set()
        await asyncio.gather(t1, t2)
    asyncio.run(_t())


# --------------------------- F1: active-task hygiene -----------------------------
def test_start_scan_registers_and_cleans_active_task(monkeypatch):
    async def _t():
        async def fake_inner(*a, **k):
            return None

        monkeypatch.setattr(services, "_run_scan_inner", fake_inner)
        db = server.db
        u = _user()
        cid = f"client-{uuid.uuid4().hex[:8]}"
        await db.clients.insert_one({"id": cid, "firm_id": u.firm_id, "name": "co"})

        before = len(services._ACTIVE_SCAN_TASKS)
        await services.start_scan(u, cid, None, 2024, db=db)
        assert len(services._ACTIVE_SCAN_TASKS) == before + 1, "task must be retained while running"

        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(services._ACTIVE_SCAN_TASKS) == before:
                break
        assert len(services._ACTIVE_SCAN_TASKS) == before, "done callback must remove the finished task"
    asyncio.run(_t())


# --------------------------- normal single-scan (behavior preserved) -------------
def test_normal_scan_completes_under_the_gate(monkeypatch):
    async def _t():
        db = server.db
        fid = _user().firm_id
        cid = f"client-{uuid.uuid4().hex[:8]}"
        scan_id = f"scan-{uuid.uuid4().hex[:8]}"
        await db.scans.insert_one({"id": scan_id, "firm_id": fid, "client_id": cid, "status": "queued"})
        fids = [f"file-{i}" for i in range(2)]
        await db.files.insert_many([
            {"id": fids[i], "firm_id": fid, "client_id": cid, "name": f"{i}.csv", "ext": "csv",
             "size": 5, "storage_path": f"up/{i}.csv", "is_deleted": False}
            for i in range(2)
        ])

        monkeypatch.setattr(storage, "get_object", lambda path: b"x")
        captured = {}

        def fake_detect(records, items, expected_period, on_progress, cancel_check):
            captured["records"] = list(records)
            return {"findings": [], "counts": {"total": 0}, "skipped": [],
                    "processed": len(records), "total": len(records), "file_states": {}, "cancelled": False}

        monkeypatch.setattr(services, "run_detection", fake_detect)
        await services._run_scan(scan_id, fid, cid, None, 2024, db)  # through the gate

        assert len(captured.get("records", [])) == 2
        scan = await db.scans.find_one({"id": scan_id})
        assert scan["status"] == "completed"
    asyncio.run(_t())


# --------------------------- F2: timeout (download path) -------------------------
def test_scan_times_out_during_downloads(monkeypatch):
    async def _t():
        db = server.db
        fid = _user().firm_id
        cid = f"client-{uuid.uuid4().hex[:8]}"
        scan_id = f"scan-{uuid.uuid4().hex[:8]}"
        await db.scans.insert_one({"id": scan_id, "firm_id": fid, "client_id": cid, "status": "queued"})
        await db.files.insert_many([
            {"id": f"f{i}", "firm_id": fid, "client_id": cid, "name": f"{i}.csv", "ext": "csv",
             "size": 5, "storage_path": f"up/{i}.csv", "is_deleted": False}
            for i in range(6)
        ])

        monkeypatch.setattr(services, "SCAN_MAX_SECONDS", 1)

        async def slow_get(*a, **k):
            await asyncio.sleep(0.5)
            return b"x"

        monkeypatch.setattr(services.asyncio, "to_thread", lambda fn, *a, **k: slow_get())
        monkeypatch.setattr(services, "run_detection", lambda *a, **k: pytest.fail("detection should not run after download timeout"))

        await services._run_scan(scan_id, fid, cid, None, 2024, db)

        scan = await db.scans.find_one({"id": scan_id})
        assert scan["status"] == "error"
        assert scan["error"] == "Scan exceeded maximum duration."
    asyncio.run(_t())


# --------------------------- F2: timeout (detection path) ------------------------
def test_scan_times_out_during_detection(monkeypatch):
    async def _t():
        db = server.db
        fid = _user().firm_id
        cid = f"client-{uuid.uuid4().hex[:8]}"
        scan_id = f"scan-{uuid.uuid4().hex[:8]}"
        await db.scans.insert_one({"id": scan_id, "firm_id": fid, "client_id": cid, "status": "queued"})
        await db.files.insert_many([
            {"id": "f0", "firm_id": fid, "client_id": cid, "name": "a.csv", "ext": "csv",
             "size": 5, "storage_path": "up/a.csv", "is_deleted": False}
        ])
        monkeypatch.setattr(storage, "get_object", lambda path: b"x")
        monkeypatch.setattr(services, "SCAN_MAX_SECONDS", 1)

        def fake_detect(records, items, expected_period, on_progress, cancel_check):
            # Simulate a detection that overruns the deadline, reporting progress after it lapsed.
            time.sleep(1.3)
            on_progress(len(records), len(records), None)
            return {"findings": [], "counts": {}, "skipped": [], "processed": len(records),
                    "total": len(records), "file_states": {}, "cancelled": True}

        monkeypatch.setattr(services, "run_detection", fake_detect)

        await services._run_scan(scan_id, fid, cid, None, 2024, db)

        scan = await db.scans.find_one({"id": scan_id})
        assert scan["status"] == "error"
        assert scan["error"] == "Scan exceeded maximum duration."
    asyncio.run(_t())
