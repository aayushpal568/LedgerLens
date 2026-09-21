"""Comprehensive test suite for Phase A: Agent Actions, Approvals Engine, and Grounding.

Hermetic test suite:
- Uses in-memory database.
- Uses MockClaudeProvider with predictable responses (zero live API calls).
- Tests action tools, approval lifecycle, anti-tampering, 10-minute TTL, IDOR tenant isolation,
  agent loop pause/resume, and deterministic factual grounding.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
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

from agent.approvals import (
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_EXPIRED,
    APPROVAL_STATUS_PENDING,
    APPROVAL_STATUS_REJECTED,
    compute_args_hash,
    create_approval,
    get_approval,
    list_approvals,
)
from agent.grounding import extract_grounded_facts, verify_grounding
from agent.loop import resume_agent_run, run_agent_loop
from agent.registry import default_registry, execute_tool
from auth_dep import AuthedUser
import database
import server
import services


class MockClaudeProvider:
    """Configurable mock LLM provider for testing multi-turn agent execution."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0
        self.prompts_received = []

    def generate(self, prompt: str, system_prompt: str = None, **kwargs) -> str:
        self.prompts_received.append(prompt)
        if self.call_count < len(self.responses):
            resp = self.responses[self.call_count]
            self.call_count += 1
            return resp if isinstance(resp, str) else json.dumps(resp)
        return json.dumps({
            "thought": "No more mock responses, wrapping up.",
            "final": "Mock execution completed.",
        })


@pytest.fixture
def test_setup():
    """Hermetic setup fixture configuring database and two distinct test firms."""
    mem_db = database.MemoryDatabase()
    server.db = mem_db

    # Firm A
    user_a = AuthedUser(user_id="user-a-1", firm_id="firm-alpha", token_version=1)
    # Firm B
    user_b = AuthedUser(user_id="user-b-1", firm_id="firm-beta", token_version=1)

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

    return {
        "db": mem_db,
        "user_a": user_a,
        "user_b": user_b,
        "client": TestClient(server.app),
    }


# ===========================================================================
# 1. Action Tools & Registry Tests
# ===========================================================================
def test_action_tools_require_approval(test_setup):
    async def _test():
        user_a = test_setup["user_a"]
        db = test_setup["db"]

        # 1. create_client without approval flag fails
        res = await execute_tool(
            user_a,
            "create_client",
            {"name": "Alpha Corp", "tax_id": "12-3456789"},
            db=db,
            is_approved=False,
        )
        assert res["success"] is False
        assert res["error_type"] == "ApprovalRequired"

        # 2. create_client WITH approval flag succeeds
        res_approved = await execute_tool(
            user_a,
            "create_client",
            {"name": "Alpha Corp", "tax_id": "12-3456789"},
            db=db,
            is_approved=True,
        )
        assert res_approved["success"] is True
        client_res = res_approved["result"]
        assert client_res["name"] == "Alpha Corp"
        assert client_res["tax_id"] == "12-3456789"
        assert "client_id" in client_res

        # 3. run_scan with approval flag succeeds
        client_id = client_res["client_id"]
        tpl = {
            "id": "tpl-1",
            "firm_id": user_a.firm_id,
            "name": "Audit Template",
            "client_type": "Small Business",
            "is_default": True,
            "created_at": services.now_iso(),
        }
        await db.templates.insert_one(tpl)

        scan_res = await execute_tool(
            user_a,
            "run_scan",
            {"client_id": client_id, "template_id": "tpl-1", "expected_period": "2024"},
            db=db,
            is_approved=True,
        )
        assert scan_res["success"] is True
        assert scan_res["result"]["status"] == "queued"
        assert scan_res["result"]["scan_id"] is not None

        # 4. set_finding_review with approval flag
        finding_id = "f-1"
        await db.findings.insert_one({
            "id": finding_id,
            "firm_id": user_a.firm_id,
            "scan_id": scan_res["result"]["scan_id"],
            "status": "unreviewed",
            "created_at": services.now_iso(),
        })

        review_res = await execute_tool(
            user_a,
            "set_finding_review",
            {"finding_id": finding_id, "review_status": "keep", "review_notes": "Verified against ledger"},
            db=db,
            is_approved=True,
        )
        assert review_res["success"] is True
        assert review_res["result"]["review_status"] == "keep"
        updated_finding = await db.findings.find_one({"id": finding_id, "firm_id": user_a.firm_id}, {"_id": 0})
        assert updated_finding["status"] == "keep"

    asyncio.run(_test())


