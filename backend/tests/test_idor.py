"""Comprehensive IDOR, Multi-Tenant Isolation, Password Reset, and Rate Limiting tests for Phase C5."""
import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATA_BACKEND", "memory")
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-32-chars-long-ledgerlens-suite")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")

import server
import services
from agent.registry import default_registry, execute_tool
from auth_dep import AuthedUser
from database import MemoryDatabase


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
# 1. IDOR Cross-Tenant Route Tests
# ===========================================================================
def test_idor_get_client_scans_cross_tenant(client):
    data_a, headers_a = _signup(client, f"idortest_a_{uuid.uuid4().hex[:6]}@firma.com", "IDOR Firm A")
    data_b, headers_b = _signup(client, f"idortest_b_{uuid.uuid4().hex[:6]}@firmb.com", "IDOR Firm B")

    # Firm A creates a client
    client_a = client.post("/api/clients", json={"name": "Client A", "client_type": "tax"}, headers=headers_a).json()

    # Firm B tries to list scans for Client A -> 404
    res = client.get(f"/api/clients/{client_a['id']}/scans", headers=headers_b)
    assert res.status_code == 404


def test_idor_cancel_scan_cross_tenant(client):
    data_a, headers_a = _signup(client, f"cancel_a_{uuid.uuid4().hex[:6]}@firma.com", "Cancel Firm A")
    data_b, headers_b = _signup(client, f"cancel_b_{uuid.uuid4().hex[:6]}@firmb.com", "Cancel Firm B")

    # Setup fake scan in Firm A
    fake_scan_id = str(uuid.uuid4())
    asyncio.run(server.db.scans.insert_one({
        "id": fake_scan_id,
        "firm_id": data_a["firm"]["id"],
        "client_id": "c-1",
        "status": "running",
    }))

    # Firm B tries to cancel Firm A's scan -> 404
    res = client.post(f"/api/scans/{fake_scan_id}/cancel", headers=headers_b)
    assert res.status_code == 404

    # Firm A cancels Firm A's scan -> 200
    res_ok = client.post(f"/api/scans/{fake_scan_id}/cancel", headers=headers_a)
    assert res_ok.status_code == 200


def test_idor_update_firm_cross_tenant(client):
    data_a, headers_a = _signup(client, f"firm_a_{uuid.uuid4().hex[:6]}@firma.com", "Firm A Original")
    data_b, headers_b = _signup(client, f"firm_b_{uuid.uuid4().hex[:6]}@firmb.com", "Firm B Original")

    # Firm B updates its firm
    res_b = client.put("/api/firm", json={"name": "Firm B Updated"}, headers=headers_b)
    assert res_b.status_code == 200
    assert res_b.json()["name"] == "Firm B Updated"

    # Firm A's name must be untouched
    res_a = client.get("/api/firm", headers=headers_a)
    assert res_a.status_code == 200
    assert res_a.json()["name"] == "Firm A Original"


def test_idor_checklist_templates_cross_tenant(client):
    data_a, headers_a = _signup(client, f"tpl_a_{uuid.uuid4().hex[:6]}@firma.com", "Template Firm A")
    data_b, headers_b = _signup(client, f"tpl_b_{uuid.uuid4().hex[:6]}@firmb.com", "Template Firm B")

    # Firm A creates a custom template
    res_create = client.post(
        "/api/checklist-templates",
        json={"name": "Confidential A Template", "client_type": "tax", "items": []},
        headers=headers_a,
    )
    assert res_create.status_code == 200
    tpl_a = res_create.json()

    # Firm B lists templates - should NOT contain Confidential A Template
    res_list_b = client.get("/api/checklist-templates", headers=headers_b)
    assert res_list_b.status_code == 200
    b_names = [t["name"] for t in res_list_b.json()]
    assert "Confidential A Template" not in b_names

    # Firm B attempts to update Firm A's template -> 404
    res_upd = client.put(
        f"/api/checklist-templates/{tpl_a['id']}",
        json={"name": "Hacked Template", "client_type": "tax", "items": []},
        headers=headers_b,
    )
    assert res_upd.status_code == 404

    # Firm B attempts to delete Firm A's template -> 404
    res_del = client.delete(f"/api/checklist-templates/{tpl_a['id']}", headers=headers_b)
    assert res_del.status_code == 404


