"""Durable Agent Worker, recovery, limits & security tests (V1 Agent Step 5).

Covers the required Step-5 behaviours against the authoritative (memory) DB backend with
fake providers — NO live/paid Claude calls:

1. Atomic claiming & concurrency
   - Two workers claim the same queued run -> exactly one wins.
   - Concurrent FIFO drain hands distinct runs to distinct workers (no double claim).
2. Recovery & retries (idempotent)
   - Stale 'running' run with expired lease is requeued and safely re-executed.
   - Retry on an already-completed run never duplicates the assistant message.
   - A run whose worker failed mid-flight is recoverable via lease expiry.
3. Cancellation
   - Cancelled run is never claimed and never executes (0 provider calls).
   - A retry/worker cannot resurrect a cancelled run.
4. Server-side limits
   - Maximum execution time (wall clock).
   - Cumulative output-token budget.
   - Maximum tool calls / turns (reuses loop counters).
   - Maximum tool-result size (oversized results truncated).
5. Provider failure handling
   - Empty / unavailable provider response fails the run with a SAFE message.
   - Provider exception fails the run with a SAFE message (no secrets/tracebacks).
6. Evidence / grounding
   - Final answer persists an evidence step; hallucinated ids are grounded/flagged.
7. Security / multi-tenancy (server-side only)
   - firm_id is taken from the AuthedUser/run, never from Claude or the frontend.
   - Firm B cannot claim or access Firm A runs/threads/messages/clients.
"""
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATA_BACKEND", "memory")
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-32-chars-long-ledgerlens-suite")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
# Keep the durable poll worker OFF during the suite; tests drive claims explicitly.
os.environ["AGENT_WORKER_ENABLED"] = "false"

import server
import services
from agent.loop import (
    MAX_CLAUDE_TURNS,
    MAX_TOOL_CALLS,
    request_run_cancellation,
    run_agent_loop,
    set_global_llm_provider,
)
from agent.worker import process_next_queued_run, _user_from_run
from auth_dep import AuthedUser


class FakeProvider:
    """Deterministic offline provider; supports an optional per-call async delay."""
    def __init__(self, responses: Optional[List[str]] = None, delay: float = 0.0, side_effect=None):
        self.responses = list(responses or [])
        self.delay = delay
        self.side_effect = side_effect
        self.calls = 0

    async def generate(self, prompt: str, system_prompt: Optional[str] = None, **kwargs) -> Optional[str]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.side_effect:
            return self.side_effect(prompt, self.calls)
        if self.responses:
            return self.responses.pop(0) if self.responses else None
        return None


class SyncFakeProvider:
    """Sync provider path (exercised via asyncio.to_thread) returning a scripted sequence."""
    def __init__(self, responses: Optional[List[str]] = None):
        self.responses = list(responses or [])
        self.calls = 0

    def generate(self, prompt: str, system_prompt: Optional[str] = None, **kwargs) -> Optional[str]:
        self.calls += 1
        if self.responses:
            return self.responses.pop(0) if self.responses else None
        return None


def _user(firm_id: Optional[str] = None) -> AuthedUser:
    return AuthedUser(user_id=f"usr-{uuid.uuid4().hex[:6]}", firm_id=firm_id or f"firm-{uuid.uuid4().hex[:8]}", token_version=1)


def _tool_call(tool: str, args: Optional[dict] = None, thought: str = "t") -> str:
    return json.dumps({"thought": thought, "tool": tool, "args": args or {}})


def _final(text: str, thought: str = "t") -> str:
    return json.dumps({"thought": thought, "final": text})


@pytest.fixture(autouse=True)
def _no_global_provider():
    yield
    set_global_llm_provider(None)


@pytest.fixture(autouse=True)
def _disable_inline_dispatch(monkeypatch):
    # Tests drive execution explicitly (claim / run_agent_loop / worker), so suppress the
    # inline background dispatch to keep queued runs deterministic.
    monkeypatch.setenv("AGENT_INLINE_DISPATCH", "false")


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        yield c


def _signup(client: TestClient, email: str, firm_name: str):
    resp = client.post("/api/auth/signup", json={"email": email, "password": "SecurePass123!", "firm_name": firm_name, "name": f"Admin {firm_name}"})
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    return resp.json(), {"Authorization": f"Bearer {token}"}


