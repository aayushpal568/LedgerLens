"""Regression tests for Phase C1:
- Near-duplicate detection on repetitive accounting ledgers with autojunk=False
- Claude agent decision vs final answer token separation
- Agent recovery from malformed/ambiguous JSON via single retry
- Canonical findings fields (filenames, file_ids, severity, confidence, evidence)
- Optional scan_id support in findings tool and service
- export_report agent tool
- Grounding verification of entity UUIDs and finding counts
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATA_BACKEND", "memory")
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-32-chars-long-ledgerlens-suite")

from auth_dep import AuthedUser
from database import MemoryDatabase
from engine.similarity import similarity
from agent.grounding import verify_grounding
from agent.loop import (
    run_agent_loop,
    set_global_llm_provider,
    CLAUDE_DECISION_MAX_TOKENS,
    CLAUDE_FINAL_ANSWER_MAX_TOKENS,
)
from agent.registry import default_registry, execute_tool
import agent.tools as agent_tools
import services


# ---------------------------------------------------------------------------
# 1. Near-duplicate autojunk regression test
# ---------------------------------------------------------------------------
def test_near_duplicate_repetitive_ledger():
    """Verify that a long repetitive accounting ledger with one amended row is detected as near-duplicate."""
    base_rows = [
        f"2024-01-{i%28 + 1:02d},ACCT-1000,CASH,150.00,USD,Daily recurring petty cash balance verification"
        for i in range(120)
    ]
    ledger_a = "\n".join(base_rows)
    
    # Ledger B modifies only row 50
    amended_rows = list(base_rows)
    amended_rows[50] = "2024-01-22,ACCT-1000,CASH,9999.00,USD,Amended petty cash balance verification"
    ledger_b = "\n".join(amended_rows)

    sim = similarity(ledger_a, ledger_b)
    assert sim >= 0.82, f"Expected similarity >= 0.82, got {sim}"


# ---------------------------------------------------------------------------
# 2. Token separation constants check
# ---------------------------------------------------------------------------
def test_token_budget_separation():
    assert CLAUDE_DECISION_MAX_TOKENS == 384
    assert CLAUDE_FINAL_ANSWER_MAX_TOKENS == 2048
    assert CLAUDE_FINAL_ANSWER_MAX_TOKENS > CLAUDE_DECISION_MAX_TOKENS


# ---------------------------------------------------------------------------
# 3. Agent recovery from malformed JSON via single retry
# ---------------------------------------------------------------------------
class MockRetryLLMProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0
        self.prompts = []

    def generate(self, prompt, **kwargs):
        self.call_count += 1
        self.prompts.append(prompt)
        if self.responses:
            return self.responses.pop(0)
        return json.dumps({"thought": "done", "final": "Fallback complete."})


def test_agent_recovers_from_malformed_json_retry():
    async def _test():
        db = MemoryDatabase()
        user = AuthedUser(user_id="user-c1", firm_id="firm-c1", token_version=1)
        
        # Turn 1: malformed non-JSON
        # Retry: valid final answer
        provider = MockRetryLLMProvider([
            "I will check the account now: {bad_json: missing_quotes",
            json.dumps({"thought": "Recovered cleanly", "final": "Here is your detailed account summary."}),
        ])
        set_global_llm_provider(provider)

        msg_res = await services.post_agent_message(user, "Summarize accounts", db=db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        result = await run_agent_loop(user, run_id, thread_id, db=db)
        assert result["status"] == services.RUN_STATUS_COMPLETED
        assert provider.call_count == 2
        assert "CORRECTION REQUIRED" in provider.prompts[1]

        # Verify assistant message was saved
        messages = await services.list_agent_messages(user, thread_id, db=db)
        assert len(messages) == 2
        assert messages[1]["text"] == "Here is your detailed account summary."
        # Verify thought not leaked
        assert "Recovered cleanly" not in messages[1]["text"]
        set_global_llm_provider(None)

    asyncio.run(_test())


def test_agent_fails_cleanly_on_double_malformed_json():
    async def _test():
        db = MemoryDatabase()
        user = AuthedUser(user_id="user-c1", firm_id="firm-c1", token_version=1)
        
        # Turn 1: malformed
        # Retry: also malformed
        provider = MockRetryLLMProvider([
            "Malformed output 1",
            "Still malformed output 2",
        ])
        set_global_llm_provider(provider)

        msg_res = await services.post_agent_message(user, "Check ledger", db=db)
        run_id = msg_res["run_id"]
        thread_id = msg_res["thread_id"]

        result = await run_agent_loop(user, run_id, thread_id, db=db)
        assert result["status"] == services.RUN_STATUS_FAILED
        assert "malformed response" in result["error"]

        run_doc = await services.get_agent_run(user, run_id, db=db)
        assert run_doc["status"] == "failed"
        assert "malformed response" in run_doc["error"]
        set_global_llm_provider(None)

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 4. Canonical findings fields & optional scan_id
# ---------------------------------------------------------------------------
def test_findings_canonical_schema_and_optional_scan_id():
    async def _test():
        db = MemoryDatabase()
        user = AuthedUser(user_id="user-c1", firm_id="firm-c1", token_version=1)

        scan_1 = "scan-111"
        scan_2 = "scan-222"
        await db.scans.insert_one({"id": scan_1, "firm_id": user.firm_id, "status": "completed"})
        await db.scans.insert_one({"id": scan_2, "firm_id": user.firm_id, "status": "completed"})

        # Insert findings matching scan_engine structure
        await db.findings.insert_one({
            "id": "fnd-1",
            "firm_id": user.firm_id,
            "scan_id": scan_1,
            "category": "possible_duplicate",
            "title": "Invoice A and Invoice B are similar",
            "confidence": 88,
            "confidence_level": "high",
            "files": [
                {"file_id": "file-101", "name": "Invoice_Jan.pdf", "ext": "PDF", "size": 1024},
                {"file_id": "file-102", "name": "Invoice_Jan_v2.pdf", "ext": "PDF", "size": 1040},
            ],
            "evidence": {"summary": "Text similarity is 88%"},
            "status": "unreviewed",
            "note": "Awaiting CPA check",
        })

        await db.findings.insert_one({
            "id": "fnd-2",
            "firm_id": user.firm_id,
            "scan_id": scan_2,
            "category": "missing_doc",
            "title": "Missing Form 1099",
            "confidence": 80,
            "confidence_level": "medium",
            "files": [],
            "evidence": {"summary": "Checklist item Form 1099 missing"},
            "status": "unreviewed",
        })

        # Test tool call with scan_id
        res_scan1 = await execute_tool(user, "get_findings", {"scan_id": scan_1}, db=db)
        assert res_scan1["success"] is True
        f_list = res_scan1["result"]
        assert len(f_list) == 1
        assert f_list[0]["id"] == "fnd-1"
        assert f_list[0]["filenames"] == ["Invoice_Jan.pdf", "Invoice_Jan_v2.pdf"]
        assert f_list[0]["file_ids"] == ["file-101", "file-102"]
        assert f_list[0]["confidence"] == 88
        assert f_list[0]["severity"] == "medium"
        assert f_list[0]["evidence"]["summary"] == "Text similarity is 88%"
        assert f_list[0]["note"] == "Awaiting CPA check"

        # Test tool call without scan_id (optional firm-wide query)
        res_all = await execute_tool(user, "get_findings", {}, db=db)
        assert res_all["success"] is True
        assert len(res_all["result"]) == 2

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 5. export_report Agent Tool
# ---------------------------------------------------------------------------
def test_export_report_agent_tool():
    async def _test():
        db = MemoryDatabase()
        user = AuthedUser(user_id="user-c1", firm_id="firm-c1", token_version=1)

        scan_id = "scan-report-test"
        await db.scans.insert_one({
            "id": scan_id,
            "firm_id": user.firm_id,
            "client_name": "Acme Corp",
            "status": "completed",
        })
        await db.findings.insert_one({
            "id": "f-1",
            "firm_id": user.firm_id,
            "scan_id": scan_id,
            "category": "wrong_period",
            "title": "Wrong period",
            "confidence": 90,
        })

        # Call export_report tool
        res = await execute_tool(user, "export_report", {"scan_id": scan_id, "format": "csv"}, db=db)
        assert res["success"] is True
        payload = res["result"]
        assert payload["scan_id"] == scan_id
        assert payload["format"] == "csv"
        assert payload["status"] == "ready"
        assert payload["download_url"] == f"/api/scans/{scan_id}/report?format=csv"
        assert "filename" in payload


    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 6. Factual Grounding Tests (Positive & Negative)
# ---------------------------------------------------------------------------
def test_grounding_positive_and_negative():
    scan_id = "11111111-2222-3333-4444-555555555555"
    file_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    
    steps = [
        {
            "step_type": "tool_result",
            "tool": "get_findings",
            "result": {
                "success": True,
                "result": [
                    {
                        "id": "fnd-1",
                        "scan_id": scan_id,
                        "file_ids": [file_id],
                        "filenames": ["receipt.pdf"],
                    },
                    {
                        "id": "fnd-2",
                        "scan_id": scan_id,
                        "file_ids": [],
                        "filenames": [],
                    }
                ]
            }
        }
    ]

    # Positive: mentions known IDs and valid count (2 findings)
    valid_text = f"Audit scan {scan_id} identified a total of 2 findings, including receipt {file_id}."
    res_pos = verify_grounding(valid_text, steps)
    assert res_pos.is_grounded is True
    assert len(res_pos.violations) == 0

    # Negative: fabricated entity UUID
    fake_uuid = "99999999-9999-9999-9999-999999999999"
    invalid_uuid_text = f"Reviewing scan {fake_uuid} complete."
    res_neg_uuid = verify_grounding(invalid_uuid_text, steps)
    assert res_neg_uuid.is_grounded is False
    assert any("unsupported entity ID" in v for v in res_neg_uuid.violations)

    # Negative: fabricated finding count
    invalid_count_text = f"The audit identified 99 findings for client."
    res_neg_count = verify_grounding(invalid_count_text, steps)
    assert res_neg_count.is_grounded is False
    assert any("finding count" in v for v in res_neg_count.violations)
