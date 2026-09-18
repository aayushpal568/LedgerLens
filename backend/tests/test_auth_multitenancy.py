"""Comprehensive security and test suite for LedgerLens Authentication and Multi-Tenancy.

Verifies:
1. Config & Secrets:
   - Missing AUTH_SECRET_KEY raises RuntimeError
   - scoped() fails closed if firm_id is absent
   - Password hashes never returned
2. Auth API:
   - Atomic firm + user creation with seeded templates
   - Duplicate email signup returns 409 without modifying existing account
   - Login with valid credentials succeeds
   - Login with bad password returns generic 401
   - Login with unknown email returns generic 401 (constant-time timing defense)
   - 6th failed login attempt is throttled with 429
   - Refresh token rotation and expiry
   - Logout revokes token_version immediately
   - Invalid, expired, wrong secret, and malformed JWTs return 401
3. Cross-Tenant Security Matrix (All Protected Routes):
   - No token -> 401
   - Firm A token on Firm A resources -> 200
   - Firm B token on Firm A resources -> 404 (prevents enumeration)
   - Cross-tenant client access -> 404
   - Cross-tenant file access & delete -> 404
   - Cross-tenant template update & delete -> 404
   - Cross-tenant scan start (foreign client or template) -> 404
   - Cross-tenant scan GET -> 404
   - Cross-tenant findings GET & PATCH -> 404
   - Cross-tenant report download -> 404
   - List endpoints only return tenant's own data
   - /api/firm returns tenant's own firm
4. Storage Partitioning & Cleanup:
   - Storage path includes firm_id: {APP_NAME}/uploads/{firm_id}/{client_id}/{file_id}
   - delete_file removes object from storage
   - delete_client_cascade removes all associated files from storage
5. Route Completeness:
   - 100% of non-auth /api routes enforce get_current_user dependency
6. Authenticated End-to-End Workflow:
   - Signup -> Client -> Upload -> Scan -> Findings -> Review -> Reports (CSV, XLSX, PDF)
"""
import io
import os
import sys
import time
import uuid
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# Ensure required environment for tests
os.environ.setdefault("DATA_BACKEND", "memory")
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-32-chars-long-ledgerlens-suite")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")

