"""Regression tests for the approved V1 production-hardening subset (A1, A5, A9).

All data is synthetic and the tests run against the hermetic in-memory backend with no
live/paid providers. Each test is self-contained (unique ids) so it does not depend on or
perturb other modules' shared database state.
"""
import asyncio
import datetime as dt
import os
import sys
import uuid
from pathlib import Path

import jwt
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
from tokens import create_access_token, decode_access_token, get_auth_secret_key  # noqa: E402


# =========================================================================
# A9 — JWT access tokens must carry (and require) an expiry claim
# =========================================================================
def test_decode_accepts_token_with_expiry():
    token = create_access_token(user_id="u-1", firm_id="f-1", token_version=1)
    payload = decode_access_token(token)
    assert payload["sub"] == "u-1"
    assert payload["firm_id"] == "f-1"
    assert "exp" in payload


def test_decode_rejects_token_missing_expiry():
    """Regression (A9): a well-signed token with NO exp was previously accepted forever."""
    now = dt.datetime.now(dt.timezone.utc)
    token_no_exp = jwt.encode(
        {"sub": "u-1", "firm_id": "f-1", "token_version": 1, "iat": int(now.timestamp())},
        get_auth_secret_key(),
        algorithm="HS256",
    )
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token_no_exp)


def test_decode_rejects_expired_token():
    expired = jwt.encode(
        {"sub": "u-1", "firm_id": "f-1", "token_version": 1, "exp": int(dt.datetime.now(dt.timezone.utc).timestamp()) - 100},
        get_auth_secret_key(),
        algorithm="HS256",
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(expired)


# =========================================================================
# A5 — approvals stranded in 'executing' are reconciled on recovery
# =========================================================================
def test_recover_stuck_agent_approvals_only_fails_stale_executing():
    async def _test():
        db = server.db
        firm = f"firm-{uuid.uuid4().hex[:8]}"
        old_claim = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        fresh_claim = dt.datetime.now(dt.timezone.utc).isoformat()

        stale = {"id": f"appr-old-{uuid.uuid4().hex[:6]}", "firm_id": firm, "status": "executing", "execution_claimed_at": old_claim}
        fresh = {"id": f"appr-fresh-{uuid.uuid4().hex[:6]}", "firm_id": firm, "status": "executing", "execution_claimed_at": fresh_claim}
        pending = {"id": f"appr-pending-{uuid.uuid4().hex[:6]}", "firm_id": firm, "status": "pending"}
        approved = {"id": f"appr-approved-{uuid.uuid4().hex[:6]}", "firm_id": firm, "status": "approved"}
        for doc in (stale, fresh, pending, approved):
            await db.agent_approvals.insert_one(dict(doc))

        result = await db.recover_stuck_agent_approvals(ttl_seconds=300)
        assert result.get("failed", 0) >= 1

        stale_after = await db.agent_approvals.find_one({"id": stale["id"]})
        fresh_after = await db.agent_approvals.find_one({"id": fresh["id"]})
        pending_after = await db.agent_approvals.find_one({"id": pending["id"]})
        approved_after = await db.agent_approvals.find_one({"id": approved["id"]})

        assert stale_after["status"] == "failed"
        assert "execution_error" in stale_after
        # A still-fresh claim and non-executing states must never be disturbed.
        assert fresh_after["status"] == "executing"
        assert pending_after["status"] == "pending"
        assert approved_after["status"] == "approved"
    asyncio.run(_test())


def test_services_recover_reports_approvals_failed_count():
    async def _test():
        db = server.db
        firm = f"firm-{uuid.uuid4().hex[:8]}"
        old_claim = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        stuck = {"id": f"appr-svc-{uuid.uuid4().hex[:6]}", "firm_id": firm, "status": "executing", "execution_claimed_at": old_claim}
        await db.agent_approvals.insert_one(dict(stuck))

        recovery = await services.recover_stale_agent_runs(db=db)
        assert "approvals_failed" in recovery
        assert recovery["approvals_failed"] >= 1
        after = await db.agent_approvals.find_one({"id": stuck["id"]})
        assert after["status"] == "failed"
    asyncio.run(_test())


# =========================================================================
# A1 — _run_scan must NOT create phantom records for unreadable files
# =========================================================================
def test_run_scan_excludes_unreadable_files_and_surfaces_them_as_skipped(monkeypatch):
    async def _test():
        db = server.db
        firm = f"firm-{uuid.uuid4().hex[:8]}"
        client_id = f"client-{uuid.uuid4().hex[:8]}"
        scan_id = f"scan-{uuid.uuid4().hex[:8]}"

        ok_id = f"file-ok-{uuid.uuid4().hex[:6]}"
        missing_id = f"file-miss-{uuid.uuid4().hex[:6]}"
        error_id = f"file-err-{uuid.uuid4().hex[:6]}"

        await db.scans.insert_one({"id": scan_id, "firm_id": firm, "client_id": client_id, "status": "queued"})
        await db.files.insert_many([
            {"id": ok_id, "firm_id": firm, "client_id": client_id, "name": "ledger.csv", "ext": "csv", "size": 10, "storage_path": f"uploads/{firm}/{client_id}/{ok_id}.csv", "is_deleted": False},
            {"id": missing_id, "firm_id": firm, "client_id": client_id, "name": "gone.csv", "ext": "csv", "size": 10, "storage_path": f"uploads/{firm}/{client_id}/{missing_id}.csv", "is_deleted": False},
            {"id": error_id, "firm_id": firm, "client_id": client_id, "name": "boom.csv", "ext": "csv", "size": 10, "storage_path": f"uploads/{firm}/{client_id}/{error_id}.csv", "is_deleted": False},
        ])

        def fake_get_object(path):
            if path.endswith(f"{ok_id}.csv"):
                return b"Account,Debit,Credit,Year\n1000,500,0,2024\n"
            if path.endswith(f"{missing_id}.csv"):
                raise FileNotFoundError(f"Object not found: {path}")
            raise RuntimeError("transient storage blip")

        captured = {}

        def fake_run_detection(records, items, expected_period, on_progress, cancel_check):
            captured["records"] = list(records)
            return {
                "findings": [],
                "counts": {"total": 0},
                "skipped": [],
                "processed": len(records),
                "total": len(records),
                "file_states": {},
                "cancelled": False,
            }

        # No real storage/LLM/OCR: fake the read path and the detection engine.
        monkeypatch.setattr(storage, "get_object", fake_get_object)
        monkeypatch.setattr(services, "run_detection", fake_run_detection)
        # Neutralize the retry backoff sleeps so the transient-error path stays fast.
        async def _no_sleep(_t):
            return None
        monkeypatch.setattr(services.asyncio, "sleep", _no_sleep)

        await services._run_scan(scan_id, firm, client_id, None, 2024, db)

        # CRITICAL (A1): only the readable file is handed to the engine. The two unreadable
        # files must NOT appear as phantom records (which previously produced false findings).
        assert "records" in captured
        assert [r["id"] for r in captured["records"]] == [ok_id]

        scan_after = await db.scans.find_one({"id": scan_id})
        assert scan_after["status"] == "completed"
        skipped = scan_after.get("skipped_files") or []
        reasons = {s["name"]: s["reason"] for s in skipped}
        assert "gone.csv" in reasons and "Document not found" in reasons["gone.csv"]
        assert "boom.csv" in reasons and "Could not read" in reasons["boom.csv"]
        # Counted in totals even though excluded from detection.
        assert scan_after.get("total_files") == 3
        assert scan_after.get("processed_files") == 1
    asyncio.run(_test())
