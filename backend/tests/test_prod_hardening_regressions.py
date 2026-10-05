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


# =========================================================================
# A4 — a live worker's lease is renewed across a slow turn (no double-exec)
# =========================================================================
import json  # noqa: E402
import agent.loop as agent_loop  # noqa: E402


def _a4_user():
    return AuthedUser(user_id=f"usr-{uuid.uuid4().hex[:6]}", firm_id=f"firm-{uuid.uuid4().hex[:8]}", token_version=1)


class _SlowAsyncProvider:
    """Async provider that blocks one turn for `sleep` seconds, then emits a final answer."""

    def __init__(self, sleep: float, final_text: str = "done"):
        self.sleep = sleep
        self.final_text = final_text
        self.calls = 0

    async def generate(self, prompt, system_prompt=None, max_tokens=None, temperature=None, **kwargs):
        self.calls += 1
        await asyncio.sleep(self.sleep)
        return json.dumps({"thought": "finish", "final": self.final_text})


def test_live_worker_lease_survives_a_slow_turn(monkeypatch):
    monkeypatch.setattr(services, "agent_lease_seconds", lambda: 3)

    async def _t():
        db = server.db
        u = _a4_user()
        thread_id = f"thr-{uuid.uuid4().hex[:8]}"
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        await db.agent_threads.insert_one({"id": thread_id, "firm_id": u.firm_id, "created_at": services.now_iso()})
        await db.agent_runs.insert_one({
            "id": run_id, "firm_id": u.firm_id, "thread_id": thread_id, "status": "queued",
            "created_by": u.user_id, "created_at": services.now_iso(), "started_at": None, "completed_at": None,
            "attempt": 0, "worker_id": None, "lease_expires_at": None, "heartbeat_at": None, "metadata": {},
        })

        provider = _SlowAsyncProvider(sleep=4.0)  # single turn far longer than the 3s lease
        task = asyncio.create_task(
            agent_loop.run_agent_loop(u, run_id, thread_id, llm_provider=provider, db=db)
        )

        # While the ONLY turn is still blocked inside the provider (past the turn-boundary
        # heartbeat), the background heartbeat must have kept the lease alive.
        await asyncio.sleep(3.5)
        rec = await services.recover_stale_agent_runs(db=db)
        assert rec.get("requeued", 0) == 0, "live worker's run must NOT be requeued"
        raw = await db.agent_runs.find_one({"id": run_id})
        assert raw["status"] == "running"
        assert raw["worker_id"] == services.WORKER_ID

        # A second worker therefore cannot claim the still-live run.
        second = await db.claim_agent_run(run_id, worker_id="worker-B", firm_id=str(u.firm_id), lease_seconds=3)
        assert second is None, "a live-leased run must not be claimable by another worker"

        result = await task
        assert result["status"] == services.RUN_STATUS_COMPLETED
        done = await db.agent_runs.find_one({"id": run_id})
        assert done["status"] == services.RUN_STATUS_COMPLETED
        assert not done.get("lease_expires_at"), "lease released on completion"
        assert not done.get("worker_id"), "worker cleared on completion"
    asyncio.run(_t())


def test_lease_heartbeat_stops_when_ownership_lost(monkeypatch):
    monkeypatch.setattr(services, "agent_lease_seconds", lambda: 3)

    async def _t():
        db = server.db
        u = _a4_user()
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        await db.agent_runs.insert_one({
            "id": run_id, "firm_id": u.firm_id, "thread_id": "t", "status": "queued",
            "created_at": services.now_iso(), "attempt": 0, "worker_id": None,
            "lease_expires_at": None, "heartbeat_at": None, "metadata": {},
        })
        claimed = await services.claim_agent_run(u, run_id, db=db)
        assert claimed is not None and claimed["status"] == "running"

        ev = asyncio.Event()
        task = asyncio.create_task(agent_loop._lease_heartbeat(u, run_id, db, interval_seconds=1, stop_event=ev))

        await asyncio.sleep(1.6)  # at least one heartbeat tick
        held = await db.agent_runs.find_one({"id": run_id})
        assert held["worker_id"] == services.WORKER_ID and held["status"] == "running"
        assert held.get("heartbeat_at")

        # Lose ownership (e.g. cancelled/completed) -> heartbeat must self-terminate, not resurrect.
        await db.agent_runs.update_one({"id": run_id}, {"$set": {"status": "completed"}})
        await asyncio.sleep(1.3)
        assert task.done(), "heartbeat should stop once the lease is no longer held"
        ev.set()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(_t())