# ===========================================================================
# 2. Agent Action Tools Cross-Tenant Isolation
# ===========================================================================
def test_agent_action_tools_cross_tenant():
    async def _test():
        db = MemoryDatabase()
        user_a = AuthedUser(user_id="u-a", firm_id="firm-a", token_version=1)
        user_b = AuthedUser(user_id="u-b", firm_id="firm-b", token_version=1)

        # Setup Client and Template for Firm A
        client_a = await services.create_client(user_a, name="Client A", client_type="tax", db=db)
        tpl_a = await services.create_template(user_a, {"name": "Tpl A", "client_type": "tax", "items": []}, db=db)

        # Firm B attempts run_scan on Firm A's client via agent tool -> fails closed
        res = await execute_tool(
            user_b,
            "run_scan",
            {"client_id": client_a["id"], "template_id": tpl_a["id"]},
            registry=default_registry,
            db=db,
            is_approved=True,
        )
        assert res.get("success") is False
        assert "404" in res.get("error", "") or "not found" in res.get("error", "").lower()

        # Firm B executes create_client -> created client must have firm_b
        res_create = await execute_tool(
            user_b,
            "create_client",
            {"name": "Client from Agent B", "notes": "Audit notes"},
            registry=default_registry,
            db=db,
            is_approved=True,
        )
        assert res_create.get("success") is True
        created_client = res_create["result"]
        # Query created client directly to confirm firm_id
        client_doc = await db.clients.find_one({"id": created_client["client_id"]})
        assert client_doc["firm_id"] == "firm-b"

        # Firm A creates a finding
        finding_id = str(uuid.uuid4())
        await db.findings.insert_one({
            "id": finding_id,
            "firm_id": "firm-a",
            "scan_id": "s-1",
            "status": "unreviewed",
            "notes": "",
        })

        # Firm B attempts to review Firm A's finding via agent tool -> fails closed
        res_rev = await execute_tool(
            user_b,
            "set_finding_review",
            {"finding_id": finding_id, "review_status": "accepted", "review_notes": "Attacked"},
            registry=default_registry,
            db=db,
            is_approved=True,
        )
        assert res_rev.get("success") is False

    asyncio.run(_test())


# ===========================================================================
# 3. Password Reset Flow Tests
# ===========================================================================
def test_password_reset_flow(client):
    email = f"reset_{uuid.uuid4().hex[:6]}@example.com"
    data, _ = _signup(client, email, "Reset Firm", password="OldPassword123!")
    user_id = data["user"]["id"]

    # 1. Request password reset
    forgot_res = client.post("/api/auth/forgot-password", json={"email": email})
    assert forgot_res.status_code == 200
    reset_data = forgot_res.json()
    assert "reset_token" in reset_data
    token = reset_data["reset_token"]

    # 2. Reset password with valid token
    reset_res = client.post("/api/auth/reset-password", json={"token": token, "new_password": "NewSecurePassword456!"})
    assert reset_res.status_code == 200
    assert "successfully" in reset_res.json()["message"]

    # 3. Old password should now fail login
    login_old = client.post("/api/auth/login", json={"email": email, "password": "OldPassword123!"})
    assert login_old.status_code == 401

    # 4. New password succeeds login
    login_new = client.post("/api/auth/login", json={"email": email, "password": "NewSecurePassword456!"})
    assert login_new.status_code == 200
    assert "access_token" in login_new.json()

    # 5. Token is single-use: repeating reset with same token must fail
    repeat_res = client.post("/api/auth/reset-password", json={"token": token, "new_password": "AnotherPassword789!"})
    assert repeat_res.status_code == 400


# ===========================================================================
# 4. Rate Limiter and AI Quota Tests
# ===========================================================================
def test_rate_limiter_and_firm_ai_quota(client):
    data, headers = _signup(client, f"quota_{uuid.uuid4().hex[:6]}@example.com", "Quota Firm")
    firm_id = data["firm"]["id"]

    # Set firm quota limit to 2 messages
    asyncio.run(server.db.firm.update_one(
        {"id": firm_id},
        {"$set": {"settings.ai_quota_limit": 2, "settings.ai_usage_count": 0}},
    ))

    # Message 1 -> OK (202)
    res1 = client.post("/api/agent/messages", json={"text": "Query 1"}, headers=headers)
    assert res1.status_code == 202

    # Message 2 -> OK (202)
    res2 = client.post("/api/agent/messages", json={"text": "Query 2"}, headers=headers)
    assert res2.status_code == 202

    # Message 3 -> Quota exceeded -> 429
    res3 = client.post("/api/agent/messages", json={"text": "Query 3"}, headers=headers)
    assert res3.status_code == 429
    assert "quota exceeded" in res3.json()["detail"].lower()
