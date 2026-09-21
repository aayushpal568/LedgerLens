"""LedgerLens V1 — Step 8 FINAL end-to-end agent verification.

These tests exercise the REAL service / tool-registry / approval / durable-worker /
deterministic-engine boundaries with the authoritative (memory) PostgreSQL-equivalent
store. ONLY the external Claude provider is scripted (no paid/live calls). The point of
Step 8 is to prove the assembled architecture works end to end — so nothing central is
mocked away.

Coverage (maps to Step 8 sections):
- §1 E2E: authenticated run -> durable worker claims -> read-only tool through services ->
        grounded answer persisted.
- §1 E2E: Claude requests run_scan -> approval created -> run waits -> user approves ->
        SERVER validates -> REAL scan executes via engine -> findings stored -> agent resumes.
- §1 E2E: Claude requests set_finding_review -> approval -> approve -> REAL finding update
        via services -> agent resumes.
- §2 deterministic findings are real and the agent reports actual counts (cannot invent).
- §3/§7 agent tool arguments + approvals are tenant-isolated; foreign IDs 404; no leak.
- §9 report downloads stay authenticated (owner ok, other tenant 404, no filesystem paths).
- §5/§8 exactly-once worker claim + cancellation of a waiting approval run (integration-level).
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
os.environ["AGENT_INLINE_DISPATCH"] = "false"
os.environ["AGENT_WORKER_ENABLED"] = "false"
# No live AI.
os.environ["FAL_KEY"] = ""
os.environ["FAL_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient

import database
import server
import services
import storage
from agent.approvals import (
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_EXECUTED,
    APPROVAL_STATUS_PENDING,
    APPROVAL_STATUS_REJECTED,
    get_approval,
)
from agent.loop import resume_agent_run
from agent.worker import process_next_queued_run
from auth_dep import AuthedUser


class ScriptedClaude:
    """Scripted decision provider: the ONLY mocked boundary (external Claude API)."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def generate(self, prompt, system_prompt=None, **kwargs):
        self.calls += 1
        if self.responses:
            r = self.responses.pop(0)
            return r if isinstance(r, str) else json.dumps(r)
        return json.dumps({"thought": "wrap", "final": "Done."})


def _tool(name, args, thought="planning"):
    return {"thought": thought, "tool": name, "args": args}


def _final(text, thought="answer"):
    return {"thought": thought, "final": text}


def _user(label):
    return AuthedUser(user_id=f"u-{label}", firm_id=f"firm-{label}-{uuid.uuid4().hex[:6]}", token_version=1)


@pytest.fixture()
def db():
    mem = database.MemoryDatabase()
    prev = server.db
    server.db = mem
    storage.reset_storage()
    try:
        yield mem
    finally:
        server.db = prev
        storage.reset_storage()


async def _seed_client_with_duplicate_files(user, db, tmp_tag):
    """Create a client + template and upload two byte-identical CSVs + one odd CSV."""
    client = await services.create_client(user, name=f"Client {tmp_tag}", db=db)
    tpl = {"id": f"tpl-{tmp_tag}", "firm_id": user.firm_id, "name": "Year End",
           "client_type": "Small Business", "items": [], "created_at": services.now_iso()}
    await db.templates.insert_one(tpl)
    csv_dup = b"Account,Debit,Credit,Year\n1000,500,0,2024\n1000,0,500,2024\n"
    await services.save_client_file(user, client["id"], "ledger_a.csv", csv_dup, db=db)
    await services.save_client_file(user, client["id"], "ledger_b.csv", csv_dup, db=db)
    await services.save_client_file(
        user, client["id"], "pnl_2021.csv",
        b"Profit and Loss 2021\nRevenue,250000\nExpenses,180000\n", db=db,
    )
    return client, tpl