# =========================================================================
# A7 — firm / account-wide purge (all collections + storage + session revoke)
# =========================================================================
from fastapi import HTTPException  # noqa: E402


def _a7_user():
    return AuthedUser(user_id=f"usr-{uuid.uuid4().hex[:8]}", firm_id=f"firm-{uuid.uuid4().hex[:8]}", token_version=1)


def _seed_full_firm(db, u):
    """Insert one doc into every firm-scoped collection for this user's firm (ids unique per firm)."""
    tok = u.firm_id.split("-")[-1]  # short unique token so docs never collide across firms
    ids = {
        "client": f"c-{tok}", "file1": f"f1-{tok}", "file2": f"f2-{tok}", "scan": f"s-{tok}",
        "finding": f"fd-{tok}", "template": f"t-{tok}", "thread": f"th-{tok}",
        "message": f"m-{tok}", "run": f"r-{tok}", "step": f"rs-{tok}", "approval": f"ap-{tok}",
    }

    async def _t():
        await db.firm.insert_one({"id": u.firm_id, "name": "F"})
        await db.users.insert_one({"id": u.user_id, "firm_id": u.firm_id, "email": f"{u.user_id}@x.test"})
        await db.clients.insert_one({"id": ids["client"], "firm_id": u.firm_id, "name": "co"})
        await db.files.insert_many([
            {"id": ids["file1"], "firm_id": u.firm_id, "client_id": ids["client"], "storage_path": f"acct-doc-checker/uploads/{tok}/f1.csv"},
            {"id": ids["file2"], "firm_id": u.firm_id, "client_id": ids["client"], "storage_path": f"acct-doc-checker/uploads/{tok}/f2.csv"},
        ])
        await db.scans.insert_one({"id": ids["scan"], "firm_id": u.firm_id, "client_id": ids["client"]})
        await db.findings.insert_one({"id": ids["finding"], "firm_id": u.firm_id, "scan_id": ids["scan"]})
        await db.templates.insert_one({"id": ids["template"], "firm_id": u.firm_id, "name": "tpl"})
        await db.agent_threads.insert_one({"id": ids["thread"], "firm_id": u.firm_id})
        await db.agent_messages.insert_one({"id": ids["message"], "firm_id": u.firm_id, "thread_id": ids["thread"]})
        await db.agent_runs.insert_one({"id": ids["run"], "firm_id": u.firm_id, "thread_id": ids["thread"]})
        await db.agent_run_steps.insert_one({"id": ids["step"], "firm_id": u.firm_id, "run_id": ids["run"]})
        await db.agent_approvals.insert_one({"id": ids["approval"], "firm_id": u.firm_id, "run_id": ids["run"]})
    asyncio.run(_t())
    return ids


def test_purge_firm_removes_every_collection():
    db = server.db
    u = _a7_user()
    _seed_full_firm(db, u)

    result = asyncio.run(services.purge_firm(u, confirm=True, db=db))
    assert result["ok"] is True
    removed = result["removed"]
    assert removed.get("clients") == 1
    assert removed.get("files") == 2
    assert removed.get("findings") == 1
    assert removed.get("templates") == 1
    assert removed.get("agent_threads") == 1
    assert removed.get("agent_messages") == 1
    assert removed.get("agent_runs") == 1
    assert removed.get("agent_run_steps") == 1
    assert removed.get("agent_approvals") == 1
    assert removed.get("users") == 1
    assert removed.get("firm") == 1
    assert result["storage_failures"] == 0
    assert result["objects_purged"] == 2

    async def _assert_empty():
        for col in ("clients", "files", "scans", "findings", "templates",
                    "agent_threads", "agent_messages", "agent_runs", "agent_run_steps", "agent_approvals"):
            leftover = await getattr(db, col).find({"firm_id": u.firm_id}).to_list(100)
            assert leftover == [], f"{col} not purged"
        assert await db.users.find_one({"id": u.user_id}) is None
        assert await db.firm.find_one({"id": u.firm_id}) is None
    asyncio.run(_assert_empty())