# =========================================================================
# 1. Atomic claiming & concurrency
# =========================================================================
def test_two_workers_claim_same_run_exactly_one_wins():
    async def _test():
        u = _user()
        res = await services.post_agent_message(u, "hello", db=server.db)
        run_id, firm_id = res["run_id"], u.firm_id

        outcomes = await asyncio.gather(
            server.db.claim_agent_run(run_id, "worker-A", firm_id, lease_seconds=60),
            server.db.claim_agent_run(run_id, "worker-B", firm_id, lease_seconds=60),
        )
        winners = [o for o in outcomes if o is not None]
        assert len(winners) == 1, "exactly one worker must claim the run"
        assert winners[0]["status"] == "running"
        assert winners[0]["attempt"] == 1
        # The loser cannot have also flipped state / incremented attempt twice.
        stored = await services.get_agent_run(u, run_id, db=server.db)
        assert stored["status"] == "running"
        # The run doc records a single live lease.
        raw = await server.db.agent_runs.find_one({"id": run_id})
        assert raw["attempt"] == 1
        assert raw["worker_id"] in ("worker-A", "worker-B")
        assert raw["lease_expires_at"] is not None
    asyncio.run(_test())


def test_concurrent_fifo_drain_no_double_claim():
    async def _test():
        u = _user()
        run_ids = []
        for i in range(4):
            res = await services.post_agent_message(u, f"msg {i}", db=server.db)
            run_ids.append(res["run_id"])

        claimed_ids = []
        for _ in range(6):  # more attempts than runs -> extra iterations return None
            doc = await server.db.claim_next_queued_agent_run("worker-X", firm_id=u.firm_id, lease_seconds=60)
            if doc is None:
                break
            claimed_ids.append(doc["id"])

        assert len(claimed_ids) == 4, "all queued runs claimed exactly once"
        assert len(set(claimed_ids)) == 4
        assert set(claimed_ids) == set(run_ids)
        # Nothing left to claim for this firm now.
        assert await server.db.claim_next_queued_agent_run("worker-Y", firm_id=u.firm_id, lease_seconds=60) is None
    asyncio.run(_test())


def test_worker_process_next_queued_run_drains_and_executes():
    async def _test():
        u = _user()
        res = await services.post_agent_message(u, "worker drain", db=server.db)
        run_id, thread_id = res["run_id"], res["thread_id"]
        provider = FakeProvider(responses=[_final("Handled by durable worker.")])
        result = await process_next_queued_run(llm_provider=provider)
        assert result is not None and result["status"] == services.RUN_STATUS_COMPLETED
        stored = await server.db.agent_runs.find_one({"id": run_id})
        assert stored["status"] == services.RUN_STATUS_COMPLETED
        # Lease released on completion (terminal state can never be re-claimed).
        assert stored["worker_id"] is None
        assert stored["lease_expires_at"] is None
        assert stored.get("attempt", 0) >= 1
        msgs = await services.list_agent_messages(u, thread_id, db=server.db)
        assert len([m for m in msgs if m["role"] == "assistant"]) == 1
    asyncio.run(_test())


# =========================================================================
# 2. Recovery & idempotent retries
# =========================================================================
def test_stale_running_run_is_requeued_and_retry_executes():
    async def _test():
        u = _user()
        res = await services.post_agent_message(u, "recover me", db=server.db)
        run_id, thread_id = res["run_id"], res["thread_id"]

        # Simulate a worker that crashed while running: lease already expired.
        await server.db.agent_runs.update_one(
            {"id": run_id},
            {"$set": {
                "status": "running",
                "worker_id": "dead-worker",
                "attempt": 1,
                "lease_expires_at": "2000-01-01T00:00:00+00:00",
                "started_at": "2000-01-01T00:00:00+00:00",
            }},
        )

        rec = await services.recover_stale_agent_runs(db=server.db)
        assert rec.get("requeued", 0) >= 1
        stored = await server.db.agent_runs.find_one({"id": run_id})
        assert stored["status"] == "queued"
        assert stored["worker_id"] is None

        # A fresh worker now claims and completes the run.
        provider = FakeProvider(responses=[_final("Recovered and done.")])
        result = await run_agent_loop(u, run_id, thread_id, llm_provider=provider, db=server.db)
        assert result["status"] == services.RUN_STATUS_COMPLETED
        stored = await server.db.agent_runs.find_one({"id": run_id})
        assert stored["attempt"] == 2  # original + one recovery re-claim
        msgs = await services.list_agent_messages(u, thread_id, db=server.db)
        assert len(msgs) == 2  # 1 user + 1 assistant (no duplicate)
    asyncio.run(_test())