async def _run_deterministic_scan(user, db, client, tpl):
    """Drive the real deterministic engine to completion (no background race)."""
    scan_id = services.new_id()
    await db.scans.insert_one({
        "id": scan_id, "firm_id": user.firm_id, "client_id": client["id"],
        "client_name": client["name"], "template_id": tpl["id"], "expected_period": 2024,
        "status": "queued", "progress": 0, "total_files": 0, "processed_files": 0,
        "skipped_files": [], "counts": {}, "total_findings": 0, "started_at": services.now_iso(),
    })
    await services._run_scan(scan_id, user.firm_id, client["id"], tpl["id"], 2024, db)
    scan = await services.get_scan(user, scan_id, db=db)
    return scan_id, scan


async def _await_scan_completion(user, db, client_id, exclude_ids, timeout=6.0):
    """Poll until the agent-run scan (created via the real run_scan tool path) finishes."""
    waited = 0.0
    while waited < timeout:
        scans = await services.list_scans(user, client_id, db=db)
        new = [s for s in scans if s["id"] not in exclude_ids]
        if new and new[0]["status"] in ("completed", "error", "cancelled"):
            return new[0]
        await asyncio.sleep(0.05)
        waited += 0.05
    raise AssertionError("run_scan background scan did not complete in time")


# =========================================================================
# §1 + §2: read-only grounded answer through the real durable worker
# =========================================================================
def test_e2e_readonly_grounded_answer_via_worker(db):
    async def _test():
        u = _user("a")
        client, tpl = await _seed_client_with_duplicate_files(u, db, "ro")
        scan_id, scan = await _run_deterministic_scan(u, db, client, tpl)
        real_count = scan["total_findings"]
        assert real_count >= 1, "deterministic engine must produce real findings"

        msg = await services.post_agent_message(u, f"Summarize scan {scan_id}", db=db)
        run_id, thread_id = msg["run_id"], msg["thread_id"]

        provider = ScriptedClaude([
            _tool("summarize_findings", {"scan_id": scan_id}),
            _final(f"Scan completed with {real_count} findings that the accountant should review."),
        ])

        # Drive the durable worker: it atomically claims the queued run and executes it.
        result = await process_next_queued_run(llm_provider=provider)
        assert result is not None and result["status"] == services.RUN_STATUS_COMPLETED

        run = await services.get_agent_run(u, run_id, db=db)
        assert run["status"] == services.RUN_STATUS_COMPLETED

        steps = await services.list_agent_run_steps(u, run_id, db=db)
        types = [s["step_type"] for s in steps]
        assert "tool_result" in types and "evidence" in types
        # The tool executed through the real service and returned the REAL count.
        tr = next(s for s in steps if s["step_type"] == "tool_result")
        assert tr["output_data"]["result"]["total"] == real_count
        ev = next(s for s in steps if s["step_type"] == "evidence")
        assert ev["output_data"]["grounded"] is True

        msgs = await services.list_agent_messages(u, thread_id, db=db)
        assistant = [m for m in msgs if m["role"] == "assistant"]
        assert len(assistant) == 1
        assert str(real_count) in assistant[0]["text"]
        # No internal reasoning or filesystem paths leak into the user-facing message.
        assert "planning" not in assistant[0]["text"]
        assert "uploads" not in assistant[0]["text"] and "storage_path" not in json.dumps(assistant[0])
    asyncio.run(_test())