# ===========================================================================
# 2. Canonical Hashing & TTL Expiry Tests
# ===========================================================================
def test_canonical_hash_deterministic():
    args1 = {"name": "Test Co", "tax_id": "999", "active": True}
    args2 = {"active": True, "name": "Test Co", "tax_id": "999"}
    assert compute_args_hash(args1) == compute_args_hash(args2)


def test_approval_ttl_expiry(test_setup):
    async def _test():
        user_a = test_setup["user_a"]
        db = test_setup["db"]

        # Create approval
        approval = await create_approval(
            user_a,
            run_id="run-ttl-1",
            thread_id="th-ttl-1",
            tool_name="create_client",
            proposed_args={"name": "Expired Client"},
            db=db,
        )
        assert approval["status"] == APPROVAL_STATUS_PENDING

        # Artificially expire the approval by updating expires_at to 15 minutes ago
        expired_time = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        await db.agent_approvals.update_one(
            {"id": approval["id"]},
            {"$set": {"expires_at": expired_time}},
        )

        # Retrieval should mark it expired
        fetched = await get_approval(user_a, approval["id"], db=db)
        assert fetched["status"] == APPROVAL_STATUS_EXPIRED

        # Attempting to approve an expired approval must raise 400
        with pytest.raises(Exception) as exc_info:
            await services.approve_agent_approval(user_a, approval["id"], db=db)
        assert "expired" in str(exc_info.value).lower()

    asyncio.run(_test())


# ===========================================================================
# 3. Cross-Tenant IDOR Protection on Approvals
# ===========================================================================
def test_cross_tenant_approval_isolation(test_setup):
    async def _test():
        user_a = test_setup["user_a"]
        user_b = test_setup["user_b"]
        db = test_setup["db"]

        approval_a = await create_approval(
            user_a,
            run_id="run-a-1",
            thread_id="th-a-1",
            tool_name="create_client",
            proposed_args={"name": "Confidential Corp"},
            db=db,
        )

        # User B cannot see Firm A's approval
        with pytest.raises(Exception):
            await get_approval(user_b, approval_a["id"], db=db)

        # User B cannot approve Firm A's approval
        with pytest.raises(Exception):
            await services.approve_agent_approval(user_b, approval_a["id"], db=db)

        # Listing approvals for Firm B returns empty
        b_approvals = await list_approvals(user_b, db=db)
        assert len(b_approvals) == 0

    asyncio.run(_test())


