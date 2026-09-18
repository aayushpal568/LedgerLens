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
