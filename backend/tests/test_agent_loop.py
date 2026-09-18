"""Comprehensive test suite for the Claude Agent Loop (Step 5).

Verifies:
1. Happy Path Execution:
   - Single-turn final answer completes run and persists assistant message.
   - Tool -> Result -> Final flow (tool execution, step recording, follow-up Claude turn).
   - Multi-step tool calls sequence (e.g. list_clients -> list_files -> final).
2. Protocol & Parsing Validation:
   - Non-JSON / malformed responses fail closed (status='failed').
   - Ambiguous responses containing both 'tool' and 'final' fail closed.
   - Responses containing neither 'tool' nor 'final' fail closed.
   - Unknown tools requested by Claude fail closed.
   - Invalid arguments requested by Claude fail closed.
3. Resilience & Boundary Safety:
   - Tool failure (e.g. 404 client) is safely fed back to Claude and handled.
   - LLM generation failure / exception marks run failed.
   - Maximum 12 turns limit stops runaway loops.
   - Maximum 10 tool calls limit stops excessive tool invocation.
4. Concurrency & Cancellation:
   - Active cancellation stops loop immediately and marks run 'cancelled'.
   - POST /api/agent/runs/{run_id}/cancel cancels run.
   - Idempotent: duplicate trigger on same run_id does not double-execute.
5. Security & Privacy:
   - Internal 'thought' field is NEVER exposed in the assistant's message text.
   - Cross-tenant run access, cancellation, and tool execution remain strictly isolated (404).
   - Unauthenticated requests remain rejected (401).
   - All tests use MockClaudeProvider (zero paid/live API calls).
"""
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

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
from agent.loop import (
    MAX_CLAUDE_TURNS,
    MAX_TOOL_CALLS,
    request_run_cancellation,
    run_agent_loop,
    set_global_llm_provider,
)
from auth_dep import AuthedUser


class MockClaudeProvider:
    """Mock LLM provider returning predefined responses in sequence without live API calls."""
    def __init__(self, responses: Optional[List[str]] = None, side_effect=None):
        self.responses = list(responses or [])
        self.side_effect = side_effect
        self.calls: List[Dict[str, Any]] = []

    def generate(self, prompt: str, system_prompt: Optional[str] = None, **kwargs) -> Optional[str]:
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt, "kwargs": kwargs})
        if self.side_effect:
            return self.side_effect(prompt, system_prompt)
        if self.responses:
            return self.responses.pop(0)
        return None


