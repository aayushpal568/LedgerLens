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


# =========================================================================
# A2 — password-reset token is never exposed; endpoint is throttled
# =========================================================================
import auth_routes  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        yield c


def _signup_user(client, email, password="OldPassword123!"):
    resp = client.post(
        "/api/auth/signup",
        json={"email": email, "password": password, "firm_name": "A2 Firm", "name": "A2 Admin"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["user"]["id"]


def test_forgot_password_never_exposes_token(client, monkeypatch):
    email = f"a2_{uuid.uuid4().hex[:8]}@example.com"
    uid = _signup_user(client, email)

    captured = {}
    monkeypatch.setattr(auth_routes, "_deliver_reset_token", lambda e, t: captured.update(email=e, token=t))

    res = client.post("/api/auth/forgot-password", json={"email": email})
    assert res.status_code == 200
    body = res.json()
    # CRITICAL (A2): the raw token must NEVER be returned in the HTTP response.
    assert "reset_token" not in body
    assert "message" in body
    # The token was still generated and delivered OUT OF BAND + stored (hashed).
    assert captured.get("email") == email and captured.get("token")
    import hashlib

    stored = server.db.users.find_one({"id": uid})
    # Support both a coroutine (memory) and dict return defensively not needed; memory is sync-awaitable.
    expected_hash = hashlib.sha256(captured["token"].encode("utf-8")).hexdigest()
    assert _run_async(stored).get("reset_token_hash") == expected_hash


def test_forgot_password_no_enumeration(client):
    bogus = f"a2_missing_{uuid.uuid4().hex[:8]}@example.com"
    res = client.post("/api/auth/forgot-password", json={"email": bogus})
    assert res.status_code == 200
    body = res.json()
    assert "reset_token" not in body
    assert "message" in body


def test_forgot_password_throttled(client):
    email = f"a2_throttle_{uuid.uuid4().hex[:8]}@example.com"
    _signup_user(client, email)
    max_attempts = auth_routes.FORGOT_PASSWORD_MAX_ATTEMPTS

    statuses = [
        client.post("/api/auth/forgot-password", json={"email": email}).status_code
        for _ in range(max_attempts + 1)
    ]
    assert statuses[0] != 429, "the first request must not be throttled"
    assert 429 in statuses, f"expected a 429 within {max_attempts + 1} requests, got {statuses}"


def _run_async(value):
    """Await a coroutine if the collection returned one (memory/postgres parity)."""
    return asyncio.run(value) if asyncio.iscoroutine(value) else value


# =========================================================================
# A3 — read endpoints are bounded; default behavior is byte-for-byte unchanged
# =========================================================================
from auth_dep import AuthedUser  # noqa: E402


def _a3_user():
    return AuthedUser(user_id=f"usr-{uuid.uuid4().hex[:6]}", firm_id=f"firm-{uuid.uuid4().hex[:8]}", token_version=1)


def test_list_files_pagination_default_unchanged():
    async def _t():
        db = server.db
        u = _a3_user()
        cid = f"client-{uuid.uuid4().hex[:8]}"
        tok = uuid.uuid4().hex[:6]
        ids = [f"{tok}{i}" for i in range(5)]
        await db.clients.insert_one({"id": cid, "firm_id": u.firm_id, "name": "c"})
        await db.files.insert_many([
            {"id": ids[i], "firm_id": u.firm_id, "client_id": cid, "name": f"{i}.csv", "ext": "csv",
             "size": 1, "storage_path": f"s/{i}", "is_deleted": False,
             "uploaded_at": f"2024-01-01T00:00:{i:02d}+00:00"}
            for i in range(5)
        ])
        desc = list(reversed(ids))  # uploaded_at asc -> sort desc = f4..f0

        full = await services.list_files(u, cid, db=db)
        assert [r["id"] for r in full] == desc, "default must be unchanged (all rows, uploaded_at desc)"

        assert [r["id"] for r in await services.list_files(u, cid, db=db, limit=2)] == desc[:2]
        assert [r["id"] for r in await services.list_files(u, cid, db=db, limit=2, offset=2)] == desc[2:4]
        assert [r["id"] for r in await services.list_files(u, cid, db=db, limit=2, offset=4)] == desc[4:]
        assert await services.list_files(u, cid, db=db, offset=99) == []
        # Oversized limit clamps to the global cap and still returns everything.
        assert [r["id"] for r in await services.list_files(u, cid, db=db, limit=10_000_000)] == desc
    asyncio.run(_t())


def test_list_files_respects_global_cap(monkeypatch):
    monkeypatch.setattr(services, "MAX_ROWS_PER_QUERY", 3)

    async def _t():
        db = server.db
        u = _a3_user()
        cid = f"client-{uuid.uuid4().hex[:8]}"
        tok = uuid.uuid4().hex[:6]
        await db.clients.insert_one({"id": cid, "firm_id": u.firm_id, "name": "c"})
        await db.files.insert_many([
            {"id": f"{tok}{i}", "firm_id": u.firm_id, "client_id": cid, "name": f"{i}.csv", "ext": "csv",
             "size": 1, "storage_path": f"s/{i}", "is_deleted": False,
             "uploaded_at": f"2024-01-01T00:00:{i:02d}+00:00"}
            for i in range(5)
        ])
        bounded = await services.list_files(u, cid, db=db)
        assert len(bounded) == 3
    asyncio.run(_t())


def test_get_findings_pagination():
    async def _t():
        db = server.db
        u = _a3_user()
        sid = f"scan-{uuid.uuid4().hex[:8]}"
        tok = uuid.uuid4().hex[:6]
        await db.scans.insert_one({"id": sid, "firm_id": u.firm_id, "client_id": "c", "status": "completed"})
        confs = [10, 20, 30, 40, 50]
        fids = [f"{tok}f{i}" for i in range(5)]
        await db.findings.insert_many([
            {"id": fids[i], "firm_id": u.firm_id, "scan_id": sid, "category": "dup", "status": "unreviewed",
             "confidence": confs[i], "note": ""}
            for i in range(5)
        ])
        order = [fids[i] for i in sorted(range(5), key=lambda i: confs[i], reverse=True)]

        assert [r["id"] for r in await services.get_findings(u, sid, db=db)] == order
        assert [r["id"] for r in await services.get_findings(u, sid, db=db, limit=2)] == order[:2]
        assert [r["id"] for r in await services.get_findings(u, sid, db=db, limit=2, offset=2)] == order[2:4]
    asyncio.run(_t())


def test_files_route_accepts_limit_offset(client):
    email = f"a3_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/auth/signup", json={"email": email, "password": "SecurePass123!", "firm_name": "A3 Firm", "name": "A3"})
    assert r.status_code == 201
    hdr = {"Authorization": f"Bearer {r.json()['access_token']}"}
    cid = client.post("/api/clients", headers=hdr, json={"name": "A3 Co", "client_type": "Small Business"}).json()["id"]
    for i in range(3):
        up = client.post(f"/api/clients/{cid}/files", headers=hdr,
                         files=[("files", (f"a{i}.csv", b"Account,Debit,Credit\n1000,500,0\n", "text/csv"))])
        assert up.status_code == 200, up.text

    assert len(client.get(f"/api/clients/{cid}/files", headers=hdr).json()) == 3
    assert len(client.get(f"/api/clients/{cid}/files?limit=2", headers=hdr).json()) == 2
    assert len(client.get(f"/api/clients/{cid}/files?limit=2&offset=3", headers=hdr).json()) == 0
    assert len(client.get(f"/api/clients/{cid}/files?offset=1", headers=hdr).json()) == 2
