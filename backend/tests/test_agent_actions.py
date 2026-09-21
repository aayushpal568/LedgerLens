"""V1 Agent Step 6 — Consequential Actions + Server-Enforced Approval.

Hermetic (in-memory DB, fake providers; zero live/paid Claude calls). Verifies:

1. run_scan & set_finding_review are registered consequential (approval-required) tools.
2. Argument validation + firm_id-injection rejection + tenant isolation for both tools.
3. A consequential tool call PAUSES the loop as waiting_for_approval and does NOT execute.
4. PENDING approvals never execute; only APPROVED approvals execute, and EXACTLY ONCE.
5. Rejected / cancelled-run / failed-run approvals never execute.
6. Double approval / concurrent claim / stale (recovered) resume cannot execute twice.
7. Cross-tenant approval access is blocked (404). Authorization is revalidated at execution.
8. Approval persistence + restart/lease recovery does not auto-execute or duplicate.
"""
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATA_BACKEND", "memory")
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-32-chars-long-ledgerlens-suite")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
# Drive execution explicitly; do not let post_agent_message auto-run with the real provider.
os.environ["AGENT_INLINE_DISPATCH"] = "false"
os.environ["AGENT_WORKER_ENABLED"] = "false"

import pytest
from fastapi import HTTPException

import database
import server
import services
from agent.approvals import (
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_EXECUTED,
    APPROVAL_STATUS_EXECUTING,
    APPROVAL_STATUS_FAILED,
    APPROVAL_STATUS_PENDING,
    APPROVAL_STATUS_REJECTED,
    get_approval,
)
from agent.loop import run_agent_loop, resume_agent_run
from agent.registry import default_registry, execute_tool
from agent.tools import VALID_REVIEW_STATUSES
from auth_dep import AuthedUser


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def generate(self, prompt, system_prompt=None, **kwargs):
        self.calls += 1
        if self.responses:
            r = self.responses.pop(0)
            return r if isinstance(r, str) else json.dumps(r)
        return json.dumps({"thought": "wrap", "final": "Done."})


def _user(label):
    return AuthedUser(user_id=f"user-{label}-1", firm_id=f"firm-{label}", token_version=1)


def _tool(name, args, thought="t"):
    return {"thought": thought, "tool": name, "args": args}


def _final(text):
    return {"thought": "answer", "final": text}


@pytest.fixture()
def db():
    """Fresh, isolated in-memory database for the duration of one test."""
    mem = database.MemoryDatabase()
    prev = server.db
    server.db = mem
    try:
        yield mem
    finally:
        server.db = prev


@pytest.fixture()
def seeded(db):
    """Two firms; Firm A owns a client, a template, one finding, and an uploaded file."""
    a = _user("alpha")
    b = _user("beta")
    client = asyncio.run(services.create_client(a, name="Acme Mfg", db=db))
    tpl = {"id": "tpl-a", "firm_id": a.firm_id, "name": "Year-End", "client_type": "Small Business",
           "items": [], "created_at": services.now_iso()}
    asyncio.run(db.templates.insert_one(tpl))
    scan = {"id": "scan-a", "firm_id": a.firm_id, "client_id": client["id"], "client_name": "Acme Mfg",
            "status": "completed", "started_at": services.now_iso()}
    asyncio.run(db.scans.insert_one(scan))
    finding = {"id": "find-a", "firm_id": a.firm_id, "scan_id": "scan-a", "client_id": client["id"],
               "category": "exact_duplicate", "status": "unreviewed", "title": "Dup invoice",
               "created_at": services.now_iso()}
    asyncio.run(db.findings.insert_one(finding))
    # Firm B owns an isolated client + finding.
    b_client = asyncio.run(services.create_client(b, name="Beta Co", db=db))
    asyncio.run(db.scans.insert_one({"id": "scan-b", "firm_id": b.firm_id, "client_id": b_client["id"], "status": "completed", "started_at": services.now_iso()}))
    asyncio.run(db.findings.insert_one({"id": "find-b", "firm_id": b.firm_id, "scan_id": "scan-b", "client_id": b_client["id"], "status": "unreviewed", "created_at": services.now_iso()}))
    return {"db": db, "a": a, "b": b, "client_id": client["id"], "tpl": "tpl-a", "finding_id": "find-a", "b_finding_id": "find-b", "b_client_id": b_client["id"]}


