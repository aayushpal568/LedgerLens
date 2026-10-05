"""B1 regression tests: /healthz stays liveness (always 200); /readyz reports readiness
(200 when DB reachable, 503 when not). Verifies the two are decoupled, both are public
(no auth), and neither leaks secrets. Hermetic (memory backend)."""
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATA_BACKEND", "memory")
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-32-chars-long-ledgerlens-suite")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
os.environ["AGENT_WORKER_ENABLED"] = "false"

import server  # noqa: E402
import storage  # noqa: E402


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        yield c


def test_healthz_is_liveness_200_when_healthy(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "ledgerlens"
    assert body["status"] == "healthy"
    assert body["database"] == "connected"


def test_readyz_is_ready_200_when_db_reachable(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["database"] == "connected"


def test_readyz_returns_503_but_healthz_stays_200_when_db_down(client, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(server.db.firm, "count_documents", boom)

    ready = client.get("/readyz")
    assert ready.status_code == 503, "readiness must fail when the DB is unreachable"
    assert ready.json()["status"] == "not_ready"
    assert ready.json()["database"] == "unreachable"

    # Liveness is intentionally decoupled: still 200 even though the dependency is down.
    live = client.get("/healthz")
    assert live.status_code == 200
    assert live.json()["status"] == "degraded"


def test_probes_are_public_no_auth(client):
    # No Authorization header present → neither probe is gated by auth.
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_probes_do_not_leak_secrets(client, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(server.db.firm, "count_documents", boom)
    joined = client.get("/healthz").text + client.get("/readyz").text
    lowered = joined.lower()
    for needle in ("postgresql://", "password", "auth_secret", "secret_key", "stack", "traceback"):
        assert needle not in lowered


# ====================== B2 Option B: storage in /readyz ======================
def test_readyz_503_when_storage_down_but_db_healthy(client, monkeypatch):
    monkeypatch.setattr(storage, "storage_readiness", lambda: "unconfigured")
    r = client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["database"] == "connected", "DB is healthy; only storage should drive 503"
    assert body["storage"] == "unconfigured"
    # liveness unaffected by a dependency outage
    assert client.get("/healthz").status_code == 200


def test_readyz_503_when_db_and_storage_both_down(client, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(server.db.firm, "count_documents", boom)
    monkeypatch.setattr(storage, "storage_readiness", lambda: "unconfigured")

    r = client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["database"] == "unreachable"
    assert body["storage"] == "unconfigured"
    assert client.get("/healthz").status_code == 200, "liveness stays 200 regardless of readiness"


def test_readyz_200_when_db_and_storage_healthy(client):
    # memory backend → storage resolves to local 'ok'
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["database"] == "connected"
    assert body["storage"] == "ok"


def test_storage_readiness_token_mapping(monkeypatch):
    def ret(mode):
        def _f(*a, **k):
            return mode
        return _f

    monkeypatch.setattr(storage, "init_storage", ret("s3"))
    assert storage.storage_readiness() == "ok"

    def raise_runtime(*a, **k):
        raise RuntimeError("not configured")

    monkeypatch.setattr(storage, "init_storage", raise_runtime)
    assert storage.storage_readiness() == "unconfigured"

    def raise_other(*a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(storage, "init_storage", raise_other)
    assert storage.storage_readiness() == "error"


def test_readyz_storage_never_exposes_secrets(client, monkeypatch):
    monkeypatch.setattr(storage, "storage_readiness", lambda: "ok")
    body = client.get("/readyz").json()
    assert body["storage"] in ("ok", "unconfigured", "error")
    joined = client.get("/readyz").text.lower()
    for needle in ("postgresql://", "s3:", "amazonaws", "bucket", "endpoint", "access_key", "secret", "dsn", "r2.cloudflarestorage"):
        assert needle not in joined
