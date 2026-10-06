"""V1 targeted improvements tests: Claude empty-response single-retry, multimodal document
request shape, analyze_document tool (tenant isolation + limits), and run_scan approval schema.
All hermetic (no network; httpx/storage/provider are faked). No secrets involved."""
import os
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATA_BACKEND", "memory")
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-32-chars-long-ledgerlens-suite")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
os.environ["AGENT_WORKER_ENABLED"] = "false"
os.environ.pop("FAL_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

import server  # noqa: E402
import services  # noqa: E402
import storage  # noqa: E402
from auth_dep import AuthedUser  # noqa: E402
from engine.providers import llm as llm_mod  # noqa: E402
import agent.tools as tools  # noqa: E402
from agent.registry import default_registry  # noqa: E402

ENDPOINT = "https://fal.run/openrouter/router/openai/v1/chat/completions"


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=self)


class _FakeClient:
    """Context manager faking httpx.Client; returns queued responses and records payloads."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _provider():
    return llm_mod.ClaudeOpusFalProvider(api_key="dummy-key-1234567890", endpoint=ENDPOINT, model="m")


def _patch_client(monkeypatch, responses):
    client = _FakeClient(responses)

    def factory(*a, **k):
        return client

    monkeypatch.setattr(llm_mod.httpx, "Client", factory)
    return {"c": client}


# -------------------- #1 retry-on-empty --------------------
def test_empty_response_retries_once_then_succeeds(monkeypatch):
    holder = _patch_client(monkeypatch, [
        _Resp(200, {"choices": [{"message": {"content": ""}}]}),          # empty -> retry
        _Resp(200, {"choices": [{"message": {"content": "Hello"}}]}),      # success
    ])
    out = _provider().generate("hi")
    assert out == "Hello"
    assert len(holder["c"].calls) == 2, "must retry exactly once"


def test_empty_twice_returns_none_after_one_retry(monkeypatch):
    holder = _patch_client(monkeypatch, [
        _Resp(200, {"choices": [{"message": {"content": ""}}]}),
        _Resp(200, {"choices": [{"message": {"content": "   "}}]}),
    ])
    out = _provider().generate("hi")
    assert out is None
    assert len(holder["c"].calls) == 2, "one initial + one retry, no more"


def test_success_not_retried(monkeypatch):
    holder = _patch_client(monkeypatch, [_Resp(200, {"choices": [{"message": {"content": "ok"}}]})])
    out = _provider().generate("hi")
    assert out == "ok"
    assert len(holder["c"].calls) == 1, "no retry when content present"


def test_auth_error_not_retried(monkeypatch):
    holder = _patch_client(monkeypatch, [_Resp(401, {"error": "bad"})])
    out = _provider().generate("hi")
    assert out is None
    assert len(holder["c"].calls) == 1, "auth/5xx errors are NOT retried"


# -------------------- #2 multimodal shape --------------------
def test_generate_multimodal_builds_image_parts(monkeypatch):
    holder = _patch_client(monkeypatch, [_Resp(200, {"choices": [{"message": {"content": "doc says X"}}]})])
    out = _provider().generate_multimodal("what is this?", [("image/png", "QUJD")])
    assert out == "doc says X"
    payload = holder["c"].calls[0]["json"]
    content = payload["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == "data:image/png;base64,QUJD"


def test_generate_multimodal_requires_key_or_images():
    p = llm_mod.ClaudeOpusFalProvider(api_key="", endpoint=ENDPOINT, model="m")
    assert p.generate_multimodal("q", [("image/png", "AA")]) is None   # no key
    assert _provider().generate_multimodal("q", []) is None            # no images


# -------------------- #2 tool: build images + limits --------------------
def test_build_document_images_pdf_pages_and_limits():
    import fitz
    doc = fitz.open()
    for i in range(3):
        pg = doc.new_page(); pg.insert_text((50, 60), f"page {i}")
    data = doc.tobytes(); doc.close()
    imgs = tools._build_document_images(data, "pdf")
    assert len(imgs) == 3 and imgs[0][0] == "image/png"
    with pytest.raises(ValueError):
        tools._build_document_images(b"x" * (tools._DOC_CLAUDE_MAX_BYTES + 10), "pdf")  # too big
    with pytest.raises(ValueError):
        tools._build_document_images(b"data", "csv")  # unsupported type


# -------------------- #2 tool: tenant isolation + provider --------------------
def _seed_file(firm_id, ext="png", sp="acct-doc-checker/uploads/x/f.png", data=b"\x89PNG fake"):
    fid = f"file-{uuid.uuid4().hex[:8]}"
    async def _t():
        await server.db.files.insert_one({"id": fid, "firm_id": firm_id, "client_id": "c", "name": "d.png", "ext": ext, "storage_path": sp, "size": len(data), "is_deleted": False})
    import asyncio; asyncio.run(_t())
    return fid, data


def test_analyze_document_tenant_isolation():
    import asyncio
    firm_a = f"firm-{uuid.uuid4().hex[:6]}"
    fid, _ = _seed_file(firm_a)
    other = AuthedUser(user_id="u2", firm_id=f"firm-{uuid.uuid4().hex[:6]}", token_version=1)
    with pytest.raises(Exception) as e:
        asyncio.run(tools.handle_analyze_document(other, {"file_id": fid}, db=server.db))
    assert getattr(e.value, "status_code", None) == 404, "cross-firm file must be 404"


def test_analyze_document_success_no_leak(monkeypatch):
    import asyncio
    from fastapi import HTTPException
    firm = f"firm-{uuid.uuid4().hex[:6]}"
    uid = f"u-{uuid.uuid4().hex[:6]}"
    fid, data = _seed_file(firm)
    monkeypatch.setattr(storage, "get_object", lambda sp: data)

    class FakeProv:
        available = True
        def generate_multimodal(self, prompt, images, sys_p, max_tokens, timeout=None):
            assert images and images[0][0] == "image/png"
            return "This is an invoice for ACME."

    # handler lazily imports ClaudeOpusFalProvider from engine.providers.llm; patch there.
    monkeypatch.setattr("engine.providers.llm.ClaudeOpusFalProvider", lambda *a, **k: FakeProv())
    u = AuthedUser(user_id=uid, firm_id=firm, token_version=1)
    res = asyncio.run(tools.handle_analyze_document(u, {"file_id": fid, "question": "summarize"}, db=server.db))
    assert res["analysis"] == "This is an invoice for ACME."
    assert "storage_path" not in str(res) and "acct-doc-checker" not in str(res)  # no path leak
    assert res["file_id"] == fid


def test_analyze_document_provider_unconfigured_no_crash(monkeypatch):
    import asyncio
    firm = f"firm-{uuid.uuid4().hex[:6]}"
    fid, data = _seed_file(firm)
    monkeypatch.setattr(storage, "get_object", lambda sp: data)
    monkeypatch.setattr("engine.providers.llm.ClaudeOpusFalProvider", lambda *a, **k: type("P", (), {"available": False})())
    u = AuthedUser(user_id="u", firm_id=firm, token_version=1)
    res = asyncio.run(tools.handle_analyze_document(u, {"file_id": fid}, db=server.db))
    assert res["analysis"] == "" and "not configured" in res["note"]


def test_analyze_document_rejects_unsupported_type(monkeypatch):
    import asyncio
    from fastapi import HTTPException
    firm = f"firm-{uuid.uuid4().hex[:6]}"
    fid, _ = _seed_file(firm, ext="csv", sp="x/f.csv", data=b"a,b,c")
    monkeypatch.setattr(storage, "get_object", lambda sp: b"a,b,c")
    u = AuthedUser(user_id="u", firm_id=firm, token_version=1)
    with pytest.raises(HTTPException) as e:
        asyncio.run(tools.handle_analyze_document(u, {"file_id": fid}, db=server.db))
    assert e.value.status_code == 400


# -------------------- #3 run_scan approval schema (no logic change) --------------------
def test_run_scan_requires_approval_and_not_readonly():
    t = default_registry.get("run_scan")
    assert t is not None
    assert t.approval_required is True
    assert t.read_only is False
    # analyze_document is read-only (no approval), run_scan still gated
    assert default_registry.get("analyze_document").approval_required is False
