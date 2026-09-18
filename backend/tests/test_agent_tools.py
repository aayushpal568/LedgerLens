"""Test suite for Agent Tool Registry and Read-Only Tools (Step 4).

Verifies:
1. Registry Integrity:
   - Contains exactly the 7 intended read-only tools.
   - All tools are flagged read_only=True.
   - Unknown tools are rejected and fail closed.
2. Security & Tenant Scoping:
   - Unauthenticated execution cannot occur (fails closed).
   - Tool arguments attempting to supply or override firm_id are blocked immediately.
   - Cross-tenant client, file, scan, finding, and run accesses return 404.
   - list_clients only returns the authenticated user's firm data.
3. Argument Validation:
   - Missing required arguments rejected.
   - Null / empty string arguments rejected.
   - Wrong argument types rejected.
   - Unexpected extra arguments rejected.
   - Non-dict argument payloads rejected.
4. Tool Execution & Output Sanitization:
   - list_clients, list_files, list_templates, get_scan_status, get_findings,
     summarize_findings, and get_agent_run_status return expected JSON structures.
   - summarize_findings calculates exact counts from actual database records (no hallucinations).
   - No raw document OCR, filesystem paths (storage_path), or sensitive fields are exposed.
   - Tools execute strictly through backend/services.py.
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# Ensure test environment
os.environ.setdefault("DATA_BACKEND", "memory")
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-32-chars-long-ledgerlens-suite")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")

import server
import services
from agent import (
    Tool,
    ToolRegistry,
    default_registry,
    execute_tool,
    validate_tool_arguments,
)
from auth_dep import AuthedUser


EXPECTED_TOOLS = {
    "list_clients",
    "list_files",
    "list_templates",
    "get_scan_status",
    "get_findings",
    "summarize_findings",
    "get_agent_run_status",
}


# ===========================================================================
# 1. Registry Integrity
# ===========================================================================
def test_registry_contains_exact_read_only_tools():
    registered_names = set(default_registry.tool_names())
    assert registered_names == EXPECTED_TOOLS, f"Registry mismatch: {registered_names} vs {EXPECTED_TOOLS}"

    for name in EXPECTED_TOOLS:
        tool = default_registry.get(name)
        assert tool is not None
        assert tool.name == name
        assert tool.read_only is True, f"Tool '{name}' must be read-only"
        assert len(tool.description.strip()) > 10
        assert tool.parameters.get("type") == "object"
        assert callable(tool.handler)


def test_unknown_tool_rejected():
    user = AuthedUser(user_id="u-test", firm_id="firm-test", token_version=1)
    res = asyncio.run(execute_tool(user, "nonexistent_danger_tool", {}))
    assert res["success"] is False
    assert res["error_type"] == "ToolNotFound"
    assert "not registered" in res["error"]


def test_registry_prevents_duplicate_registration():
    custom_reg = ToolRegistry()
    t1 = Tool(name="tool_a", description="A", parameters={}, handler=lambda u, a: None)
    custom_reg.register(t1)
    with pytest.raises(ValueError, match="already registered"):
        custom_reg.register(t1)


# ===========================================================================
# 2. Security & Authentication Checks
# ===========================================================================
def test_unauthenticated_execution_fails_closed():
    # User is None
    with pytest.raises(ValueError, match="Security violation"):
        asyncio.run(execute_tool(None, "list_clients", {}))

    # User missing firm_id
    user_no_firm = AuthedUser(user_id="u1", firm_id="", token_version=1)
    with pytest.raises(ValueError, match="Security violation"):
        asyncio.run(execute_tool(user_no_firm, "list_clients", {}))


def test_firm_id_tampering_in_arguments_blocked():
    user = AuthedUser(user_id="u1", firm_id="firm-alpha", token_version=1)
    res = asyncio.run(execute_tool(user, "list_clients", {"firm_id": "firm-beta"}))
    assert res["success"] is False
    assert res["error_type"] == "InvalidArgument"
    assert "firm_id" in res["error"]


# ===========================================================================
# 3. Argument Validation Checks
# ===========================================================================
def test_argument_validation_missing_required():
    user = AuthedUser(user_id="u1", firm_id="firm-test", token_version=1)

    # list_files requires client_id
    res = asyncio.run(execute_tool(user, "list_files", {}))
    assert res["success"] is False
    assert res["error_type"] == "InvalidArgument"
    assert "Missing required argument: 'client_id'" in res["error"]

    # get_scan_status requires scan_id
    res = asyncio.run(execute_tool(user, "get_scan_status", {}))
    assert res["success"] is False
    assert res["error_type"] == "InvalidArgument"
    assert "Missing required argument: 'scan_id'" in res["error"]


def test_argument_validation_empty_or_whitespace_values():
    user = AuthedUser(user_id="u1", firm_id="firm-test", token_version=1)

    # Empty string
    res = asyncio.run(execute_tool(user, "list_files", {"client_id": ""}))
    assert res["success"] is False
    assert "cannot be null or empty" in res["error"]

    # Whitespace string
    res = asyncio.run(execute_tool(user, "get_scan_status", {"scan_id": "   "}))
    assert res["success"] is False
    assert "cannot be null or empty" in res["error"]


def test_argument_validation_wrong_types_and_extra_arguments():
    user = AuthedUser(user_id="u1", firm_id="firm-test", token_version=1)

    # scan_id passed as integer instead of string
    res = asyncio.run(execute_tool(user, "get_scan_status", {"scan_id": 99999}))
    assert res["success"] is False
    assert "must be a string" in res["error"]

    # Unexpected argument
    res = asyncio.run(execute_tool(user, "list_clients", {"unexpected_param": "val"}))
    assert res["success"] is False
    assert "Unexpected argument" in res["error"]

    # Non-dict argument payload
    res = asyncio.run(execute_tool(user, "list_clients", ["not", "a", "dict"]))
    assert res["success"] is False
    assert "must be provided as a dictionary" in res["error"]


# ===========================================================================
# 4. Tool Execution, Output Structure, and Sanitization
# ===========================================================================
def test_read_only_tools_execution_and_sanitization():
    firm_id = f"firm_{uuid.uuid4().hex[:6]}"
    user = AuthedUser(user_id=f"usr_{uuid.uuid4().hex[:6]}", firm_id=firm_id, token_version=1)

    async def _test():
        # Setup: Client
        client_doc = await services.create_client(user, name="Acme Logistics", client_type="Corporation", notes="Key Client", db=server.db)
        client_id = client_doc["id"]

        # Setup: File
        file_doc = await services.save_client_file(user, client_id, "payroll_q1.csv", b"emp,amt\n1,100\n", db=server.db)
        file_id = file_doc["id"]

        # Setup: Template
        await services.create_template(user, {
            "name": "Standard Corporate Audit",
            "client_type": "Corporation",
            "description": "Annual audit checklist",
            "items": [{"name": "Bank Statement", "aliases": ["Statement"], "required": True}],
        }, db=server.db)

        # Setup: Scan
        scan_id = str(uuid.uuid4())
        await server.db.scans.insert_one({
            "id": scan_id,
            "firm_id": firm_id,
            "client_id": client_id,
            "client_name": "Acme Logistics",
            "status": "completed",
            "progress": 100,
            "processed_files": 1,
            "total_files": 1,
            "total_findings": 2,
            "expected_period": 2024,
            "started_at": services.now_iso(),
            "completed_at": services.now_iso(),
            "summary": {"exact_duplicate": 1, "missing_doc": 1},
        })

        # Setup: Findings
        f1_id = str(uuid.uuid4())
        f2_id = str(uuid.uuid4())
        await server.db.findings.insert_one({
            "id": f1_id,
            "firm_id": firm_id,
            "scan_id": scan_id,
            "client_id": client_id,
            "category": "exact_duplicate",
            "severity": "high",
            "title": "Duplicate Payroll Records",
            "description": "Matching hash detected between uploads.",
            "filenames": ["payroll_q1.csv", "payroll_copy.csv"],
            "status": "unreviewed",
            "detected_at": services.now_iso(),
        })
        await server.db.findings.insert_one({
            "id": f2_id,
            "firm_id": firm_id,
            "scan_id": scan_id,
            "client_id": client_id,
            "category": "missing_doc",
            "severity": "medium",
            "title": "Missing Tax Clearance Certificate",
            "description": "Checklist item was not provided.",
            "filenames": [],
            "status": "keep",
            "detected_at": services.now_iso(),
        })

        # Setup: Agent run
        msg_res = await services.post_agent_message(user, "Please summarize my client", db=server.db)
        run_id = msg_res["run_id"]

        # 1. Test list_clients
        res_clients = await execute_tool(user, "list_clients", {})
        assert res_clients["success"] is True
        clients_list = res_clients["result"]
        assert any(c["id"] == client_id for c in clients_list)
        # Verify sanitization
        first_c = next(c for c in clients_list if c["id"] == client_id)
        assert "name" in first_c and first_c["name"] == "Acme Logistics"
        assert "_id" not in first_c

        # 2. Test list_files
        res_files = await execute_tool(user, "list_files", {"client_id": client_id})
        assert res_files["success"] is True
        files_list = res_files["result"]
        assert len(files_list) >= 1
        first_f = files_list[0]
        assert first_f["id"] == file_id
        assert first_f["filename"] == "payroll_q1.csv"
        # CRITICAL SANITIZATION: No storage paths or raw bytes exposed
        assert "storage_path" not in first_f
        assert "content" not in first_f
        assert "raw_ocr" not in first_f
        assert "_id" not in first_f

        # 3. Test list_templates
        res_tpls = await execute_tool(user, "list_templates", {})
        assert res_tpls["success"] is True
        assert len(res_tpls["result"]) > 0

        # 4. Test get_scan_status
        res_scan = await execute_tool(user, "get_scan_status", {"scan_id": scan_id})
        assert res_scan["success"] is True
        scan_info = res_scan["result"]
        assert scan_info["id"] == scan_id
        assert scan_info["status"] == "completed"
        assert scan_info["progress"] == 100
        assert scan_info["client_name"] == "Acme Logistics"

        # 5. Test get_findings
        res_findings = await execute_tool(user, "get_findings", {"scan_id": scan_id})
        assert res_findings["success"] is True
        findings_list = res_findings["result"]
        assert len(findings_list) == 2
        categories = {f["category"] for f in findings_list}
        assert "exact_duplicate" in categories
        assert "missing_doc" in categories
        # Verify sanitization
        for f in findings_list:
            assert "_id" not in f
            assert "raw_text" not in f
            assert "ocr" not in f

        # Filtered get_findings
        res_filtered = await execute_tool(user, "get_findings", {"scan_id": scan_id, "category": "exact_duplicate"})
        assert res_filtered["success"] is True
        assert len(res_filtered["result"]) == 1
        assert res_filtered["result"][0]["category"] == "exact_duplicate"

        # 6. Test summarize_findings (Uses actual DB records, 0 hallucinations)
        res_summary = await execute_tool(user, "summarize_findings", {"scan_id": scan_id})
        assert res_summary["success"] is True
        summary = res_summary["result"]
        assert summary["scan_id"] == scan_id
        assert summary["total"] == 2
        assert summary["by_category"]["exact_duplicate"] == 1
        assert summary["by_category"]["missing_doc"] == 1
        assert summary["by_status"]["unreviewed"] == 1
        assert summary["by_status"]["keep"] == 1
        assert len(summary["important_findings"]) >= 1
        assert summary["important_findings"][0]["category"] == "exact_duplicate"

        # 7. Test get_agent_run_status
        res_run = await execute_tool(user, "get_agent_run_status", {"run_id": run_id})
        assert res_run["success"] is True
        run_data = res_run["result"]
        assert run_data["id"] == run_id
        assert run_data["status"] == "queued"

    asyncio.run(_test())


# ===========================================================================
# 5. Strict Cross-Tenant Isolation
# ===========================================================================
def test_cross_tenant_isolation_in_tools():
    firm_a = f"firm_a_{uuid.uuid4().hex[:6]}"
    firm_b = f"firm_b_{uuid.uuid4().hex[:6]}"
    user_a = AuthedUser(user_id="usr-a", firm_id=firm_a, token_version=1)
    user_b = AuthedUser(user_id="usr-b", firm_id=firm_b, token_version=1)

    async def _test():
        # Firm A creates resources
        c_a = await services.create_client(user_a, name="Firm A Secret Client", db=server.db)
        client_a_id = c_a["id"]

        f_a = await services.save_client_file(user_a, client_a_id, "confidential.csv", b"data\n", db=server.db)

        scan_a_id = str(uuid.uuid4())
        await server.db.scans.insert_one({
            "id": scan_a_id,
            "firm_id": firm_a,
            "client_id": client_a_id,
            "status": "completed",
        })

        run_a = await services.post_agent_message(user_a, "Private query", db=server.db)
        run_a_id = run_a["run_id"]

        # Firm B attempts to list Firm A's files
        b_files = await execute_tool(user_b, "list_files", {"client_id": client_a_id})
        assert b_files["success"] is False
        assert b_files["error_code"] == 404
        assert "Client not found" in b_files["error"]

        # Firm B attempts to view Firm A's scan
        b_scan = await execute_tool(user_b, "get_scan_status", {"scan_id": scan_a_id})
        assert b_scan["success"] is False
        assert b_scan["error_code"] == 404
        assert "Scan not found" in b_scan["error"]

        # Firm B attempts to view Firm A's findings
        b_findings = await execute_tool(user_b, "get_findings", {"scan_id": scan_a_id})
        assert b_findings["success"] is False
        assert b_findings["error_code"] == 404

        # Firm B attempts to summarize Firm A's findings
        b_summary = await execute_tool(user_b, "summarize_findings", {"scan_id": scan_a_id})
        assert b_summary["success"] is False
        assert b_summary["error_code"] == 404

        # Firm B attempts to inspect Firm A's agent run
        b_run = await execute_tool(user_b, "get_agent_run_status", {"run_id": run_a_id})
        assert b_run["success"] is False
        assert b_run["error_code"] == 404
        assert "Agent run not found" in b_run["error"]

        # Firm B list_clients returns 0 Firm A clients
        b_clients = await execute_tool(user_b, "list_clients", {})
        assert b_clients["success"] is True
        assert all(c["id"] != client_a_id for c in b_clients["result"])

    asyncio.run(_test())