def test_live_lease_is_not_stolen_by_recovery():
    async def _test():
        u = _user()
        res = await services.post_agent_message(u, "still working", db=server.db)
        run_id = res["run_id"]
        # A different worker holds a FUTURE lease -> must NOT be recovered.
        await server.db.agent_runs.update_one(
            {"id": run_id},
            {"$set": {"status": "running", "worker_id": "live-worker",
                      "lease_expires_at": services.now_iso().replace("T", "T")}},
        )
        # Extend lease into the future.
        from datetime import datetime, timedelta, timezone
        future = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
        await server.db.agent_runs.update_one({"id": run_id}, {"$set": {"lease_expires_at": future}})
        rec = await services.recover_stale_agent_runs(db=server.db)
        assert rec.get("requeued", 0) == 0
        stored = await server.db.agent_runs.find_one({"id": run_id})
        assert stored["status"] == "running"
        assert stored["worker_id"] == "live-worker"
    asyncio.run(_test())


def test_retry_completed_run_does_not_duplicate_message():
    async def _test():
        u = _user()
        res = await services.post_agent_message(u, "once", db=server.db)
        run_id, thread_id = res["run_id"], res["thread_id"]
        r1 = await run_agent_loop(u, run_id, thread_id, llm_provider=FakeProvider(responses=[_final("Done A")]), db=server.db)
        assert r1["status"] == services.RUN_STATUS_COMPLETED
        # Retry a completed run -> not claimable, no new message.
        r2 = await run_agent_loop(u, run_id, thread_id, llm_provider=FakeProvider(responses=[_final("Done B")]), db=server.db)
        assert r2["status"] == services.RUN_STATUS_COMPLETED
        assert r2.get("claimed") is False
        msgs = await services.list_agent_messages(u, thread_id, db=server.db)
        assistant = [m for m in msgs if m["role"] == "assistant"]
        assert len(assistant) == 1
    asyncio.run(_test())


# =========================================================================
# 3. Cancellation
# =========================================================================
def test_cancelled_run_is_never_claimed_or_executed():
    async def _test():
        u = _user()
        res = await services.post_agent_message(u, "cancel before run", db=server.db)
        run_id = res["run_id"]
        # Persist cancellation via the cancel path (also sets in-memory + DB).
        await services.cancel_agent_run(u, run_id, db=server.db)
        # Direct claim must fail (terminal cancelled).
        claimed = await server.db.claim_agent_run(run_id, "worker-Z", u.firm_id, lease_seconds=60)
        assert claimed is None
        prov = FakeProvider(responses=[_final("should never run")])
        result = await run_agent_loop(u, run_id, res["thread_id"], llm_provider=prov, db=server.db)
        assert result["status"] == services.RUN_STATUS_CANCELLED
        assert prov.calls == 0
    asyncio.run(_test())


def test_cancellation_mid_run_stops_before_next_call():
    async def _test():
        u = _user()
        res = await services.post_agent_message(u, "cancel mid", db=server.db)
        run_id, thread_id = res["run_id"], res["thread_id"]

        state = {"n": 0}

        async def _prov(prompt, system_prompt=None, **kwargs):
            state["n"] += 1
            if state["n"] == 1:
                # A concurrent cancellation lands after the first turn's tool proposal.
                request_run_cancellation(run_id)
                return _tool_call("list_clients", {})
            return _final("unreachable")

        provider = FakeProvider()
        provider.generate = _prov  # type: ignore
        result = await run_agent_loop(u, run_id, thread_id, llm_provider=provider, db=server.db)
        assert result["status"] == services.RUN_STATUS_CANCELLED
        assert state["n"] == 1  # loop stopped before making the next LLM call
        stored = await server.db.agent_runs.find_one({"id": run_id})
        assert stored["status"] == services.RUN_STATUS_CANCELLED
        msgs = await services.list_agent_messages(u, thread_id, db=server.db)
        assert [m for m in msgs if m["role"] == "assistant"] == []
    asyncio.run(_test())