def _new_run(user, db, text="please act"):
    res = asyncio.run(services.post_agent_message(user, text, db=db))
    return res["run_id"], res["thread_id"]


def _scan_count(user, client_id, db):
    return len(asyncio.run(services.list_scans(user, client_id, db=db)))


# =========================================================================
# 1. Registration
# =========================================================================
def test_consequential_tools_are_registered_and_approval_required(db):
    for name in ("run_scan", "set_finding_review"):
        tool = default_registry.get(name)
        assert tool is not None, f"{name} must be registered"
        assert tool.approval_required is True and tool.read_only is False


# =========================================================================
# 2. Argument validation + firm_id injection + tenant isolation
# =========================================================================
def test_run_scan_argument_rules(seeded):
    a, db, client_id, tpl = seeded["a"], seeded["db"], seeded["client_id"], seeded["tpl"]

    # Missing required args -> invalid
    r = asyncio.run(execute_tool(a, "run_scan", {"client_id": client_id}, db=db, is_approved=True))
    assert r["success"] is False and r["error_type"] == "InvalidArgument"

    # firm_id injection is rejected (never trusted from Claude/frontend)
    r = asyncio.run(execute_tool(a, "run_scan", {"client_id": client_id, "template_id": tpl, "firm_id": "firm-beta"}, db=db, is_approved=True))
    assert r["success"] is False and "firm_id" in r["error"]

    # Without approval -> not executed
    r = asyncio.run(execute_tool(a, "run_scan", {"client_id": client_id, "template_id": tpl}, db=db, is_approved=False))
    assert r["success"] is False and r["error_type"] == "ApprovalRequired"

    # Valid + approved -> creates a scan, safe reference, no paths
    r = asyncio.run(execute_tool(a, "run_scan", {"client_id": client_id, "template_id": tpl}, db=db, is_approved=True))
    assert r["success"] is True
    payload = r["result"]
    assert payload["scan_id"] and payload["status"] in ("queued", "completed", "reused_active") or payload.get("reused_active")
    assert "storage_path" not in json.dumps(payload)


def test_run_scan_cross_tenant_isolation(seeded):
    b, db = seeded["b"], seeded["db"]
    # Firm B cannot scan Firm A's client.
    r = asyncio.run(execute_tool(b, "run_scan", {"client_id": seeded["client_id"], "template_id": seeded["tpl"]}, db=db, is_approved=True))
    assert r["success"] is False
    assert r.get("error_type") in ("NotFoundError", "ServiceError")


def test_set_finding_review_rules(seeded):
    a, db, finding_id = seeded["a"], seeded["db"], seeded["finding_id"]

    # Invalid review state (not an existing app value) is rejected
    r = asyncio.run(execute_tool(a, "set_finding_review", {"finding_id": finding_id, "review_status": "accepted"}, db=db, is_approved=True))
    assert r["success"] is False and r["error_type"] == "InvalidArgument"

    # Missing finding_id
    r = asyncio.run(execute_tool(a, "set_finding_review", {"review_status": "keep"}, db=db, is_approved=True))
    assert r["success"] is False and r["error_type"] == "InvalidArgument"

    # Valid + approved updates using the EXISTING status vocabulary
    assert "keep" in VALID_REVIEW_STATUSES
    r = asyncio.run(execute_tool(a, "set_finding_review", {"finding_id": finding_id, "review_status": "keep", "review_notes": "verified"}, db=db, is_approved=True))
    assert r["success"] is True and r["result"]["review_status"] == "keep"
    stored = asyncio.run(db.findings.find_one({"id": finding_id, "firm_id": a.firm_id}, {"_id": 0}))
    assert stored["status"] == "keep"

    # Cross-tenant: Firm B cannot review Firm A's finding
    rb = asyncio.run(execute_tool(seeded["b"], "set_finding_review", {"finding_id": finding_id, "review_status": "keep"}, db=db, is_approved=True))
    assert rb["success"] is False and rb.get("error_type") in ("NotFoundError", "ServiceError")