def test_purge_firm_requires_confirmation():
    db = server.db
    u = _a7_user()
    _seed_full_firm(db, u)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(services.purge_firm(u, confirm=False, db=db))
    assert exc.value.status_code == 400

    async def _still_there():
        return await db.clients.find({"firm_id": u.firm_id}).to_list(100)
    assert asyncio.run(_still_there()) != [], "data must survive a non-confirmed purge attempt"

    # Clean up so this firm's docs don't leak into later tests.
    asyncio.run(services.purge_firm(u, confirm=True, db=db))


def test_purge_firm_is_tenant_isolated():
    db = server.db
    a = _a7_user()
    b = _a7_user()
    _seed_full_firm(db, a)
    _seed_full_firm(db, b)

    asyncio.run(services.purge_firm(a, confirm=True, db=db))

    async def _check():
        a_clients = await db.clients.find({"firm_id": a.firm_id}).to_list(100)
        b_clients = await db.clients.find({"firm_id": b.firm_id}).to_list(100)
        assert a_clients == [], "purged firm A must be empty"
        assert len(b_clients) == 1, "firm B must be untouched"
        assert await db.firm.find_one({"id": b.firm_id}) is not None, "firm B record must remain"
        assert await db.users.find_one({"id": b.user_id}) is not None, "firm B user must remain"
    asyncio.run(_check())
    asyncio.run(services.purge_firm(b, confirm=True, db=db))


def test_delete_firm_route_purges_and_revokes_session(client):
    email = f"a7_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/auth/signup", json={"email": email, "password": "SecurePass123!", "firm_name": "A7 Firm", "name": "A7"})
    assert r.status_code == 201
    tok = r.json()["access_token"]
    firm_id = r.json()["firm"]["id"]
    hdr = {"Authorization": f"Bearer {tok}"}

    cid = client.post("/api/clients", headers=hdr, json={"name": "A7 Co", "client_type": "Small Business"}).json()["id"]
    up = client.post(f"/api/clients/{cid}/files", headers=hdr,
                     files=[("files", ("a.csv", b"Account,Debit,Credit\n1000,500,0\n", "text/csv"))])
    assert up.status_code == 200, up.text

    file_doc = asyncio.run(server.db.files.find_one({"client_id": cid}, {"_id": 0}))
    sp = file_doc["storage_path"]
    assert storage.get_object(sp)  # object exists before purge

    # confirm=false -> 400, nothing deleted
    bad = client.request("DELETE", "/api/firm", headers=hdr, json={"confirm": False})
    assert bad.status_code == 400
    assert len(client.get("/api/clients", headers=hdr).json()) == 1

    # confirm=true -> 200, purged
    ok = client.request("DELETE", "/api/firm", headers=hdr, json={"confirm": True})
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True

    # Same bearer now rejected: the user row (and token_version anchor) is gone.
    assert client.get("/api/firm", headers=hdr).status_code == 401

    # Firm gone from the DB and the backing object was purged.
    assert asyncio.run(server.db.firm.find_one({"id": firm_id})) is None
    with pytest.raises(FileNotFoundError):
        storage.get_object(sp)


# =========================================================================
# A10 — report 'Generated' timestamp is timezone-aware (UTC), not naive
# =========================================================================
import re  # noqa: E402
from engine import report as report_engine  # noqa: E402


def test_generated_stamp_renders_given_utc_datetime():
    stamp = report_engine._generated_stamp(dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc))
    assert stamp == "2024-01-02 03:04 UTC"


def test_generated_stamp_naive_input_treated_as_utc():
    stamp = report_engine._generated_stamp(dt.datetime(2020, 5, 6, 7, 8))
    assert stamp == "2020-05-06 07:08 UTC"


def test_generated_stamp_default_is_utc_labelled():
    stamp = report_engine._generated_stamp()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", stamp), stamp


def test_to_pdf_uses_utc_stamp_and_preserves_format(monkeypatch):
    calls = []
    orig = report_engine._generated_stamp

    def spy(now=None):
        calls.append(now)
        return orig(now)

    monkeypatch.setattr(report_engine, "_generated_stamp", spy)
    findings = [{
        "category": "missing_doc", "title": "Missing invoice", "files": [{"name": "a.csv"}],
        "confidence": 90, "confidence_level": "high", "status": "unreviewed",
    }]
    data = report_engine.to_pdf(findings, {"client_name": "ACME", "expected_period": 2024})
    assert isinstance(data, (bytes, bytearray))
    assert data[:5] == b"%PDF-", "PDF output format must be preserved"
    assert len(calls) >= 1, "the timestamp must be rendered via the tz-aware helper"