# =========================================================================
# 4. Server-side limits
# =========================================================================
def test_wall_clock_timeout_fails_run(monkeypatch):
    async def _test():
        u = _user()
        res = await services.post_agent_message(u, "slow forever", db=server.db)
        run_id, thread_id = res["run_id"], res["thread_id"]
        monkeypatch.setattr(services, "agent_run_timeout_seconds", lambda: 1)
        # Single tool call that sleeps past the 1s budget; the next turn's guard must time out.
        provider = FakeProvider(delay=1.5, responses=[_tool_call("list_templates", {})])
        result = await run_agent_loop(u, run_id, thread_id, llm_provider=provider, db=server.db)
        assert result["status"] == services.RUN_STATUS_FAILED
        assert "maximum execution time" in result["error"].lower()
    asyncio.run(_test())


def test_output_token_budget_limit(monkeypatch):
    async def _test():
        u = _user()
        res = await services.post_agent_message(u, "token budget", db=server.db)
        run_id, thread_id = res["run_id"], res["thread_id"]
        # Force a tiny budget so the SECOND Claude turn trips the cap.
        monkeypatch.setattr(services, "agent_max_total_output_tokens", lambda: 500)
        provider = FakeProvider(responses=[_tool_call("list_templates", {}) for _ in range(MAX_CLAUDE_TURNS + 3)])
        result = await run_agent_loop(u, run_id, thread_id, llm_provider=provider, db=server.db)
        assert result["status"] == services.RUN_STATUS_FAILED
        assert "output-token budget" in result["error"].lower()
    asyncio.run(_test())


def test_max_tool_calls_limit():
    async def _test():
        u = _user()
        res = await services.post_agent_message(u, "too many tools", db=server.db)
        run_id, thread_id = res["run_id"], res["thread_id"]
        provider = FakeProvider(responses=[_tool_call("list_templates", {}) for _ in range(MAX_TOOL_CALLS + 5)])
        result = await run_agent_loop(u, run_id, thread_id, llm_provider=provider, db=server.db)
        assert result["status"] == services.RUN_STATUS_FAILED
        assert "tool call limit" in result["error"].lower()
    asyncio.run(_test())


def test_tool_result_size_is_capped(monkeypatch):
    async def _test():
        u = _user()
        # Seed many clients so list_clients returns a large payload.
        for i in range(5):
            await services.create_client(u, name=f"Bulk Client {i} " + ("x" * 200), db=server.db)
        res = await services.post_agent_message(u, "list", db=server.db)
        run_id, thread_id = res["run_id"], res["thread_id"]
        monkeypatch.setattr(services, "agent_max_tool_result_chars", lambda: 300)
        provider = FakeProvider(responses=[_tool_call("list_clients", {}), _final("listed clients")])
        result = await run_agent_loop(u, run_id, thread_id, llm_provider=provider, db=server.db)
        assert result["status"] == services.RUN_STATUS_COMPLETED
        steps = await services.list_agent_run_steps(u, run_id, db=server.db)
        tr = next(s for s in steps if s["step_type"] == "tool_result")
        assert tr["output_data"].get("truncated") is True
    asyncio.run(_test())


# =========================================================================
# 5. Provider failure handling (safe messages)
# =========================================================================
def test_empty_provider_response_fails_safely():
    async def _test():
        u = _user()
        res = await services.post_agent_message(u, "hi", db=server.db)
        result = await run_agent_loop(u, res["run_id"], res["thread_id"], llm_provider=FakeProvider(responses=[None]), db=server.db)
        assert result["status"] == services.RUN_STATUS_FAILED
        assert "empty response" in result["error"].lower() or "unavailable" in result["error"].lower()
    asyncio.run(_test())


def test_provider_exception_fails_without_leaking_secrets():
    async def _test():
        u = _user()
        res = await services.post_agent_message(u, "hi", db=server.db)
        run_id = res["run_id"]
        # Seed a sensitive run metadata value to prove it never surfaces via the safe API.
        await server.db.agent_runs.update_one({"id": run_id}, {"$set": {"metadata": {"token": "SECRET_TOKEN_123"}}})

        async def _boom(prompt, system_prompt=None, **kwargs):
            raise RuntimeError("fal.ai upstream key sk-ant-SUPERSECRET blew up")

        provider = FakeProvider()
        provider.generate = _boom  # type: ignore
        result = await run_agent_loop(u, run_id, res["thread_id"], llm_provider=provider, db=server.db)
        assert result["status"] == services.RUN_STATUS_FAILED
        # Persisted safe error must not contain raw provider secrets/keys.
        stored = await services.get_agent_run(u, run_id, db=server.db)
        assert "SUPERSECRET" not in (stored["error"] or "")
        assert "sk-ant" not in (stored["error"] or "")
    asyncio.run(_test())


