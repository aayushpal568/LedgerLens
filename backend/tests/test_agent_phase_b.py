"""Test suite for Phase B backend endpoints: thread messages, run steps, and run metadata."""
import asyncio
import os
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATA_BACKEND", "memory")
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-32-chars-long-ledgerlens-suite")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")

import pytest
from fastapi.testclient import TestClient

from auth_dep import AuthedUser
import database
import server
import services
from tokens import create_access_token


@pytest.fixture
def test_setup():
    mem_db = database.MemoryDatabase()
    server.db = mem_db

    user_a = AuthedUser(user_id="user-a-b", firm_id="firm-alpha-b", token_version=1)
    user_b = AuthedUser(user_id="user-b-b", firm_id="firm-beta-b", token_version=1)

    asyncio.run(mem_db.users.insert_one({
        "id": user_a.user_id,
        "firm_id": user_a.firm_id,
        "token_version": user_a.token_version,
        "email": "user_a@alpha.com",
    }))
    asyncio.run(mem_db.users.insert_one({
        "id": user_b.user_id,
        "firm_id": user_b.firm_id,
        "token_version": user_b.token_version,
        "email": "user_b@beta.com",
    }))

    token_a = create_access_token(user_a.user_id, user_a.firm_id, user_a.token_version)
    token_b = create_access_token(user_b.user_id, user_b.firm_id, user_b.token_version)

    return {
        "db": mem_db,
        "user_a": user_a,
        "user_b": user_b,
        "headers_a": {"Authorization": f"Bearer {token_a}"},
        "headers_b": {"Authorization": f"Bearer {token_b}"},
        "client": TestClient(server.app),
    }


def test_list_agent_thread_messages_endpoint(test_setup):
    async def _test():
        user_a = test_setup["user_a"]
        client = test_setup["client"]
        headers_a = test_setup["headers_a"]
        headers_b = test_setup["headers_b"]
        db = test_setup["db"]

        # Post a message creating thread and run
        msg_res = await services.post_agent_message(user_a, "Hello AI", db=db)
        thread_id = msg_res["thread_id"]

        # Insert an assistant response
        await db.agent_messages.insert_one({
            "id": "msg-assist-1",
            "firm_id": user_a.firm_id,
            "thread_id": thread_id,
            "role": "assistant",
            "text": "Hello, how can I help you?",
            "created_at": services.now_iso(),
        })

        # Firm A retrieves messages -> 200 OK
        resp = client.get(f"/api/agent/threads/{thread_id}/messages", headers=headers_a)
        assert resp.status_code == 200
        msgs = resp.json()
        assert len(msgs) == 2
        assert msgs[0]["text"] == "Hello AI"
        assert msgs[1]["text"] == "Hello, how can I help you?"

        # Firm B attempts retrieval on Firm A's thread -> 404
        resp_b = client.get(f"/api/agent/threads/{thread_id}/messages", headers=headers_b)
        assert resp_b.status_code == 404

        # Unauthenticated -> 401
        resp_unauth = client.get(f"/api/agent/threads/{thread_id}/messages")
        assert resp_unauth.status_code == 401

    asyncio.run(_test())


def test_list_agent_run_steps_endpoint_hides_thoughts(test_setup):
    async def _test():
        user_a = test_setup["user_a"]
        client = test_setup["client"]
        headers_a = test_setup["headers_a"]
        headers_b = test_setup["headers_b"]
        db = test_setup["db"]

        msg_res = await services.post_agent_message(user_a, "Check template", db=db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        # Insert a thought step and a tool step
        await services.create_agent_run_step(
            user_a,
            run_id=run_id,
            thread_id=thread_id,
            step_type="thought",
            input_data={"thought": "Secret internal reasoning"},
            status="completed",
            db=db,
        )
        await services.create_agent_run_step(
            user_a,
            run_id=run_id,
            thread_id=thread_id,
            step_type="tool_call",
            input_data={"tool": "list_templates"},
            status="completed",
            db=db,
        )

        # Firm A retrieves steps -> 200 OK
        resp = client.get(f"/api/agent/runs/{run_id}/steps", headers=headers_a)
        assert resp.status_code == 200
        steps = resp.json()
        assert len(steps) == 1
        assert steps[0]["step_type"] == "tool_call"
        # Ensure thoughts are filtered out
        assert not any(s["step_type"] == "thought" for s in steps)

        # Firm B attempts retrieval -> 404
        resp_b = client.get(f"/api/agent/runs/{run_id}/steps", headers=headers_b)
        assert resp_b.status_code == 404

    asyncio.run(_test())


def test_get_agent_run_exposes_metadata(test_setup):
    async def _test():
        user_a = test_setup["user_a"]
        client = test_setup["client"]
        headers_a = test_setup["headers_a"]
        db = test_setup["db"]

        msg_res = await services.post_agent_message(user_a, "Scan action", db=db)
        run_id = msg_res["run_id"]

        # Update run with approval metadata
        await services.update_agent_run(
            user_a,
            run_id,
            {
                "status": "waiting_for_approval",
                "metadata": {"approval_id": "appr-12345", "tool": "run_scan"},
            },
            db=db,
        )

        resp = client.get(f"/api/agent/runs/{run_id}", headers=headers_a)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "waiting_for_approval"
        assert data["metadata"]["approval_id"] == "appr-12345"

    asyncio.run(_test())