# ===========================================================================
# 4. Agent Loop Pause and Resume: Approved Action
# ===========================================================================
def test_agent_loop_action_approved_lifecycle(test_setup):
    async def _test():
        user_a = test_setup["user_a"]
        db = test_setup["db"]

        # Initialize thread, run, and user message
        msg_res = await services.post_agent_message(
            user_a,
            text="Please create a new client named Beta Logistics",
            db=db,
        )
        thread_id = msg_res["thread_id"]
        run_id = msg_res["run_id"]

        # Turn 1: Claude proposes create_client action
        # Turn 2: After approval, Claude returns final answer
        provider = MockClaudeProvider([
            {
                "thought": "The user wants to create a new client, so I should call create_client.",
                "tool": "create_client",
                "args": {"name": "Beta Logistics", "notes": "Freight partner"},
            },
            {
                "thought": "The client has been created successfully. Now inform the user.",
                "final": "I have created client 'Beta Logistics' for your firm.",
            },
        ])

        # Run agent loop -> should pause for approval!
        result1 = await run_agent_loop(
            user_a,
            run_id,
            thread_id,
            llm_provider=provider,
            db=db,
        )
        assert result1["status"] == services.RUN_STATUS_WAITING_FOR_APPROVAL
        approval_id = result1["approval_id"]

        # Verify run state in database
        run_db = await services.get_agent_run(user_a, run_id, db=db)
        assert run_db["status"] == services.RUN_STATUS_WAITING_FOR_APPROVAL

        # Verify approval record
        appr_doc = await get_approval(user_a, approval_id, db=db)
        assert appr_doc["status"] == APPROVAL_STATUS_PENDING
        assert appr_doc["tool_name"] == "create_client"
        assert appr_doc["proposed_args"]["name"] == "Beta Logistics"

        # User approves via services / API
        await services.approve_agent_approval(user_a, approval_id, db=db)

        # Resume agent run
        result2 = await resume_agent_run(
            user_a,
            approval_id,
            is_approved=True,
            llm_provider=provider,
            db=db,
        )
        assert result2["status"] == services.RUN_STATUS_COMPLETED

        # Verify final assistant message was stored
        messages = await services.list_agent_messages(user_a, thread_id, db=db)
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert "Beta Logistics" in assistant_msgs[0]["text"]

        # Verify client was actually created in firm
        clients = await services.list_clients(user_a, db=db)
        client_names = [c["name"] for c in clients]
        assert "Beta Logistics" in client_names

    asyncio.run(_test())


# ===========================================================================
# 5. Agent Loop Pause and Resume: Rejected Action
# ===========================================================================
def test_agent_loop_action_rejected_lifecycle(test_setup):
    async def _test():
        user_a = test_setup["user_a"]
        db = test_setup["db"]

        msg_res = await services.post_agent_message(
            user_a,
            text="Please create client Acme",
            db=db,
        )
        thread_id = msg_res["thread_id"]
        run_id = msg_res["run_id"]

        provider = MockClaudeProvider([
            {
                "thought": "Proposing create_client for Acme",
                "tool": "create_client",
                "args": {"name": "Acme"},
            },
            {
                "thought": "The action was rejected by the user. Acknowledge this politely.",
                "final": "The action to create client Acme was rejected, so no new client was created.",
            },
        ])

        # Run agent loop -> pauses
        result1 = await run_agent_loop(
            user_a,
            run_id,
            thread_id,
            llm_provider=provider,
            db=db,
        )
        assert result1["status"] == services.RUN_STATUS_WAITING_FOR_APPROVAL
        approval_id = result1["approval_id"]

        # User rejects the action
        await services.reject_agent_approval(user_a, approval_id, reason="Duplicate account", db=db)

        # Resume agent run with rejection
        result2 = await resume_agent_run(
            user_a,
            approval_id,
            is_approved=False,
            reason="Duplicate account",
            llm_provider=provider,
            db=db,
        )
        assert result2["status"] == services.RUN_STATUS_COMPLETED

        # Verify client Acme was NOT created
        clients = await services.list_clients(user_a, db=db)
        assert not any(c["name"] == "Acme" for c in clients)

        # Verify final assistant message
        messages = await services.list_agent_messages(user_a, thread_id, db=db)
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        assert "rejected" in assistant_msgs[0]["text"].lower()

    asyncio.run(_test())