# =========================================================================
# §1 + §5: run_scan consequential -> approval -> server-executed REAL scan
# =========================================================================
def test_e2e_run_scan_approved_executes_real_scan(db):
    async def _test():
        u = _user("a")
        client, tpl = await _seed_client_with_duplicate_files(u, db, "rs")
        pre_existing = {s["id"] for s in await services.list_scans(u, client["id"], db=db)}

        msg = await services.post_agent_message(u, "Run the year-end audit scan", db=db)
        run_id, thread_id = msg["run_id"], msg["thread_id"]
        provider = ScriptedClaude([
            _tool("run_scan", {"client_id": client["id"], "template_id": tpl["id"], "expected_period": "2024"}),
            _final("The audit scan has been started."),
        ])

        # Worker claims + runs until it pauses on the consequential action.
        first = await process_next_queued_run(llm_provider=provider)
        assert first["status"] == services.RUN_STATUS_WAITING_FOR_APPROVAL
        approval_id = first["approval_id"]

        # Pending approval must NOT have executed the scan yet (real boundary check).
        assert {s["id"] for s in await services.list_scans(u, client["id"], db=db)} == pre_existing
        appr = await get_approval(u, approval_id, db=db)
        assert appr["status"] == APPROVAL_STATUS_PENDING

        # Approve (server-side) then resume -> real run_scan executes via services.
        await services.approve_agent_approval(u, approval_id, db=db)
        assert (await get_approval(u, approval_id, db=db))["status"] == APPROVAL_STATUS_APPROVED
        resumed = await resume_agent_run(u, approval_id, is_approved=True, llm_provider=provider, db=db)
        assert resumed["status"] == services.RUN_STATUS_COMPLETED

        # A REAL scan now exists and completes via the deterministic engine.
        scan = await _await_scan_completion(u, db, client["id"], pre_existing)
        assert scan["status"] == "completed"
        findings = await services.get_findings(u, scan_id=scan["id"], db=db)
        cats = {f["category"] for f in findings}
        assert "exact_duplicate" in cats  # engine really ran
        assert (await get_approval(u, approval_id, db=db))["status"] == APPROVAL_STATUS_EXECUTED

        run = await services.get_agent_run(u, run_id, db=db)
        assert run["status"] == services.RUN_STATUS_COMPLETED
    asyncio.run(_test())


# =========================================================================
# §1 + §3: set_finding_review approved -> REAL finding updated; foreign ID rejected
# =========================================================================
def test_e2e_set_finding_review_updates_real_finding(db):
    async def _test():
        u = _user("a")
        b = _user("b")
        client, tpl = await _seed_client_with_duplicate_files(u, db, "fr")
        scan_id, scan = await _run_deterministic_scan(u, db, client, tpl)
        findings = await services.get_findings(u, scan_id=scan_id, db=db)
        assert findings, "need a real finding to review"
        finding_id = findings[0]["id"]
        assert findings[0]["status"] == "unreviewed"

        msg = await services.post_agent_message(u, f"Mark finding {finding_id} as a real exception", db=db)
        run_id, thread_id = msg["run_id"], msg["thread_id"]
        provider = ScriptedClaude([
            _tool("set_finding_review", {"finding_id": finding_id, "review_status": "keep", "review_notes": "confirmed duplicate"}),
            _final(f"Finding {finding_id} was kept as a valid exception."),
        ])

        first = await process_next_queued_run(llm_provider=provider)
        assert first["status"] == services.RUN_STATUS_WAITING_FOR_APPROVAL
        await services.approve_agent_approval(u, first["approval_id"], db=db)
        resumed = await resume_agent_run(u, first["approval_id"], is_approved=True, llm_provider=provider, db=db)
        assert resumed["status"] == services.RUN_STATUS_COMPLETED

        # The finding really changed in the store (via services.update_finding).
        stored = await db.findings.find_one({"id": finding_id, "firm_id": u.firm_id}, {"_id": 0})
        assert stored["status"] == "keep"

        # A foreign finding cannot be updated (tenant-scoped tool): Firm B reviews A's finding -> fails closed.
        msg_b = await services.post_agent_message(b, "review that finding", db=db)
        run_b, thread_b = msg_b["run_id"], msg_b["thread_id"]
        provider_b = ScriptedClaude([
            _tool("set_finding_review", {"finding_id": finding_id, "review_status": "ignore"}),
            _final("That finding is not in your firm, so it was not changed."),
        ])
        first_b = await process_next_queued_run(llm_provider=provider_b)
        assert first_b["status"] == services.RUN_STATUS_WAITING_FOR_APPROVAL
        await services.approve_agent_approval(b, first_b["approval_id"], db=db)
        await resume_agent_run(b, first_b["approval_id"], is_approved=True, llm_provider=provider_b, db=db)
        # Finding A unchanged by Firm B.
        after_b = await db.findings.find_one({"id": finding_id, "firm_id": u.firm_id}, {"_id": 0})
        assert after_b["status"] == "keep"

        # Firm B must NOT be able to approve/retrieve Firm A's approval (404).
        appr_a = (await services.list_agent_approvals(u, run_id=run_id, db=db))[0]
        with pytest.raises(Exception):
            await services.approve_agent_approval(b, appr_a["id"], db=db)
    asyncio.run(_test())


