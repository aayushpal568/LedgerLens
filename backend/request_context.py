"""Request-scoped logging context and a pure-ASGI correlation middleware (A11).

Design goals (smallest safe production-ready observability):
- One stable request id per inbound HTTP request, honouring a client-supplied
  ``X-Request-ID`` only after strict sanitisation (anti log-injection), and always
  echoed back on the response so callers/ops can grep a single id end to end.
- Structured JSON logs (or a contextual text format for local dev) with the ids
  auto-injected, so NO existing ``logger.*`` call site has to change.
- A pure-ASGI middleware (not BaseHTTPMiddleware) so it never buffers the report
  ``StreamingResponse`` and keeps the single asyncio task/context chain intact, which
  means ``asyncio.create_task`` background work (scan / approval-resume) inherits the
  request id for free.

Security: only opaque correlation ids are ever logged. The Authorization header, JWTs,
request bodies, and document contents are NEVER placed into the logging context.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

# --- Correlation context (per asyncio task / context) -----------------------------------
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("user_id", default="-")
firm_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("firm_id", default="-")

_REQUEST_ID_MAX_LEN = 128
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def sanitize_request_id(raw: Optional[str]) -> Optional[str]:
    """Return a safe request id or None. Rejects control chars (CR/LF), spaces, and
    over-long or wrong-charset values so an attacker cannot forge log lines or split the
    echoed response header."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or len(s) > _REQUEST_ID_MAX_LEN:
        return None
    return s if _REQUEST_ID_RE.match(s) else None


def new_request_id() -> str:
    return uuid.uuid4().hex


def bind_auth_context(user_id: Optional[str], firm_id: Optional[str]) -> None:
    """Attach the authenticated tenant to the log context. Correlation-only side effect;
    it never influences authentication decisions or token validation."""
    if user_id:
        user_id_var.set(str(user_id))
    if firm_id:
        firm_id_var.set(str(firm_id))


# --- Structured formatting --------------------------------------------------------------
# Keys an access/structured line is allowed to add alongside the standard fields.
_ALLOWED_EXTRA_KEYS = {
    "method", "path", "status", "duration_ms", "storage_init_ms",
    "pool_size", "pool_idle", "requeued", "failed", "approvals_failed",
}


class RequestContextFilter(logging.Filter):
    """Inject the current correlation ids onto every record. Attached to handlers so it
    also applies to records from child loggers that propagate up."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        if not hasattr(record, "user_id"):
            record.user_id = user_id_var.get()
        if not hasattr(record, "firm_id"):
            record.firm_id = firm_id_var.get()
        return True


class JSONFormatter(logging.Formatter):
    """Render a record as a single-line JSON object with contextual + allow-listed extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "user_id": getattr(record, "user_id", "-"),
            "firm_id": getattr(record, "firm_id", "-"),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k in _ALLOWED_EXTRA_KEYS:
                    payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_TEXT_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s rid=%(request_id)s uid=%(user_id)s "
    "fid=%(firm_id)s %(message)s"
)


def configure_logging(mode: str = "text") -> None:
    """Idempotently install the correlation filter + formatter on the root handler(s).

    ``mode``: 'json' for production, anything else keeps a human-readable but contextual
    text line. Existing messages are unchanged; only the rendered envelope changes and the
    correlation fields are appended."""
    root = logging.getLogger()
    if not root.handlers:  # pragma: no cover - basicConfig normally guarantees one
        logging.basicConfig(level=logging.INFO)
    formatter: logging.Formatter = JSONFormatter() if mode == "json" else logging.Formatter(_TEXT_FORMAT)
    filt = RequestContextFilter()
    for handler in root.handlers:
        handler.setFormatter(formatter)
        if not any(isinstance(f, RequestContextFilter) for f in handler.filters):
            handler.addFilter(filt)


# --- Pure-ASGI correlation middleware ---------------------------------------------------
class RequestContextMiddleware:
    """Assigns/echoes a request id, times the request, and emits one structured access line.

    Implemented as raw ASGI so streaming responses pass through untouched and the request
    runs in a single asyncio context (so downstream deps/background tasks inherit the id)."""

    def __init__(self, app) -> None:
        self.app = app
        self.logger = logging.getLogger("ledgerlens.access")

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        inbound: Optional[str] = None
        for key, value in scope.get("headers", []) or []:
            if key.decode("latin1").lower() == "x-request-id":
                inbound = value.decode("latin1")
                break

        request_id = sanitize_request_id(inbound) or new_request_id()
        token = request_id_var.set(request_id)
        method = scope.get("method", "-")
        path = scope.get("path", "-")  # path only; query string is intentionally not logged
        start = time.perf_counter()
        status_holder = {"code": 500}

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                status_holder["code"] = message.get("status", 500)
                message = dict(message)
                headers = list(message.get("headers", []) or [])
                headers = [h for h in headers if h[0].lower() != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode("latin1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            self.logger.exception(
                "http_request",
                extra={"extra_fields": {"method": method, "path": path, "status": 500, "duration_ms": duration_ms}},
            )
            raise
        else:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            self.logger.info(
                "http_request",
                extra={"extra_fields": {
                    "method": method, "path": path,
                    "status": status_holder["code"], "duration_ms": duration_ms,
                }},
            )
        finally:
            request_id_var.reset(token)