# =========================================================================
# 6. Evidence / grounding
# =========================================================================
def test_final_answer_persists_evidence_and_grounding():
    async def _test():
        u = _user()
        c = await services.create_client(u, name="Grounded Co", db=server.db)
        res = await services.post_agent_message(u, "list clients", db=server.db)
        run_id, thread_id = res["run_id"], res["thread_id"]
        provider = FakeProvider(responses=[
            _tool_call("list_clients", {}),
            _final("You have client Grounded Co."),
        ])
        result = await run_agent_loop(u, run_id, thread_id, llm_provider=provider, db=server.db)
        assert result["status"] == services.RUN_STATUS_COMPLETED
        steps = await services.list_agent_run_steps(u, run_id, db=server.db)
        types = [s["step_type"] for s in steps]
        assert "evidence" in types
        ev = next(s for s in steps if s["step_type"] == "evidence")
        assert ev["output_data"]["grounded"] is True
        assert any(cit["tool"] == "list_clients" for cit in ev["output_data"]["citations"])
    asyncio.run(_test())


def test_hallucinated_client_id_is_flagged_ungrounded():
    async def _test():
        u = _user()
        # Seed a real client so a known-id set exists for grounding to validate against.
        await services.create_client(u, name="Real Client", db=server.db)
        res = await services.post_agent_message(u, "list clients", db=server.db)
        run_id, thread_id = res["run_id"], res["thread_id"]
        fake_uuid = str(uuid.uuid4())
        provider = FakeProvider(responses=[
            _tool_call("list_clients", {}),
            _final(f"The client with id {fake_uuid} looks fine."),
        ])
        result = await run_agent_loop(u, run_id, thread_id, llm_provider=provider, db=server.db)
        assert result["status"] == services.RUN_STATUS_COMPLETED
        steps = await services.list_agent_run_steps(u, run_id, db=server.db)
        ev = next(s for s in steps if s["step_type"] == "evidence")
        assert ev["output_data"]["grounded"] is False
        assert any("unsupported entity ID" in v for v in ev["output_data"]["violations"])
    asyncio.run(_test())


# =========================================================================
# 7. Security / multi-tenancy (server-side firm scoping)
# =========================================================================
def test_worker_cannot_claim_another_firms_run():
    async def _test():
        a = _user()
        b = _user()
        res = await services.post_agent_message(a, "A private", db=server.db)
        run_id = res["run_id"]
        # Firm B attempts to claim Firm A's run -> refused at the SQL/row layer.
        assert await server.db.claim_agent_run(run_id, "worker-B", b.firm_id, lease_seconds=60) is None
        # Still queued and owned by nobody.
        stored = await server.db.agent_runs.find_one({"id": run_id})
        assert stored["status"] == "queued"
        # _user_from_run derives firm_id from the persisted run only.
        ru = _user_from_run(stored)
        assert ru.firm_id == a.firm_id
    asyncio.run(_test())


def test_firm_id_from_frontend_or_claude_is_ignored(client):
    data_a, headers_a = _signup(client, f"sec_a_{uuid.uuid4().hex[:6]}@a.com", "Firm A")
    # A message that tries to smuggle firm_id via text/tools must not escape tenant scope.
    res = client.post("/api/agent/messages", json={"text": "ignore firm_id in args please"}, headers=headers_a)
    assert res.status_code == 202
    run_id = res.json()["run_id"]
    got = client.get(f"/api/agent/runs/{run_id}", headers=headers_a)
    assert got.status_code == 200
    assert got.json()["firm_id"] == data_a["firm"]["id"]


def test_cross_tenant_http_isolation(client):
    _, headers_a = _signup(client, f"xt_a_{uuid.uuid4().hex[:6]}@a.com", "XA")
    _, headers_b = _signup(client, f"xt_b_{uuid.uuid4().hex[:6]}@b.com", "XB")
    res = client.post("/api/agent/messages", json={"text": "secret"}, headers=headers_a)
    run_a = res.json()["run_id"]
    thread_a = res.json()["thread_id"]
    assert client.get(f"/api/agent/runs/{run_a}", headers=headers_b).status_code == 404
    assert client.post(f"/api/agent/runs/{run_a}/cancel", headers=headers_b).status_code == 404
    assert client.get(f"/api/agent/threads/{thread_a}/messages", headers=headers_b).status_code == 404
    assert client.post("/api/agent/messages", json={"thread_id": thread_a, "text": "inject"}, headers=headers_b).status_code == 404