def test_firm_id_injection_rejected_on_finding_tool(seeded):
    a, db = seeded["a"], seeded["db"]
    r = asyncio.run(execute_tool(a, "set_finding_review", {"finding_id": seeded["finding_id"], "review_status": "ignore", "firm_id": "firm-beta"}, db=db, is_approved=True))
    assert r["success"] is False and "firm_id" in r["error"]


# =========================================================================
# 3. Loop pauses on consequential tool
# =========================================================================
def test_loop_pauses_on_consequential_tool_and_does_not_execute(seeded):
    a, db, client_id, tpl = seeded["a"], seeded["db"], seeded["client_id"], seeded["tpl"]
    run_id, thread_id = _new_run(a, db, "run a scan on Acme")
    before = _scan_count(a, client_id, db)
    provider = FakeProvider([_tool("run_scan", {"client_id": client_id, "template_id": tpl}), _final("scan started")])

    result = asyncio.run(run_agent_loop(a, run_id, thread_id, llm_provider=provider, db=db))
    assert result["status"] == services.RUN_STATUS_WAITING_FOR_APPROVAL
    approval_id = result["approval_id"]

    # Pending approval must not have executed the scan yet.
    assert _scan_count(a, client_id, db) == before
    appr = asyncio.run(get_approval(a, approval_id, db=db))
    assert appr["status"] == APPROVAL_STATUS_PENDING
    assert appr["tool_name"] == "run_scan"
    assert appr["proposed_args"]["client_id"] == client_id
    # Persistence: approval is bound to run_id + firm_id + user + args (hash) + timestamps.
    assert appr["run_id"] == run_id and appr["firm_id"] == a.firm_id and appr["user_id"] == a.user_id
    assert appr["args_hash"] and appr["created_at"] and appr["expires_at"]
    run = asyncio.run(services.get_agent_run(a, run_id, db=db))
    assert run["status"] == services.RUN_STATUS_WAITING_FOR_APPROVAL


# =========================================================================
# 4. PENDING approval does not execute (authorization rechecked at execution)
# =========================================================================
def test_pending_approval_does_not_execute(seeded):
    a, db, client_id, tpl = seeded["a"], seeded["db"], seeded["client_id"], seeded["tpl"]
    run_id, thread_id = _new_run(a, db, "scan")
    provider = FakeProvider([_tool("run_scan", {"client_id": client_id, "template_id": tpl}), _final("x")])
    result = asyncio.run(run_agent_loop(a, run_id, thread_id, llm_provider=provider, db=db))
    approval_id = result["approval_id"]
    before = _scan_count(a, client_id, db)

    # Resume as if approved WITHOUT actually approving -> server must refuse (status still pending).
    out = asyncio.run(resume_agent_run(a, approval_id, is_approved=True, llm_provider=provider, db=db))
    assert out.get("reason") == "approval_not_executable"
    assert _scan_count(a, client_id, db) == before
    appr = asyncio.run(get_approval(a, approval_id, db=db))
    assert appr["status"] == APPROVAL_STATUS_PENDING


# =========================================================================
# 5. Approved executes exactly once + loop resumes to completion
# =========================================================================
def test_approved_executes_exactly_once_and_resumes(seeded):
    a, db, client_id, tpl = seeded["a"], seeded["db"], seeded["client_id"], seeded["tpl"]
    run_id, thread_id = _new_run(a, db, "scan")
    provider = FakeProvider([_tool("run_scan", {"client_id": client_id, "template_id": tpl}), _final("I started the audit scan.")])
    result = asyncio.run(run_agent_loop(a, run_id, thread_id, llm_provider=provider, db=db))
    approval_id = result["approval_id"]
    before = _scan_count(a, client_id, db)

    asyncio.run(services.approve_agent_approval(a, approval_id, db=db))
    asyncio.run(resume_agent_run(a, approval_id, is_approved=True, llm_provider=provider, db=db))

    # Exactly one new scan created and approval executed.
    assert _scan_count(a, client_id, db) == before + 1
    appr = asyncio.run(get_approval(a, approval_id, db=db))
    assert appr["status"] == APPROVAL_STATUS_EXECUTED
    run = asyncio.run(services.get_agent_run(a, run_id, db=db))
    assert run["status"] == services.RUN_STATUS_COMPLETED
    msgs = asyncio.run(services.list_agent_messages(a, thread_id, db=db))
    assert any(m["role"] == "assistant" for m in msgs)

    # A second resume on the executed approval must NOT create another scan.
    asyncio.run(resume_agent_run(a, approval_id, is_approved=True, llm_provider=provider, db=db))
    assert _scan_count(a, client_id, db) == before + 1


