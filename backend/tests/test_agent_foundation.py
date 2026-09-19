"""Comprehensive test suite for LedgerLens Agent Foundation (Step 3).

Verifies:
1. Route Authentication:
   - Unauthenticated POST /api/agent/messages returns 401
   - Unauthenticated GET /api/agent/runs/{run_id} returns 401
2. Authenticated Thread, Message, and Run Creation:
   - Initial message without thread_id creates new thread, user message, and queued run
   - Returns HTTP 202 with run_id, thread_id, status="queued"
   - Database documents exist with strictly scoped firm_id
3. Message Continuation:
   - Sending message with existing thread_id appends message and generates new run
4. Strict Multi-Tenant Isolation:
   - Firm B user cannot access Firm A run (returns 404)
   - Firm B user cannot post to Firm A thread (returns 404)
   - Attempting cross-tenant explicit firm_id raises PermissionError via scoped()
5. Input Validation:
   - Empty or whitespace text returns 400
   - Missing text returns 422
   - Oversized text (>10000 chars) returns 400
   - Nonexistent thread_id returns 404
   - Nonexistent run_id returns 404
6. Run Lifecycle & Run Steps (Step 4 Readiness):
   - update_agent_run transitions through valid states (queued -> running -> completed)
   - Invalid run status rejected with 400
   - create_agent_run_step persists execution steps with tenant scoping
   - list_agent_run_steps retrieves steps chronologically
"""
import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# Ensure test environment
os.environ.setdefault("DATA_BACKEND", "memory")
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-32-chars-long-ledgerlens-suite")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")

import server
import services
from auth_dep import AuthedUser
from db_access import scoped


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        yield c