# ===========================================================================
# 6. Maximum run_scan Limits (At most 1 per run)
# ===========================================================================
def test_run_scan_limit_exceeded(test_setup):
    async def _test():
        user_a = test_setup["user_a"]
        db = test_setup["db"]

        msg_res = await services.post_agent_message(user_a, text="Run two scans", db=db)
        thread_id = msg_res["thread_id"]
        run_id = msg_res["run_id"]

        # Create dummy client and template
        c = await services.create_client(user_a, "Client Scan Test", db=db)
        t = {
            "id": "tpl-scan",
            "firm_id": user_a.firm_id,
            "name": "Tpl Scan",
            "created_at": services.now_iso(),
        }
        await db.templates.insert_one(t)

        # Simulate run step where run_scan already executed
        await services.create_agent_run_step(
            user_a,
            run_id=run_id,
            thread_id=thread_id,
            step_type="tool_call",
            input_data={"tool": "run_scan", "args": {"client_id": c["id"], "template_id": "tpl-scan"}},
            db=db,
        )
        await services.create_agent_run_step(
            user_a,
            run_id=run_id,
            thread_id=thread_id,
            step_type="tool_result",
            input_data={"tool": "run_scan"},
            output_data={"scan_id": "scan-1", "success": True},
            db=db,
        )

        # Claude attempts a second run_scan
        provider = MockClaudeProvider([
            {
                "thought": "Running second scan",
                "tool": "run_scan",
                "args": {"client_id": c["id"], "template_id": "tpl-scan"},
            }
        ])

        result = await run_agent_loop(
            user_a,
            run_id,
            thread_id,
            llm_provider=provider,
            db=db,
            is_resume=True,
        )
        assert result["status"] == services.RUN_STATUS_FAILED
        assert "maximum limit of 1 'run_scan'" in result["error"]

    asyncio.run(_test())


# ===========================================================================
# 7. Deterministic Grounding Tests
# ===========================================================================
def test_grounding_verification_detects_rejected_claim():
    steps = [
        {
            "step_type": "tool_call",
            "input_data": {"tool": "create_client", "args": {"name": "FakeCorp"}},
        },
        {
            "step_type": "tool_result",
            "input_data": {"tool": "create_client"},
            "output_data": {
                "success": False,
                "tool": "create_client",
                "status": "rejected",
                "error": "Action was rejected by user.",
                "error_type": "ActionRejected",
            },
        },
    ]

    # Hallucinated answer claiming success despite rejection
    hallucinated_answer = "I successfully created client 'FakeCorp' for your firm."
    res = verify_grounding(hallucinated_answer, steps)
    assert res.is_grounded is False
    assert len(res.violations) > 0
    assert "rejected" in res.safe_text.lower()

    # Honest answer acknowledging rejection
    grounded_answer = "The request to create client FakeCorp was rejected by the user."
    res2 = verify_grounding(grounded_answer, steps)
    assert res2.is_grounded is True


# ===========================================================================
# 8. Approval Endpoints via HTTP TestClient
# ===========================================================================
def test_approval_http_endpoints(test_setup):
    async def _test():
        user_a = test_setup["user_a"]
        user_b = test_setup["user_b"]
        client = test_setup["client"]
        db = test_setup["db"]

        from tokens import create_access_token

        token_a = create_access_token(user_a.user_id, user_a.firm_id, user_a.token_version)
        headers_a = {"Authorization": f"Bearer {token_a}"}

        token_b = create_access_token(user_b.user_id, user_b.firm_id, user_b.token_version)
        headers_b = {"Authorization": f"Bearer {token_b}"}

        # Seed an approval for Firm A
        appr = await create_approval(
            user_a,
            run_id="run-http-1",
            thread_id="th-http-1",
            tool_name="create_client",
            proposed_args={"name": "HTTP Client"},
            db=db,
        )

        # GET /api/agent/approvals
        resp = client.get("/api/agent/approvals", headers=headers_a)
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["id"] == appr["id"]

        # GET /api/agent/approvals/{id}
        resp_single = client.get(f"/api/agent/approvals/{appr['id']}", headers=headers_a)
        assert resp_single.status_code == 200
        assert resp_single.json()["tool_name"] == "create_client"

        # Firm B cannot see it
        resp_b = client.get(f"/api/agent/approvals/{appr['id']}", headers=headers_b)
        assert resp_b.status_code == 404

        # POST /api/agent/approvals/{id}/reject
        resp_reject = client.post(
            f"/api/agent/approvals/{appr['id']}/reject",
            json={"reason": "Not approved"},
            headers=headers_a,
        )
        assert resp_reject.status_code == 202
        assert resp_reject.json()["status"] == APPROVAL_STATUS_REJECTED

    asyncio.run(_test())