def test_double_approve_concurrent_claim_single_winner(seeded):
    a, db = seeded["a"], seeded["db"]
    run_id, thread_id = _new_run(a, db, "scan")
    provider = FakeProvider([_tool("run_scan", {"client_id": seeded["client_id"], "template_id": seeded["tpl"]}), _final("x")])
    result = asyncio.run(run_agent_loop(a, run_id, thread_id, llm_provider=provider, db=db))
    approval_id = result["approval_id"]
    asyncio.run(services.approve_agent_approval(a, approval_id, db=db))

    async def _race():
        return await asyncio.gather(
            services.claim_agent_approval_for_execution(a, approval_id, db=db),
            services.claim_agent_approval_for_execution(a, approval_id, db=db),
        )
    won = asyncio.run(_race())
    assert sorted(won) == [False, True], "exactly one executor may claim the approved action"


# =========================================================================
# 6. Rejected / cancelled / failed never execute
# =========================================================================
def test_rejected_approval_never_executes(seeded):
    a, db, client_id, tpl = seeded["a"], seeded["db"], seeded["client_id"], seeded["tpl"]
    run_id, thread_id = _new_run(a, db, "scan")
    provider = FakeProvider([_tool("run_scan", {"client_id": client_id, "template_id": tpl}), _final("rejected, no scan run")])
    result = asyncio.run(run_agent_loop(a, run_id, thread_id, llm_provider=provider, db=db))
    approval_id = result["approval_id"]
    before = _scan_count(a, client_id, db)

    asyncio.run(services.reject_agent_approval(a, approval_id, reason="not now", db=db))
    asyncio.run(resume_agent_run(a, approval_id, is_approved=False, reason="not now", llm_provider=provider, db=db))
    assert _scan_count(a, client_id, db) == before
    appr = asyncio.run(get_approval(a, approval_id, db=db))
    assert appr["status"] == APPROVAL_STATUS_REJECTED

    # Attempting to execute a rejected approval via the approve path must fail too.
    out = asyncio.run(resume_agent_run(a, approval_id, is_approved=True, llm_provider=provider, db=db))
    assert out.get("reason") in ("approval_not_executable", None)
    assert _scan_count(a, client_id, db) == before


def test_cancelled_run_cannot_execute_approval(seeded):
    a, db, client_id, tpl = seeded["a"], seeded["db"], seeded["client_id"], seeded["tpl"]
    run_id, thread_id = _new_run(a, db, "scan")
    provider = FakeProvider([_tool("run_scan", {"client_id": client_id, "template_id": tpl}), _final("x")])
    result = asyncio.run(run_agent_loop(a, run_id, thread_id, llm_provider=provider, db=db))
    approval_id = result["approval_id"]
    asyncio.run(services.approve_agent_approval(a, approval_id, db=db))
    before = _scan_count(a, client_id, db)

    # Run gets cancelled while waiting.
    asyncio.run(services.cancel_agent_run(a, run_id, db=db))
    out = asyncio.run(resume_agent_run(a, approval_id, is_approved=True, llm_provider=provider, db=db))
    assert out.get("reason") == "run_not_resumable" or out.get("status") == services.RUN_STATUS_CANCELLED
    assert _scan_count(a, client_id, db) == before
    appr = asyncio.run(get_approval(a, approval_id, db=db))
    assert appr["status"] == APPROVAL_STATUS_APPROVED  # never executed