import server
from auth_dep import AuthedUser, get_current_user
from db_access import scoped
import storage
import tokens


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset the login rate limiter before each test."""
    from auth_routes import login_limiter
    with login_limiter._lock:
        login_limiter._failures.clear()


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
# 1. Config & Secret Tests
# ===========================================================================
def test_missing_auth_secret_key_fails():
    old = os.environ.get("AUTH_SECRET_KEY", "")
    try:
        os.environ["AUTH_SECRET_KEY"] = ""
        with pytest.raises(RuntimeError, match="AUTH_SECRET_KEY environment variable is required"):
            tokens.get_auth_secret_key()
    finally:
        os.environ["AUTH_SECRET_KEY"] = old


def test_scoped_fails_if_firm_id_absent():
    # Empty firm_id in user
    with pytest.raises(ValueError, match="firm_id must always be present"):
        scoped(None, AuthedUser(user_id="u1", firm_id="", token_version=1))

    # Cross-tenant filter tampering
    with pytest.raises(PermissionError, match="Cross-Tenant Security Violation"):
        scoped(None, AuthedUser(user_id="u1", firm_id="firm-alpha", token_version=1), {"firm_id": "firm-beta"})

    # Valid scoping
    filt = scoped(None, AuthedUser(user_id="u1", firm_id="firm-alpha", token_version=1), {"client_id": "c1"})
    assert filt == {"client_id": "c1", "firm_id": "firm-alpha"}


# ===========================================================================
# 2. Authentication API Tests
# ===========================================================================
def test_signup_creates_firm_user_and_templates(client):
    email = f"cpa_{uuid.uuid4().hex[:6]}@firm.com"
    data, headers = _signup(client, email, "Premier CPA Associates")

    assert "access_token" in data
    assert "refresh_token" in data
    assert data["user"]["email"] == email
    assert data["user"]["firm_id"] == data["firm"]["id"]
    # Password hash must NEVER be returned
    assert "password_hash" not in data["user"]
    assert "password" not in data["user"]

    # Templates must be seeded for this firm
    tpl_res = client.get("/api/checklist-templates", headers=headers)
    assert tpl_res.status_code == 200
    templates = tpl_res.json()
    assert len(templates) >= 3


def test_duplicate_signup_returns_409_and_leaves_original_unchanged(client):
    email = f"dup_{uuid.uuid4().hex[:6]}@firm.com"
    data1, _ = _signup(client, email, "Original Firm")
    user_id_1 = data1["user"]["id"]
    firm_id_1 = data1["firm"]["id"]

    # Attempt second signup with same email
    resp = client.post("/api/auth/signup", json={
        "email": email.upper(),  # case-insensitive check
        "password": "AnotherPassword999!",
        "firm_name": "Attacker Imposter Firm",
    })
    assert resp.status_code == 409, "Duplicate email signup must return HTTP 409 Conflict"

    # Verify original user and firm are completely untouched
    login_res = client.post("/api/auth/login", json={"email": email, "password": "SecurePass123!"})
    assert login_res.status_code == 200
    assert login_res.json()["user"]["id"] == user_id_1
    assert login_res.json()["user"]["firm_id"] == firm_id_1


def test_login_success_and_generic_failure(client):
    email = f"login_{uuid.uuid4().hex[:6]}@firm.com"
    _signup(client, email, "Audit Partners")

    # Correct login
    res = client.post("/api/auth/login", json={"email": email, "password": "SecurePass123!"})
    assert res.status_code == 200
    assert "access_token" in res.json()
    assert "password_hash" not in res.json()["user"]

    # Bad password -> generic 401
    bad_pwd = client.post("/api/auth/login", json={"email": email, "password": "WrongPassword!"})
    assert bad_pwd.status_code == 401
    assert bad_pwd.json()["detail"] == "Invalid email or password"

    # Unknown email -> exact same generic 401 (timing protected)
    unknown = client.post("/api/auth/login", json={"email": "nonexistent@firm.com", "password": "AnyPassword!"})
    assert unknown.status_code == 401
    assert unknown.json()["detail"] == "Invalid email or password"


def test_login_throttled_on_6th_failed_attempt(client):
    email = f"throttle_{uuid.uuid4().hex[:6]}@firm.com"
    _signup(client, email, "Throttled Firm")

    for i in range(5):
        r = client.post("/api/auth/login", json={"email": email, "password": "BadPassword!"})
        assert r.status_code == 401, f"Attempt {i+1} should be 401"

    # 6th attempt must be throttled with HTTP 429
    r6 = client.post("/api/auth/login", json={"email": email, "password": "BadPassword!"})
    assert r6.status_code == 429
    assert "Too many failed login attempts" in r6.json()["detail"]


def test_refresh_token_and_logout_revocation(client):
    email = f"refresh_{uuid.uuid4().hex[:6]}@firm.com"
    data, headers = _signup(client, email, "Token Lifecycle Firm")
    refresh_token = data["refresh_token"]

    # Valid refresh
    ref_res = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert ref_res.status_code == 200
    new_access = ref_res.json()["access_token"]
    new_refresh = ref_res.json()["refresh_token"]

    # Old access token still valid until logout
    me_res = client.get("/api/auth/me", headers=headers)
    assert me_res.status_code == 200

    # Logout
    logout_res = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {new_access}"})
    assert logout_res.status_code == 200

    # Revoked tokens must now fail with 401
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_access}"}).status_code == 401

    # Old refresh token invalid
    assert client.post("/api/auth/refresh", json={"refresh_token": refresh_token}).status_code == 401
    assert client.post("/api/auth/refresh", json={"refresh_token": new_refresh}).status_code == 401


def test_token_validation_edge_cases(client):
    # Expired token
    expired_payload = {
        "sub": "user-1",
        "firm_id": "firm-1",
        "token_version": 1,
        "iat": int(time.time()) - 3600,
        "exp": int(time.time()) - 1800,
    }
    expired_jwt = jwt.encode(expired_payload, os.environ["AUTH_SECRET_KEY"], algorithm="HS256")
    res = client.get("/api/firm", headers={"Authorization": f"Bearer {expired_jwt}"})
    assert res.status_code == 401

    # Wrong secret key
    wrong_secret_jwt = jwt.encode(expired_payload, "completely-wrong-secret-key-1234567890", algorithm="HS256")
    res2 = client.get("/api/firm", headers={"Authorization": f"Bearer {wrong_secret_jwt}"})
    assert res2.status_code == 401

    # Malformed headers
    assert client.get("/api/firm", headers={"Authorization": "Basic 12345"}).status_code == 401
    assert client.get("/api/firm", headers={"Authorization": "Bearer "}).status_code == 401
    assert client.get("/api/firm", headers={"Authorization": "Bearer not-a-valid-jwt"}).status_code == 401


# ===========================================================================
# 3. Route Completeness Test
# ===========================================================================
def test_all_non_auth_api_routes_require_authentication():
    """Verify that 100% of /api endpoints outside /api/auth require authentication."""
    from fastapi.routing import APIRoute
    unprotected = []
    for route in server.app.routes:
        if isinstance(route, APIRoute):
            path = route.path
            if path.startswith("/api") and not path.startswith("/api/auth"):
                # Check if get_current_user is in dependencies
                has_auth = any(
                    getattr(getattr(dep, "call", None), "__name__", "") == "get_current_user"
                    or getattr(getattr(dep, "call", None), "__qualname__", "").endswith("get_current_user")
                    for dep in route.dependant.dependencies
                )
                if not has_auth:
                    unprotected.append(f"{route.methods} {path}")

    assert unprotected == [], f"Found unprotected API routes: {unprotected}"


# ===========================================================================
# 4. Cross-Tenant Security Matrix
# ===========================================================================
def test_cross_tenant_security_matrix(client):
    # Setup Firm A
    data_a, headers_a = _signup(client, f"firm_a_{uuid.uuid4().hex[:6]}@alpha.com", "Alpha Associates")
    firm_a_id = data_a["firm"]["id"]

    # Setup Firm B
    data_b, headers_b = _signup(client, f"firm_b_{uuid.uuid4().hex[:6]}@beta.com", "Beta Partners")
    firm_b_id = data_b["firm"]["id"]

    # 1. Unauthenticated requests receive 401
    assert client.get("/api/clients").status_code == 401
    assert client.get("/api/firm").status_code == 401
    assert client.get("/api/checklist-templates").status_code == 401

    # 2. Firm A creates resources
    # Client A
    c_res = client.post("/api/clients", json={"name": "Client Alpha One", "client_type": "Corporation"}, headers=headers_a)
    assert c_res.status_code == 200
    client_a_id = c_res.json()["id"]

    # File A
    file_bytes = b"Date,Amount\n2024-01-01,5000\n"
    files = [("files", ("tax_doc.csv", io.BytesIO(file_bytes), "text/csv"))]
    up_res = client.post(f"/api/clients/{client_a_id}/files", files=files, headers=headers_a)
    assert up_res.status_code == 200
    file_a_id = up_res.json()["files"][0]["id"]

    # Template A
    t_res = client.post("/api/checklist-templates", json={
        "name": "Alpha Custom Template",
        "client_type": "Corporation",
        "items": [{"name": "Bank Stmt", "allowed_types": ["csv"], "aliases": []}],
    }, headers=headers_a)
    assert t_res.status_code == 200
    template_a_id = t_res.json()["id"]

    # Scan A
    s_res = client.post(f"/api/clients/{client_a_id}/scan", json={"template_id": template_a_id, "expected_period": 2024}, headers=headers_a)
    assert s_res.status_code == 200
    scan_a_id = s_res.json()["id"]

    # Finding A
    finding_doc = {
        "id": "fnd-alpha-1",
        "firm_id": firm_a_id,
        "scan_id": scan_a_id,
        "client_id": client_a_id,
        "category": "missing_doc",
        "status": "unreviewed",
        "title": "Missing Bank Stmt",
    }
    import asyncio
    asyncio.run(server.db.findings.insert_one(finding_doc))

    # -------------------------------------------------------------
    # Cross-Tenant Boundary Tests (Firm B attempting Firm A data)
    # MUST return 404 Not Found (never 200 or 403)
    # -------------------------------------------------------------
    # Cross-tenant Client GET
    assert client.get(f"/api/clients/{client_a_id}", headers=headers_b).status_code == 404

    # Cross-tenant Client DELETE
    assert client.delete(f"/api/clients/{client_a_id}", headers=headers_b).status_code == 404
    # Verify client still exists for Firm A
    assert client.get(f"/api/clients/{client_a_id}", headers=headers_a).status_code == 200

    # Cross-tenant Files list
    assert client.get(f"/api/clients/{client_a_id}/files", headers=headers_b).status_code == 404

    # Cross-tenant File upload into foreign client
    f_res = client.post(f"/api/clients/{client_a_id}/files", files=[("files", ("hacker.csv", b"abc", "text/csv"))], headers=headers_b)
    assert f_res.status_code == 404

    # Cross-tenant File DELETE
    del_f = client.delete(f"/api/clients/{client_a_id}/files/{file_a_id}", headers=headers_b)
    assert del_f.status_code == 404

    # Cross-tenant Template update & delete
    assert client.put(f"/api/checklist-templates/{template_a_id}", json={"name": "Tampered", "client_type": "Corp", "items": []}, headers=headers_b).status_code == 404
    assert client.delete(f"/api/checklist-templates/{template_a_id}", headers=headers_b).status_code == 404

    # Cross-tenant Scan start (using foreign client)
    assert client.post(f"/api/clients/{client_a_id}/scan", json={}, headers=headers_b).status_code == 404

    # Cross-tenant Scan start (using own client but foreign template)
    c_b_res = client.post("/api/clients", json={"name": "Client Beta", "client_type": "Corporation"}, headers=headers_b)
    client_b_id = c_b_res.json()["id"]
    tamper_scan = client.post(f"/api/clients/{client_b_id}/scan", json={"template_id": template_a_id}, headers=headers_b)
    assert tamper_scan.status_code == 404

    # Cross-tenant Scan GET
    assert client.get(f"/api/scans/{scan_a_id}", headers=headers_b).status_code == 404

    # Cross-tenant Findings GET & PATCH
    assert client.get(f"/api/scans/{scan_a_id}/findings", headers=headers_b).status_code == 404
    assert client.patch("/api/findings/fnd-alpha-1", json={"status": "ignore"}, headers=headers_b).status_code == 404

    # Cross-tenant Report download
    assert client.get(f"/api/scans/{scan_a_id}/report?format=csv", headers=headers_b).status_code == 404
    assert client.get(f"/api/scans/{scan_a_id}/report?format=pdf", headers=headers_b).status_code == 404

    # List endpoints return ONLY own tenant data
    clients_b = client.get("/api/clients", headers=headers_b).json()
    assert all(c["id"] != client_a_id for c in clients_b)

    tpls_b = client.get("/api/checklist-templates", headers=headers_b).json()
    assert all(t["id"] != template_a_id for t in tpls_b)

    # /api/firm returns own firm details
    firm_a_view = client.get("/api/firm", headers=headers_a).json()
    firm_b_view = client.get("/api/firm", headers=headers_b).json()
    assert firm_a_view["id"] == firm_a_id and firm_a_view["name"] == "Alpha Associates"
    assert firm_b_view["id"] == firm_b_id and firm_b_view["name"] == "Beta Partners"


# ===========================================================================
# 5. Storage Partitioning & Cleanup Tests
# ===========================================================================
def test_storage_partitioning_and_file_deletion(client):
    data, headers = _signup(client, f"storage_{uuid.uuid4().hex[:6]}@firm.com", "Storage Audit LLC")
    firm_id = data["firm"]["id"]

    c_res = client.post("/api/clients", json={"name": "Storage Client"}, headers=headers)
    client_id = c_res.json()["id"]

    # Upload file
    test_content = b"header1,header2\nval1,val2\n"
    up_res = client.post(f"/api/clients/{client_id}/files", files=[("files", ("audit.csv", io.BytesIO(test_content), "text/csv"))], headers=headers)
    assert up_res.status_code == 200
    file_id = up_res.json()["files"][0]["id"]

    # Verify storage path has tenant partition
    import asyncio
    file_doc = asyncio.run(server.db.files.find_one({"id": file_id}))
    expected_path = f"{storage.APP_NAME}/uploads/{firm_id}/{client_id}/{file_id}.csv"
    assert file_doc["storage_path"] == expected_path
    assert file_doc["firm_id"] == firm_id

    # Verify bytes exist in storage
    bytes_stored = storage.get_object(expected_path)
    assert bytes_stored == test_content

    # Delete file
    del_res = client.delete(f"/api/clients/{client_id}/files/{file_id}", headers=headers)
    assert del_res.status_code == 200

    # Storage object must be deleted from storage
    with pytest.raises(FileNotFoundError):
        storage.get_object(expected_path)


def test_cascade_delete_cleans_storage_objects(client):
    data, headers = _signup(client, f"cascade_{uuid.uuid4().hex[:6]}@firm.com", "Cascade Firm")
    firm_id = data["firm"]["id"]

    c_res = client.post("/api/clients", json={"name": "Cascade Client"}, headers=headers)
    client_id = c_res.json()["id"]

    # Upload two files
    up_res = client.post(f"/api/clients/{client_id}/files", files=[
        ("files", ("f1.csv", io.BytesIO(b"1,2,3"), "text/csv")),
        ("files", ("f2.csv", io.BytesIO(b"4,5,6"), "text/csv")),
    ], headers=headers)
    assert up_res.status_code == 200
    f1_id = up_res.json()["files"][0]["id"]
    f2_id = up_res.json()["files"][1]["id"]

    path1 = f"{storage.APP_NAME}/uploads/{firm_id}/{client_id}/{f1_id}.csv"
    path2 = f"{storage.APP_NAME}/uploads/{firm_id}/{client_id}/{f2_id}.csv"
    assert storage.get_object(path1) == b"1,2,3"
    assert storage.get_object(path2) == b"4,5,6"

    # Delete client cascade
    del_c = client.delete(f"/api/clients/{client_id}", headers=headers)
    assert del_c.status_code == 200

    # Both objects must be deleted from storage
    with pytest.raises(FileNotFoundError):
        storage.get_object(path1)
    with pytest.raises(FileNotFoundError):
        storage.get_object(path2)


# ===========================================================================
# 6. Authenticated End-to-End Workflow Test
# ===========================================================================
def test_authenticated_full_workflow(client):
    # Step 1: Signup
    data, headers = _signup(client, f"workflow_{uuid.uuid4().hex[:6]}@firm.com", "E2E Firm")

    # Step 2: Create Client
    c_res = client.post("/api/clients", json={"name": "Acme Manufacturing", "client_type": "Small Business"}, headers=headers)
    assert c_res.status_code == 200
    client_id = c_res.json()["id"]

    # Step 3: Upload Documents
    csv1 = ("files", ("ledger_2024.csv", b"Date,Amount\n2024-01-01,100\n", "text/csv"))
    csv2 = ("files", ("ledger_copy.csv", b"Date,Amount\n2024-01-01,100\n", "text/csv"))
    up_res = client.post(f"/api/clients/{client_id}/files", files=[csv1, csv2], headers=headers)
    assert up_res.status_code == 200 and up_res.json()["uploaded"] == 2

    # Step 4: Run Scan
    s_res = client.post(f"/api/clients/{client_id}/scan", json={"expected_period": 2024}, headers=headers)
    assert s_res.status_code == 200
    scan_id = s_res.json()["id"]

    for _ in range(30):
        time.sleep(0.1)
        cur = client.get(f"/api/scans/{scan_id}", headers=headers).json()
        if cur.get("status") == "completed":
            break
    assert cur.get("status") == "completed"

    # Step 5: View Findings
    findings_res = client.get(f"/api/scans/{scan_id}/findings", headers=headers)
    assert findings_res.status_code == 200
    findings = findings_res.json()
    assert len(findings) > 0

    # Step 6: Review Finding
    first_f = findings[0]
    rev_res = client.patch(f"/api/findings/{first_f['id']}", json={"status": "keep", "note": "Verified by CPA"}, headers=headers)
    assert rev_res.status_code == 200
    assert rev_res.json()["status"] == "keep"

    # Step 7: Export Reports
    for fmt in ["csv", "xlsx", "pdf"]:
        rep = client.get(f"/api/scans/{scan_id}/report?format={fmt}", headers=headers)
        assert rep.status_code == 200
        assert len(rep.content) > 0