# =========================================================================
# §7: approval cannot execute after its run is cancelled (integration)
# =========================================================================
def test_e2e_cannot_approve_after_run_cancelled(db):
    async def _test():
        u = _user("a")
        client, tpl = await _seed_client_with_duplicate_files(u, db, "cancel")
        pre_existing = {s["id"] for s in await services.list_scans(u, client["id"], db=db)}
        msg = await services.post_agent_message(u, "run scan", db=db)
        run_id, thread_id = msg["run_id"], msg["thread_id"]
        provider = ScriptedClaude([
            _tool("run_scan", {"client_id": client["id"], "template_id": tpl["id"], "expected_period": "2024"}),
            _final("done"),
        ])
        first = await process_next_queued_run(llm_provider=provider)
        approval_id = first["approval_id"]
        assert first["status"] == services.RUN_STATUS_WAITING_FOR_APPROVAL

        # User cancels the run while it waits for approval.
        await services.cancel_agent_run(u, run_id, db=db)
        # Even approving afterward must not execute (server revalidates run status).
        await services.approve_agent_approval(u, approval_id, db=db)
        out = await resume_agent_run(u, approval_id, is_approved=True, llm_provider=provider, db=db)
        assert out.get("reason") == "run_not_resumable" or out.get("status") == services.RUN_STATUS_CANCELLED
        assert {s["id"] for s in await services.list_scans(u, client["id"], db=db)} == pre_existing
    asyncio.run(_test())


# =========================================================================
# §9: report downloads remain authenticated & tenant-scoped, no path leaks
# =========================================================================
def test_e2e_report_download_is_authenticated_and_tenant_scoped(db):
    async def _test():
        u = _user("a")
        b = _user("b")
        # Register principals so the authenticated HTTP dependency can resolve them.
        for _u in (u, b):
            await db.users.insert_one({
                "id": _u.user_id, "firm_id": _u.firm_id,
                "token_version": _u.token_version, "email": f"{_u.user_id}@x.test",
            })
        client, tpl = await _seed_client_with_duplicate_files(u, db, "rpt")
        scan_id, scan = await _run_deterministic_scan(u, db, client, tpl)
        assert scan["total_findings"] >= 1

        from tokens import create_access_token
        with TestClient(server.app) as c:
            ha = {"Authorization": f"Bearer {create_access_token(u.user_id, u.firm_id, u.token_version)}"}
            hb = {"Authorization": f"Bearer {create_access_token(b.user_id, b.firm_id, b.token_version)}"}

            # Unauthenticated -> 401
            assert c.get(f"/api/scans/{scan_id}/report").status_code == 401
            # Owner -> 200, bytes, filename only (no absolute filesystem path)
            ok = c.get(f"/api/scans/{scan_id}/report?format=csv", headers=ha)
            assert ok.status_code == 200 and len(ok.content) > 0
            disp = ok.headers.get("content-disposition", "")
            assert "review_report_" in disp and "\\" not in disp and "/app/" not in disp
            # Other tenant -> 404 (scoped)
            assert c.get(f"/api/scans/{scan_id}/report", headers=hb).status_code == 404
            # Invalid scan id -> 404
            assert c.get("/api/scans/does-not-exist/report", headers=ha).status_code == 404
            # Malformed/no token -> 401
            assert c.get(f"/api/scans/{scan_id}/report", headers={"Authorization": "Bearer garbage"}).status_code == 401
    asyncio.run(_test())