def test_failed_run_cannot_execute_approval(seeded):
    a, db, client_id, tpl = seeded["a"], seeded["db"], seeded["client_id"], seeded["tpl"]
    run_id, thread_id = _new_run(a, db, "scan")
    provider = FakeProvider([_tool("run_scan", {"client_id": client_id, "template_id": tpl}), _final("x")])
    result = asyncio.run(run_agent_loop(a, run_id, thread_id, llm_provider=provider, db=db))
    approval_id = result["approval_id"]
    asyncio.run(services.approve_agent_approval(a, approval_id, db=db))
    asyncio.run(db.agent_runs.update_one({"id": run_id}, {"$set": {"status": services.RUN_STATUS_FAILED}}))
    before = _scan_count(a, client_id, db)

    out = asyncio.run(resume_agent_run(a, approval_id, is_approved=True, llm_provider=provider, db=db))
    assert out.get("reason") == "run_not_resumable"
    assert _scan_count(a, client_id, db) == before


# =========================================================================
# 7. Cross-tenant approval access + persistence/restart recovery
# =========================================================================
def test_cross_tenant_approval_access_blocked(seeded):
    a, b, db = seeded["a"], seeded["b"], seeded["db"]
    run_id, thread_id = _new_run(a, db, "scan")
    provider = FakeProvider([_tool("run_scan", {"client_id": seeded["client_id"], "template_id": seeded["tpl"]}), _final("x")])
    result = asyncio.run(run_agent_loop(a, run_id, thread_id, llm_provider=provider, db=db))
    approval_id = result["approval_id"]

    with pytest.raises(HTTPException):
        asyncio.run(get_approval(b, approval_id, db=db))
    with pytest.raises(HTTPException):
        asyncio.run(services.approve_agent_approval(b, approval_id, db=db))
    with pytest.raises(HTTPException):
        asyncio.run(resume_agent_run(b, approval_id, is_approved=True, llm_provider=provider, db=db))


def test_stale_resume_after_recovery_does_not_duplicate(seeded):
    """Simulates a recovered/stale worker attempting to re-run an already-executed approval."""
    a, db, client_id, tpl = seeded["a"], seeded["db"], seeded["client_id"], seeded["tpl"]
    run_id, thread_id = _new_run(a, db, "scan")
    provider = FakeProvider([_tool("run_scan", {"client_id": client_id, "template_id": tpl}), _final("x")])
    result = asyncio.run(run_agent_loop(a, run_id, thread_id, llm_provider=provider, db=db))
    approval_id = result["approval_id"]
    before = _scan_count(a, client_id, db)
    asyncio.run(services.approve_agent_approval(a, approval_id, db=db))
    asyncio.run(resume_agent_run(a, approval_id, is_approved=True, llm_provider=provider, db=db))
    assert _scan_count(a, client_id, db) == before + 1

    # A "stale" claim by a recovered worker must now fail (approval is executed).
    assert asyncio.run(services.claim_agent_approval_for_execution(a, approval_id, db=db)) is False
    # Recovery must not touch the terminal/ waiting runs in a way that re-executes:
    rec = asyncio.run(services.recover_stale_agent_runs(db=db))
    assert isinstance(rec, dict)
    assert _scan_count(a, client_id, db) == before + 1


def test_waiting_run_survives_recovery_without_executing(seeded):
    a, db, client_id, tpl = seeded["a"], seeded["db"], seeded["client_id"], seeded["tpl"]
    run_id, thread_id = _new_run(a, db, "scan")
    provider = FakeProvider([_tool("run_scan", {"client_id": client_id, "template_id": tpl}), _final("x")])
    result = asyncio.run(run_agent_loop(a, run_id, thread_id, llm_provider=provider, db=db))
    approval_id = result["approval_id"]
    before = _scan_count(a, client_id, db)

    # Force the lease to look expired, then recover: a waiting_for_approval run must stay
    # waiting (never auto-executed, never requeued into a scan).
    asyncio.run(db.agent_runs.update_one({"id": run_id}, {"$set": {"lease_expires_at": "2000-01-01T00:00:00+00:00"}}))
    asyncio.run(services.recover_stale_agent_runs(db=db))
    assert asyncio.run(services.get_agent_run(a, run_id, db=db))["status"] == services.RUN_STATUS_WAITING_FOR_APPROVAL
    assert asyncio.run(get_approval(a, approval_id, db=db))["status"] == APPROVAL_STATUS_PENDING
    assert _scan_count(a, client_id, db) == before