@pytest.fixture(autouse=True)
def cleanup_llm_provider():
    yield
    set_global_llm_provider(None)


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
# 1. Happy Path & Conversation Flow Tests
# ===========================================================================
def test_successful_single_turn_final_response():
    firm_id = f"firm_{uuid.uuid4().hex[:6]}"
    user = AuthedUser(user_id="usr-1", firm_id=firm_id, token_version=1)

    async def _test():
        # Setup run
        msg_res = await services.post_agent_message(user, "What is LedgerLens?", db=server.db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        mock = MockClaudeProvider(responses=[
            json.dumps({
                "thought": "Direct question about product capabilities.",
                "final": "LedgerLens is a CPA audit and document verification platform.",
            })
        ])

        result = await run_agent_loop(user, run_id, thread_id, llm_provider=mock, db=server.db)
        assert result["status"] == services.RUN_STATUS_COMPLETED

        # Verify run in DB
        run = await services.get_agent_run(user, run_id, db=server.db)
        assert run["status"] == services.RUN_STATUS_COMPLETED
        assert run["completed_at"] is not None
        assert run["error"] is None

        # Verify message in thread
        messages = await services.list_agent_messages(user, thread_id, db=server.db)
        assert len(messages) == 2  # user + assistant
        assistant_msg = messages[1]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg["text"] == "LedgerLens is a CPA audit and document verification platform."
        # THOUGHT MUST NOT BE EXPOSED IN MESSAGE TEXT
        assert "Direct question" not in assistant_msg["text"]

    asyncio.run(_test())


def test_tool_result_final_flow():
    firm_id = f"firm_{uuid.uuid4().hex[:6]}"
    user = AuthedUser(user_id="usr-1", firm_id=firm_id, token_version=1)

    async def _test():
        # Create client so list_clients has data
        await services.create_client(user, name="Delta Partners", client_type="Partnership", db=server.db)

        msg_res = await services.post_agent_message(user, "List my clients please", db=server.db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        mock = MockClaudeProvider(responses=[
            # Turn 1: Call list_clients
            json.dumps({
                "thought": "The user wants to see their clients. I will call list_clients.",
                "tool": "list_clients",
                "args": {},
            }),
            # Turn 2: Final response
            json.dumps({
                "thought": "I have the client list. Summarizing for accountant.",
                "final": "You have 1 active client: Delta Partners (Partnership).",
            }),
        ])

        result = await run_agent_loop(user, run_id, thread_id, llm_provider=mock, db=server.db)
        assert result["status"] == services.RUN_STATUS_COMPLETED
        assert len(mock.calls) == 2

        # Verify run steps stored
        steps = await services.list_agent_run_steps(user, run_id, db=server.db)
        step_types = [s["step_type"] for s in steps]
        assert "tool_call" in step_types
        assert "tool_result" in step_types

        # Verify final message
        messages = await services.list_agent_messages(user, thread_id, db=server.db)
        assert len(messages) == 2
        assert "Delta Partners" in messages[1]["text"]

    asyncio.run(_test())


def test_multiple_tool_calls_sequence():
    firm_id = f"firm_{uuid.uuid4().hex[:6]}"
    user = AuthedUser(user_id="usr-1", firm_id=firm_id, token_version=1)

    async def _test():
        c = await services.create_client(user, name="Apex Holdings", db=server.db)
        client_id = c["id"]
        await services.save_client_file(user, client_id, "trial_balance.csv", b"acc,bal\n101,500\n", db=server.db)

        msg_res = await services.post_agent_message(user, "Check files for Apex", db=server.db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        mock = MockClaudeProvider(responses=[
            # Step 1: list_clients to find client_id
            json.dumps({
                "thought": "Locating client Apex.",
                "tool": "list_clients",
                "args": {},
            }),
            # Step 2: list_files with client_id
            json.dumps({
                "thought": "Found client Apex. Listing files.",
                "tool": "list_files",
                "args": {"client_id": client_id},
            }),
            # Step 3: Final answer
            json.dumps({
                "thought": "Reporting file found.",
                "final": "Apex Holdings has 1 uploaded file: trial_balance.csv.",
            }),
        ])

        result = await run_agent_loop(user, run_id, thread_id, llm_provider=mock, db=server.db)
        assert result["status"] == services.RUN_STATUS_COMPLETED
        assert len(mock.calls) == 3

        # 2 tool calls -> 4 steps (2 calls + 2 results)
        steps = await services.list_agent_run_steps(user, run_id, db=server.db)
        tool_calls = [s for s in steps if s["step_type"] == "tool_call"]
        assert len(tool_calls) == 2

    asyncio.run(_test())


# ===========================================================================
# 2. Protocol Violations & Malformed Response Tests
# ===========================================================================
def test_invalid_json_response_fails_run():
    user = AuthedUser(user_id="usr-1", firm_id="firm-test", token_version=1)

    async def _test():
        msg_res = await services.post_agent_message(user, "Hi", db=server.db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        mock = MockClaudeProvider(responses=["Plain text response without any JSON format."])
        result = await run_agent_loop(user, run_id, thread_id, llm_provider=mock, db=server.db)
        assert result["status"] == services.RUN_STATUS_FAILED
        assert "could not be parsed into JSON" in result["error"]

        run = await services.get_agent_run(user, run_id, db=server.db)
        assert run["status"] == services.RUN_STATUS_FAILED
        assert run["error"] is not None

    asyncio.run(_test())


def test_ambiguous_response_both_tool_and_final():
    user = AuthedUser(user_id="usr-1", firm_id="firm-test", token_version=1)

    async def _test():
        msg_res = await services.post_agent_message(user, "Hi", db=server.db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        mock = MockClaudeProvider(responses=[
            json.dumps({
                "thought": "both",
                "tool": "list_clients",
                "args": {},
                "final": "I'm finished anyway",
            })
        ])
        result = await run_agent_loop(user, run_id, thread_id, llm_provider=mock, db=server.db)
        assert result["status"] == services.RUN_STATUS_FAILED
        assert "both 'tool' and 'final'" in result["error"]

    asyncio.run(_test())


def test_protocol_violation_neither_tool_nor_final():
    user = AuthedUser(user_id="usr-1", firm_id="firm-test", token_version=1)

    async def _test():
        msg_res = await services.post_agent_message(user, "Hi", db=server.db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        mock = MockClaudeProvider(responses=[
            json.dumps({"thought": "Thinking only, no tool and no final answer"})
        ])
        result = await run_agent_loop(user, run_id, thread_id, llm_provider=mock, db=server.db)
        assert result["status"] == services.RUN_STATUS_FAILED
        assert "neither 'tool' nor 'final'" in result["error"]

    asyncio.run(_test())


def test_unknown_tool_fails_run():
    user = AuthedUser(user_id="usr-1", firm_id="firm-test", token_version=1)

    async def _test():
        msg_res = await services.post_agent_message(user, "Hi", db=server.db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        mock = MockClaudeProvider(responses=[
            json.dumps({"thought": "hack", "tool": "delete_database", "args": {}})
        ])
        result = await run_agent_loop(user, run_id, thread_id, llm_provider=mock, db=server.db)
        assert result["status"] == services.RUN_STATUS_FAILED
        assert "Unknown tool 'delete_database'" in result["error"]

    asyncio.run(_test())


def test_invalid_tool_arguments_fails_run():
    user = AuthedUser(user_id="usr-1", firm_id="firm-test", token_version=1)

    async def _test():
        msg_res = await services.post_agent_message(user, "Hi", db=server.db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        # Missing required client_id for list_files
        mock = MockClaudeProvider(responses=[
            json.dumps({"thought": "t", "tool": "list_files", "args": {}})
        ])
        result = await run_agent_loop(user, run_id, thread_id, llm_provider=mock, db=server.db)
        assert result["status"] == services.RUN_STATUS_FAILED
        assert "Missing required argument" in result["error"]

    asyncio.run(_test())


# ===========================================================================
# 3. Resilience, Tool Error Handling, and Boundaries
# ===========================================================================
def test_tool_failure_handled_in_subsequent_turn():
    user = AuthedUser(user_id="usr-1", firm_id="firm-test", token_version=1)

    async def _test():
        msg_res = await services.post_agent_message(user, "Check client 999", db=server.db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        mock = MockClaudeProvider(responses=[
            # Turn 1: Try nonexistent client
            json.dumps({
                "thought": "Query files",
                "tool": "list_files",
                "args": {"client_id": "nonexistent-id-999"},
            }),
            # Turn 2: Claude sees tool failure result and reports clearly to user
            json.dumps({
                "thought": "Client was not found in firm records. Explaining to user.",
                "final": "Client nonexistent-id-999 does not exist in your firm.",
            }),
        ])

        result = await run_agent_loop(user, run_id, thread_id, llm_provider=mock, db=server.db)
        assert result["status"] == services.RUN_STATUS_COMPLETED

        messages = await services.list_agent_messages(user, thread_id, db=server.db)
        assert "does not exist" in messages[1]["text"]

    asyncio.run(_test())


def test_llm_generation_failure_marks_run_failed():
    user = AuthedUser(user_id="usr-1", firm_id="firm-test", token_version=1)

    async def _test():
        msg_res = await services.post_agent_message(user, "Hi", db=server.db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        def _raise_error(*args, **kwargs):
            raise RuntimeError("Fal.ai API timeout after 60s")

        mock = MockClaudeProvider(side_effect=_raise_error)
        result = await run_agent_loop(user, run_id, thread_id, llm_provider=mock, db=server.db)
        assert result["status"] == services.RUN_STATUS_FAILED
        assert "Fal.ai API timeout" in result["error"]

    asyncio.run(_test())


def test_maximum_turns_limit():
    user = AuthedUser(user_id="usr-1", firm_id="firm-test", token_version=1)

    async def _test():
        msg_res = await services.post_agent_message(user, "Loop forever", db=server.db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        # Return tool call for list_templates every turn
        mock = MockClaudeProvider(responses=[
            json.dumps({"thought": f"Turn {i}", "tool": "list_templates", "args": {}})
            for i in range(MAX_CLAUDE_TURNS + 2)
        ])

        result = await run_agent_loop(user, run_id, thread_id, llm_provider=mock, db=server.db)
        assert result["status"] == services.RUN_STATUS_FAILED
        assert "maximum" in result["error"]

    asyncio.run(_test())


def test_maximum_tool_calls_limit():
    user = AuthedUser(user_id="usr-1", firm_id="firm-test", token_version=1)

    async def _test():
        msg_res = await services.post_agent_message(user, "Call too many tools", db=server.db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        mock = MockClaudeProvider(responses=[
            json.dumps({"thought": f"Call {i}", "tool": "list_templates", "args": {}})
            for i in range(MAX_TOOL_CALLS + 3)
        ])

        result = await run_agent_loop(user, run_id, thread_id, llm_provider=mock, db=server.db)
        assert result["status"] == services.RUN_STATUS_FAILED
        assert "tool call limit" in result["error"].lower() or "maximum" in result["error"].lower()

    asyncio.run(_test())


# ===========================================================================
# 4. Concurrency, Cancellation & Idempotency
# ===========================================================================
def test_cancellation_before_execution():
    user = AuthedUser(user_id="usr-1", firm_id="firm-test", token_version=1)

    async def _test():
        msg_res = await services.post_agent_message(user, "Cancel me", db=server.db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        # Signal cancellation before loop starts
        request_run_cancellation(run_id)

        mock = MockClaudeProvider(responses=[json.dumps({"thought": "t", "final": "never called"})])
        result = await run_agent_loop(user, run_id, thread_id, llm_provider=mock, db=server.db)
        assert result["status"] == services.RUN_STATUS_CANCELLED
        assert len(mock.calls) == 0

        run = await services.get_agent_run(user, run_id, db=server.db)
        assert run["status"] == services.RUN_STATUS_CANCELLED

    asyncio.run(_test())


def test_cancellation_endpoint_via_http(client):
    data, headers = _signup(client, f"cancel_{uuid.uuid4().hex[:6]}@firm.com", "Cancel Firm")

    # Post message
    post_res = client.post("/api/agent/messages", json={"text": "Query to cancel"}, headers=headers)
    assert post_res.status_code == 202
    run_id = post_res.json()["run_id"]

    # Cancel endpoint
    cancel_res = client.post(f"/api/agent/runs/{run_id}/cancel", headers=headers)
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == services.RUN_STATUS_CANCELLED

    # Check status
    status_res = client.get(f"/api/agent/runs/{run_id}", headers=headers)
    assert status_res.status_code == 200
    assert status_res.json()["status"] == services.RUN_STATUS_CANCELLED


def test_idempotent_duplicate_run_execution():
    user = AuthedUser(user_id="usr-1", firm_id="firm-test", token_version=1)

    async def _test():
        msg_res = await services.post_agent_message(user, "Once only", db=server.db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        mock = MockClaudeProvider(responses=[
            json.dumps({"thought": "t", "final": "First run success"})
        ])

        # First run succeeds
        res1 = await run_agent_loop(user, run_id, thread_id, llm_provider=mock, db=server.db)
        assert res1["status"] == services.RUN_STATUS_COMPLETED

        # Attempt second run on same run_id
        res2 = await run_agent_loop(user, run_id, thread_id, llm_provider=mock, db=server.db)
        assert res2["status"] == services.RUN_STATUS_COMPLETED

        # Message is NOT duplicated
        messages = await services.list_agent_messages(user, thread_id, db=server.db)
        assert len(messages) == 2  # exactly 1 user + 1 assistant

    asyncio.run(_test())


# ===========================================================================
# 5. Security & Multi-Tenancy Isolation
# ===========================================================================
def test_cross_tenant_isolation_runs_and_cancel(client):
    data_a, headers_a = _signup(client, f"firm_a_{uuid.uuid4().hex[:6]}@firma.com", "Firm A")
    res_a = client.post("/api/agent/messages", json={"text": "Firm A instructions"}, headers=headers_a)
    run_a_id = res_a.json()["run_id"]

    data_b, headers_b = _signup(client, f"firm_b_{uuid.uuid4().hex[:6]}@firmb.com", "Firm B")

    # Firm B cannot GET Firm A run
    assert client.get(f"/api/agent/runs/{run_a_id}", headers=headers_b).status_code == 404

    # Firm B cannot cancel Firm A run
    assert client.post(f"/api/agent/runs/{run_a_id}/cancel", headers=headers_b).status_code == 404