def _signup(client: TestClient, email: str, firm_name: str, password: str = "SecurePass123!"):
    resp = client.post("/api/auth/signup", json={
        "email": email,
        "password": password,
        "firm_name": firm_name,
        "name": f"Admin {firm_name}",
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    token = data["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return data, headers


# ===========================================================================
# 1. Unauthenticated Access
# ===========================================================================
def test_unauthenticated_agent_access_rejected(client):
    # No auth header
    res_post = client.post("/api/agent/messages", json={"text": "hello"})
    assert res_post.status_code == 401

    res_get = client.get(f"/api/agent/runs/{uuid.uuid4()}")
    assert res_get.status_code == 401


# ===========================================================================
# 2. Authenticated Message Posting & Run Creation
# ===========================================================================
def test_authenticated_message_creates_thread_and_queued_run(client):
    data, headers = _signup(client, f"agent_user_{uuid.uuid4().hex[:6]}@testfirm.com", "Agent Firm A")
    firm_id = data["firm"]["id"]
    user_id = data["user"]["id"]

    msg_text = "Please examine the latest payroll audit files for duplicates."
    res = client.post("/api/agent/messages", json={"text": msg_text}, headers=headers)
    assert res.status_code == 202

    body = res.json()
    assert "run_id" in body
    assert "thread_id" in body
    assert body["status"] == "queued"
    assert "created_at" in body

    run_id = body["run_id"]
    thread_id = body["thread_id"]

    # Verify GET /api/agent/runs/{run_id}
    run_res = client.get(f"/api/agent/runs/{run_id}", headers=headers)
    assert run_res.status_code == 200
    run_data = run_res.json()
    assert run_data["id"] == run_id
    assert run_data["firm_id"] == firm_id
    assert run_data["thread_id"] == thread_id
    assert run_data["status"] in ("queued", "running", "failed")
    assert run_data["created_by"] == user_id


# ===========================================================================
# 3. Message Continuation in Existing Thread
# ===========================================================================
def test_message_continuation_in_existing_thread(client):
    data, headers = _signup(client, f"continue_{uuid.uuid4().hex[:6]}@testfirm.com", "Continue Firm")

    # Message 1
    res1 = client.post("/api/agent/messages", json={"text": "First query"}, headers=headers)
    assert res1.status_code == 202
    thread_id = res1.json()["thread_id"]
    run1_id = res1.json()["run_id"]

    # Message 2 in same thread
    res2 = client.post("/api/agent/messages", json={"thread_id": thread_id, "text": "Follow-up query"}, headers=headers)
    assert res2.status_code == 202
    assert res2.json()["thread_id"] == thread_id
    run2_id = res2.json()["run_id"]
    assert run1_id != run2_id

    # Verify messages in thread via service
    user = AuthedUser(user_id=data["user"]["id"], firm_id=data["firm"]["id"], token_version=1)
    import asyncio
    messages = asyncio.run(services.list_agent_messages(user, thread_id, db=server.db))
    assert len(messages) == 2
    assert messages[0]["text"] == "First query"
    assert messages[1]["text"] == "Follow-up query"


# ===========================================================================
# 4. Strict Multi-Tenant Isolation
# ===========================================================================
def test_cross_tenant_isolation_runs_and_threads(client):
    # Firm A creates a thread and run
    data_a, headers_a = _signup(client, f"firm_a_{uuid.uuid4().hex[:6]}@firma.com", "Firm Alpha")
    res_a = client.post("/api/agent/messages", json={"text": "Firm Alpha confidential instructions"}, headers=headers_a)
    assert res_a.status_code == 202
    run_a_id = res_a.json()["run_id"]
    thread_a_id = res_a.json()["thread_id"]

    # Firm B attempts to access Firm A's run
    data_b, headers_b = _signup(client, f"firm_b_{uuid.uuid4().hex[:6]}@firmb.com", "Firm Beta")
    res_b_get_run = client.get(f"/api/agent/runs/{run_a_id}", headers=headers_b)
    assert res_b_get_run.status_code == 404

    # Firm B attempts to post into Firm A's thread
    res_b_post = client.post("/api/agent/messages", json={"thread_id": thread_a_id, "text": "Malicious inject"}, headers=headers_b)
    assert res_b_post.status_code == 404

    # Direct scoped() tampering check
    user_b = AuthedUser(user_id=data_b["user"]["id"], firm_id=data_b["firm"]["id"], token_version=1)
    with pytest.raises(PermissionError):
        scoped(server.db.agent_runs, user_b, {"id": run_a_id, "firm_id": data_a["firm"]["id"]})


# ===========================================================================
# 5. Input Validation
# ===========================================================================
def test_agent_message_validation(client):
    _, headers = _signup(client, f"valid_{uuid.uuid4().hex[:6]}@testfirm.com", "Validation Firm")

    # Empty text
    res = client.post("/api/agent/messages", json={"text": ""}, headers=headers)
    assert res.status_code == 400

    # Whitespace text
    res = client.post("/api/agent/messages", json={"text": "   \n\t  "}, headers=headers)
    assert res.status_code == 400

    # Missing text field
    res = client.post("/api/agent/messages", json={}, headers=headers)
    assert res.status_code == 422

    # Oversized text (>10000 characters)
    oversized = "A" * (services.MAX_AGENT_MESSAGE_LENGTH + 1)
    res = client.post("/api/agent/messages", json={"text": oversized}, headers=headers)
    assert res.status_code == 400
    assert "exceeds maximum allowed length" in res.json()["detail"]

    # Boundary: exactly 10000 characters succeeds
    boundary = "B" * services.MAX_AGENT_MESSAGE_LENGTH
    res = client.post("/api/agent/messages", json={"text": boundary}, headers=headers)
    assert res.status_code == 202

    # Nonexistent thread_id
    res = client.post("/api/agent/messages", json={"thread_id": str(uuid.uuid4()), "text": "Valid"}, headers=headers)
    assert res.status_code == 404

    # Nonexistent run_id
    res = client.get(f"/api/agent/runs/{uuid.uuid4()}", headers=headers)
    assert res.status_code == 404


# ===========================================================================
# 6. Run States, Run Steps, and Persistence (Step 4 Readiness)
# ===========================================================================
def test_agent_run_states_and_run_steps():
    import asyncio

    user = AuthedUser(user_id="usr-test-1", firm_id="firm-test-1", token_version=1)

    async def _test():
        # Create message and run
        res = await services.post_agent_message(user, "Analyze invoice anomalies", db=server.db)
        run_id = res["run_id"]
        thread_id = res["thread_id"]

        # Initial state is queued
        run = await services.get_agent_run(user, run_id, db=server.db)
        assert run["status"] == services.RUN_STATUS_QUEUED

        # Transition to running
        updated = await services.update_agent_run(user, run_id, {"status": services.RUN_STATUS_RUNNING}, db=server.db)
        assert updated["status"] == services.RUN_STATUS_RUNNING

        # Add execution step
        step = await services.create_agent_run_step(
            user,
            run_id=run_id,
            thread_id=thread_id,
            step_type="tool_call",
            input_data={"tool": "list_clients"},
            output_data={"count": 3},
            status="completed",
            db=server.db,
        )
        assert step["id"] is not None
        assert step["step_type"] == "tool_call"
        assert step["firm_id"] == user.firm_id

        # List steps
        steps = await services.list_agent_run_steps(user, run_id, db=server.db)
        assert len(steps) == 1
        assert steps[0]["id"] == step["id"]

        # Transition to completed
        final_run = await services.update_agent_run(user, run_id, {"status": services.RUN_STATUS_COMPLETED}, db=server.db)
        assert final_run["status"] == services.RUN_STATUS_COMPLETED

        # Attempt invalid status
        with pytest.raises(Exception) as exc_info:
            await services.update_agent_run(user, run_id, {"status": "invented_status"}, db=server.db)
        assert "Invalid run status" in str(exc_info.value)

    asyncio.run(_test())


# ===========================================================================
# 7. Idempotency (Header and Body)
# ===========================================================================
def test_agent_idempotency_via_header_and_body(client):
    data, headers = _signup(client, f"idempotent_{uuid.uuid4().hex[:6]}@testfirm.com", "Idempotent Firm")
    user_id = data["user"]["id"]
    firm_id = data["firm"]["id"]
    user = AuthedUser(user_id=user_id, firm_id=firm_id, token_version=1)

    import asyncio

    # Test 1: Idempotency via Idempotency-Key header
    idem_header = {"Idempotency-Key": f"header-idem-{uuid.uuid4().hex}"}
    res1 = client.post(
        "/api/agent/messages",
        json={"text": "Idempotent query via header"},
        headers={**headers, **idem_header},
    )
    assert res1.status_code == 202
    body1 = res1.json()
    run1_id = body1["run_id"]
    thread1_id = body1["thread_id"]

    # Immediate replay with same header
    res2 = client.post(
        "/api/agent/messages",
        json={"text": "Idempotent query via header"},
        headers={**headers, **idem_header},
    )
    assert res2.status_code == 202
    body2 = res2.json()
    assert body2["run_id"] == run1_id
    assert body2["thread_id"] == thread1_id

    # Verify only 1 run and 1 message created in DB
    async def _verify_counts():
        runs = await server.db.agent_runs.find(
            scoped(server.db.agent_runs, user, {"idempotency_key": idem_header["Idempotency-Key"]})
        ).to_list(10)
        assert len(runs) == 1
        messages = await server.db.agent_messages.find(
            scoped(server.db.agent_messages, user, {"thread_id": thread1_id})
        ).to_list(10)
        assert len(messages) == 1

    asyncio.run(_verify_counts())

    # Test 2: Idempotency via JSON body idempotency_key
    idem_body_key = f"body-idem-{uuid.uuid4().hex}"
    res3 = client.post(
        "/api/agent/messages",
        json={"text": "Idempotent query via body", "idempotency_key": idem_body_key},
        headers=headers,
    )
    assert res3.status_code == 202
    body3 = res3.json()
    run3_id = body3["run_id"]

    # Replay with same body key
    res4 = client.post(
        "/api/agent/messages",
        json={"text": "Idempotent query via body", "idempotency_key": idem_body_key},
        headers=headers,
    )
    assert res4.status_code == 202
    body4 = res4.json()
    assert body4["run_id"] == run3_id


# ===========================================================================
# 8. Cross-Tenant Idempotency Isolation
# ===========================================================================
def test_cross_tenant_idempotency_isolation(client):
    shared_idem_key = f"shared-idem-key-{uuid.uuid4().hex}"

    # Firm A uses the key
    data_a, headers_a = _signup(client, f"idem_a_{uuid.uuid4().hex[:6]}@firma.com", "Firm A")
    res_a = client.post(
        "/api/agent/messages",
        json={"text": "Firm A query", "idempotency_key": shared_idem_key},
        headers=headers_a,
    )
    assert res_a.status_code == 202
    run_a_id = res_a.json()["run_id"]

    # Firm B uses the exact same key
    data_b, headers_b = _signup(client, f"idem_b_{uuid.uuid4().hex[:6]}@firmb.com", "Firm B")
    res_b = client.post(
        "/api/agent/messages",
        json={"text": "Firm B query", "idempotency_key": shared_idem_key},
        headers=headers_b,
    )
    assert res_b.status_code == 202
    run_b_id = res_b.json()["run_id"]

    # Strict isolation: Firm B gets its own run, NOT Firm A's run
    assert run_a_id != run_b_id
    assert res_a.json()["thread_id"] != res_b.json()["thread_id"]


# ===========================================================================
# 9. Invalid IDs Handling
# ===========================================================================
def test_invalid_ids_agent_routes(client):
    _, headers = _signup(client, f"invalid_ids_{uuid.uuid4().hex[:6]}@testfirm.com", "Invalid IDs Firm")

    # GET with whitespace run_id (%20%20)
    res = client.get("/api/agent/runs/%20%20", headers=headers)
    assert res.status_code == 400

    # GET with oversized run_id (>256 chars)
    oversized_id = "x" * 300
    res = client.get(f"/api/agent/runs/{oversized_id}", headers=headers)
    assert res.status_code == 400

    # GET with nonexistent random ID
    res = client.get(f"/api/agent/runs/nonexistent-run-{uuid.uuid4()}", headers=headers)
    assert res.status_code == 404

    # POST with nonexistent thread_id
    res = client.post(
        "/api/agent/messages",
        json={"text": "Hello", "thread_id": f"nonexistent-thread-{uuid.uuid4()}"},
        headers=headers,
    )
    assert res.status_code == 404

    # POST with oversized thread_id (>256 chars)
    res = client.post(
        "/api/agent/messages",
        json={"text": "Hello", "thread_id": "t" * 300},
        headers=headers,
    )
    assert res.status_code == 400

    # POST with oversized idempotency_key (>256 chars)
    res = client.post(
        "/api/agent/messages",
        json={"text": "Hello", "idempotency_key": "k" * 300},
        headers=headers,
    )
    assert res.status_code == 400

    # POST with whitespace thread_id creates new thread cleanly
    res = client.post(
        "/api/agent/messages",
        json={"text": "Whitespace thread id", "thread_id": "   "},
        headers=headers,
    )
    assert res.status_code == 202
    assert res.json()["thread_id"] is not None


# ===========================================================================
# 10. Safe Progress and Status Exposure (Never Leaks Internal Thoughts)
# ===========================================================================
def test_agent_run_safe_status_exposure(client):
    import asyncio

    data, headers = _signup(client, f"safe_status_{uuid.uuid4().hex[:6]}@testfirm.com", "Safe Status Firm")
    user_id = data["user"]["id"]
    firm_id = data["firm"]["id"]
    user = AuthedUser(user_id=user_id, firm_id=firm_id, token_version=1)

    async def _setup():
        # Create a run directly in DB with sensitive thoughts & prompts in metadata
        run_id = f"run-leak-test-{uuid.uuid4().hex}"
        thread_id = f"thread-leak-test-{uuid.uuid4().hex}"
        doc = {
            "id": run_id,
            "firm_id": firm_id,
            "thread_id": thread_id,
            "status": "running",
            "created_by": user_id,
            "created_at": "2026-09-19T10:00:00Z",
            "started_at": "2026-09-19T10:00:01Z",
            "completed_at": None,
            "error": "Error: connection timeout to backend internal",
            "metadata": {
                "initial_message_id": "msg-001",
                "thought": "INTERNAL PRIVATE REASONING: DO NOT SHOW USER",
                "prompt": "SYSTEM PROMPT: You are LedgerLens AI",
                "system_prompt": "SECRET SYSTEM PROMPT",
                "token": "secret_internal_token_xyz",
                "raw_response": "{'some': 'raw'}",
                "internal": "debug_data",
                "safe_step_count": 2,
            },
        }
        await server.db.agent_runs.insert_one(doc)
        return run_id

    run_id = asyncio.run(_setup())

    # Query GET /api/agent/runs/{run_id}
    res = client.get(f"/api/agent/runs/{run_id}", headers=headers)
    assert res.status_code == 200
    run_body = res.json()

    # Verify safe fields present
    assert run_body["id"] == run_id
    assert run_body["status"] == "running"
    assert run_body["metadata"]["initial_message_id"] == "msg-001"
    assert run_body["metadata"]["safe_step_count"] == 2

    # Verify secrets and thoughts NEVER exposed
    assert "thought" not in run_body["metadata"]
    assert "prompt" not in run_body["metadata"]
    assert "system_prompt" not in run_body["metadata"]
    assert "token" not in run_body["metadata"]
    assert "raw_response" not in run_body["metadata"]
    assert "internal" not in run_body["metadata"]


# ===========================================================================
# 11. PostgreSQL Persistence Collections Verification
# ===========================================================================
def test_agent_collections_persistence_and_scoping():
    import asyncio

    user_a = AuthedUser(user_id="u-col-1", firm_id="firm-col-a", token_version=1)
    user_b = AuthedUser(user_id="u-col-2", firm_id="firm-col-b", token_version=1)

    async def _test():
        # Verify COLLECTIONS registration
        from database import COLLECTIONS
        for col_name in ("agent_threads", "agent_messages", "agent_runs", "agent_run_steps"):
            assert col_name in COLLECTIONS

        # Insert documents for firm A
        t_id = f"t-{uuid.uuid4().hex[:6]}"
        m_id = f"m-{uuid.uuid4().hex[:6]}"
        r_id = f"r-{uuid.uuid4().hex[:6]}"
        s_id = f"s-{uuid.uuid4().hex[:6]}"

        await server.db.agent_threads.insert_one({"id": t_id, "firm_id": user_a.firm_id, "title": "Test Thread"})
        await server.db.agent_messages.insert_one({"id": m_id, "firm_id": user_a.firm_id, "thread_id": t_id, "text": "Msg"})
        await server.db.agent_runs.insert_one({"id": r_id, "firm_id": user_a.firm_id, "thread_id": t_id, "status": "queued"})
        await server.db.agent_run_steps.insert_one({"id": s_id, "firm_id": user_a.firm_id, "run_id": r_id, "step_type": "tool_call"})

        # Scoped queries for user A find them
        assert await server.db.agent_threads.find_one(scoped(server.db.agent_threads, user_a, {"id": t_id})) is not None
        assert await server.db.agent_messages.find_one(scoped(server.db.agent_messages, user_a, {"id": m_id})) is not None
        assert await server.db.agent_runs.find_one(scoped(server.db.agent_runs, user_a, {"id": r_id})) is not None
        assert await server.db.agent_run_steps.find_one(scoped(server.db.agent_run_steps, user_a, {"id": s_id})) is not None

        # Scoped queries for user B cannot find them
        assert await server.db.agent_threads.find_one(scoped(server.db.agent_threads, user_b, {"id": t_id})) is None
        assert await server.db.agent_messages.find_one(scoped(server.db.agent_messages, user_b, {"id": m_id})) is None
        assert await server.db.agent_runs.find_one(scoped(server.db.agent_runs, user_b, {"id": r_id})) is None
        assert await server.db.agent_run_steps.find_one(scoped(server.db.agent_run_steps, user_b, {"id": s_id})) is None
    asyncio.run(_test())
