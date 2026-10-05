"""Regression tests for A11 — request-id correlation + structured logging.

Covers: strict X-Request-ID sanitisation (anti log-injection), JSON formatter field
inclusion/allow-listing (never leaks secrets), idempotent logging setup, response header
echo, and end-to-end correlation of user_id/firm_id onto an authenticated request's access
log via the pure-ASGI middleware + auth-dependency context binding.
"""
import json
import logging
import os
import re
import sys
import uuid
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
import request_context as rc  # noqa: E402


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines = []
        self.setFormatter(rc.JSONFormatter())
        self.addFilter(rc.RequestContextFilter())

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:  # pragma: no cover
            self.handleError(record)


def _json_records(lines):
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


# --- Unit: sanitisation ---------------------------------------------------------------
def test_sanitize_request_id_accepts_clean_values():
    assert rc.sanitize_request_id("abc-123.X_y") == "abc-123.X_y"


def test_sanitize_request_id_rejects_unsafe_values():
    assert rc.sanitize_request_id(None) is None
    assert rc.sanitize_request_id("") is None
    assert rc.sanitize_request_id("   ") is None
    assert rc.sanitize_request_id("abc\r\nINJECTED: 1") is None  # CRLF injection
    assert rc.sanitize_request_id("with space") is None
    assert rc.sanitize_request_id("a" * 200) is None  # over-long
    assert rc.sanitize_request_id("emoji😀") is None  # wrong charset


# --- Unit: JSON formatter -------------------------------------------------------------
def _make_record(name="t", level=logging.INFO, msg="hello", **extra):
    rec = logging.LogRecord(name, level, __file__, 1, msg, None, None)
    if extra:
        rec.extra_fields = extra
    return rec


def test_json_formatter_injects_context_and_allowlisted_extras():
    # set via tokens to be robust across contextvar impls
    tok_r = rc.request_id_var.set("rid-1")
    tok_u = rc.user_id_var.set("u-1")
    tok_f = rc.firm_id_var.set("f-1")
    try:
        rec = _make_record(msg="http_request", method="GET", path="/api/clients", status=200, duration_ms=1.5)
        filt = rc.RequestContextFilter()
        filt.filter(rec)
        payload = json.loads(rc.JSONFormatter().format(rec))
    finally:
        rc.request_id_var.reset(tok_r)
        rc.user_id_var.reset(tok_u)
        rc.firm_id_var.reset(tok_f)
    assert payload["request_id"] == "rid-1"
    assert payload["user_id"] == "u-1"
    assert payload["firm_id"] == "f-1"
    assert payload["method"] == "GET"
    assert payload["status"] == 200
    assert payload["duration_ms"] == 1.5


def test_json_formatter_drops_disallowed_extras_and_never_leaks_secrets():
    rec = _make_record(msg="http_request", status=200, authorization="Bearer SUPERSECRET", token="abc.def.ghi")
    rc.RequestContextFilter().filter(rec)
    text = rc.JSONFormatter().format(rec)
    payload = json.loads(text)
    assert "authorization" not in payload and "token" not in payload
    assert "SUPERSECRET" not in text
    assert payload["status"] == 200


def test_configure_logging_installs_filter_idempotently():
    handler = logging.Handler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        rc.configure_logging("json")
        rc.configure_logging("json")  # second call must not duplicate the filter
        assert any(isinstance(f, rc.RequestContextFilter) for f in handler.filters)
        assert sum(1 for f in handler.filters if isinstance(f, rc.RequestContextFilter)) == 1
    finally:
        root.removeHandler(handler)


# --- Integration: middleware + correlation -------------------------------------------
def test_response_carries_request_id_header_and_inbound_is_echoed():
    client = TestClient(server.app)
    r = client.get("/healthz")
    assert r.status_code == 200
    rid = r.headers.get("x-request-id")
    assert rid and rc.sanitize_request_id(rid) == rid  # present + valid

    r2 = client.get("/healthz", headers={"X-Request-ID": "trace-abc-123"})
    assert r2.headers.get("x-request-id") == "trace-abc-123"


def test_authenticated_request_correlates_user_and_firm_in_access_log():
    client = TestClient(server.app)
    email = f"obs_{uuid.uuid4().hex[:8]}@example.com"
    signup = client.post("/api/auth/signup", json={"email": email, "password": "SecurePass123!", "firm_name": "Obs Firm", "name": "Obs"})
    assert signup.status_code == 201
    token = signup.json()["access_token"]
    uid = signup.json()["user"]["id"]
    fid = signup.json()["firm"]["id"]

    cap = _Capture()
    access_logger = logging.getLogger("ledgerlens.access")
    prev_level = access_logger.level
    access_logger.setLevel(logging.INFO)  # pytest's logging plugin raises the ROOT level; pin the source logger
    access_logger.addHandler(cap)
    try:
        resp = client.get("/api/clients", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
    finally:
        access_logger.removeHandler(cap)
        access_logger.setLevel(prev_level)

    access = [p for p in _json_records(cap.lines) if p.get("msg") == "http_request" and p.get("path") == "/api/clients"]
    assert access, "no access-log record captured for the request"
    rec = access[-1]
    assert rec["status"] == 200
    assert rec["request_id"] not in ("", None)
    assert rec["user_id"] == uid, "user_id must be correlated from the auth dependency"
    assert rec["firm_id"] == fid, "firm_id must be correlated from the auth dependency"
    assert rec["duration_ms"] >= 0

    # The raw bearer token must never appear in any captured log line.
    joined = "\n".join(cap.lines)
    assert token not in joined
