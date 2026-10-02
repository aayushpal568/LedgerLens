import asyncio
import logging
import os
import uuid
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, APIRouter, UploadFile, File, HTTPException, Request, Depends, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ConfigDict, field_validator
from starlette.middleware.cors import CORSMiddleware

from engine import run_detection, default_templates, SUPPORTED_EXTENSIONS
from engine import report as report_engine
import storage

ROOT_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT_DIR / ".env")
except Exception:
    pass

# -----------------------------------------------------------------------------
# Configuration Validation (Fail-Closed)
# -----------------------------------------------------------------------------
INSECURE_PLACEHOLDER_KEYS = {
    "change-this-to-a-secure-random-secret-key-in-production",
    "secret",
    "password",
    "12345678",
    "ledgerlens-secret-key",
}

AUTH_SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", "").strip()
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development").lower().strip()

if not AUTH_SECRET_KEY:
    config_error_auth = "AUTH_SECRET_KEY environment variable is required but missing or empty."
elif len(AUTH_SECRET_KEY) < 32:
    config_error_auth = "AUTH_SECRET_KEY must be at least 32 characters long for cryptographic security."
elif (ENVIRONMENT == "production" or os.environ.get("DATA_BACKEND", "").lower() == "postgres") and AUTH_SECRET_KEY in INSECURE_PLACEHOLDER_KEYS:
    config_error_auth = "Security violation: Insecure placeholder AUTH_SECRET_KEY cannot be used in production."
else:
    config_error_auth = None


CORS_ORIGINS_RAW = os.environ.get("CORS_ORIGINS", "").strip()
if not CORS_ORIGINS_RAW:
    config_error_cors = "CORS_ORIGINS environment variable is required (e.g. http://localhost:3000)."
elif "*" in [o.strip() for o in CORS_ORIGINS_RAW.split(",")]:
    config_error_cors = "CORS_ORIGINS cannot contain wildcard '*' when credentials are enabled."
else:
    config_error_cors = None
    CORS_ALLOWED_ORIGINS = [o.strip() for o in CORS_ORIGINS_RAW.split(",") if o.strip()]

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATA_BACKEND = os.environ.get("DATA_BACKEND", "postgres").lower().strip()

from database import get_database

config_error: Optional[str] = config_error_auth or config_error_cors
try:
    db = get_database(DATABASE_URL or None, DATA_BACKEND, reset=True)
except Exception as e:
    config_error = str(e)
    db = None


from auth_dep import AuthedUser, get_current_user
from db_access import scoped
from auth_routes import auth_router

app = FastAPI(title="LedgerLens Cloud Accounting AI API")

# Protected API Router: all routes strictly require authentication
api_router = APIRouter(prefix="/api", dependencies=[Depends(get_current_user)])


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

import services

CANCEL_REQUESTS = services.CANCEL_REQUESTS
now_iso = services.now_iso
new_id = services.new_id

from collections import defaultdict
import threading
import time


class SlidingWindowRateLimiter:
    """Thread-safe sliding-window rate limiter per tenant key."""

    def __init__(self, max_requests: int, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._records = defaultdict(list)
        self._lock = threading.Lock()

    def check_and_record(self, key: str) -> bool:
        """Returns True if request is allowed, False if throttled."""
        with self._lock:
            now = time.time()
            cutoff = now - self.window_seconds
            timestamps = [t for t in self._records[key] if t > cutoff]
            if len(timestamps) >= self.max_requests:
                self._records[key] = timestamps
                return False
            timestamps.append(now)
            self._records[key] = timestamps
            return True


file_upload_limiter = SlidingWindowRateLimiter(
    max_requests=int(os.environ.get("RATE_LIMIT_FILES_PER_MIN", "60")),
    window_seconds=60,
)
scan_start_limiter = SlidingWindowRateLimiter(
    max_requests=int(os.environ.get("RATE_LIMIT_SCANS_PER_MIN", "30")),
    window_seconds=60,
)
agent_msg_limiter = SlidingWindowRateLimiter(
    max_requests=int(os.environ.get("RATE_LIMIT_MESSAGES_PER_MIN", "60")),
    window_seconds=60,
)
clean = services.clean
_run_scan = services._run_scan
_finalize_scan = services._finalize_scan
_load_items = services._load_items
_make_progress = services._make_progress


# ----------------------------- models ------------------------------
class FirmUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: Optional[str] = None
    contact_email: Optional[str] = None
    retention_note: Optional[str] = None
    settings: Optional[dict] = None


class ClientCreate(BaseModel):
    name: str
    client_type: str = "Small Business"
    notes: Optional[str] = ""

    @field_validator("name")
    @classmethod
    def _name_required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Client name is required and cannot be blank.")
        return v


class ChecklistItem(BaseModel):
    name: str
    aliases: List[str] = Field(default_factory=list)
    allowed_types: List[str] = Field(default_factory=list)
    rule: dict = Field(default_factory=dict)


class TemplateBody(BaseModel):
    name: str
    client_type: str
    items: List[ChecklistItem]


class ScanBody(BaseModel):
    template_id: Optional[str] = None
    expected_period: Optional[int] = None


class FindingUpdate(BaseModel):
    status: Optional[str] = Field(default=None, pattern="^(unreviewed|keep|keep_both|ignore|review_later)$")
    note: Optional[str] = Field(default=None, max_length=10000)


class AgentMessageRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    thread_id: Optional[str] = None
    text: str
    idempotency_key: Optional[str] = None


class ApprovalRejectRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: Optional[str] = None


# ----------------------------- firm --------------------------------
@api_router.get("/firm")
async def get_firm(current_user: AuthedUser = Depends(get_current_user)):
    return await services.get_firm(current_user, db=db)


@api_router.put("/firm")
async def update_firm(body: FirmUpdate, current_user: AuthedUser = Depends(get_current_user)):
    return await services.update_firm(current_user, body.model_dump(), db=db)


# ---------------------------- clients ------------------------------
@api_router.get("/clients")
async def list_clients(current_user: AuthedUser = Depends(get_current_user)):
    return await services.list_clients(current_user, db=db)


@api_router.post("/clients")
async def create_client(body: ClientCreate, current_user: AuthedUser = Depends(get_current_user)):
    return await services.create_client(current_user, name=body.name, client_type=body.client_type, notes=body.notes, db=db)


@api_router.get("/clients/{client_id}")
async def get_client(client_id: str, current_user: AuthedUser = Depends(get_current_user)):
    return await services.get_client(current_user, client_id, db=db)


@api_router.delete("/clients/{client_id}")
async def delete_client(client_id: str, current_user: AuthedUser = Depends(get_current_user)):
    return await services.delete_client(current_user, client_id, db=db)


# ----------------------------- files -------------------------------
@api_router.get("/clients/{client_id}/files")
async def list_files(client_id: str, current_user: AuthedUser = Depends(get_current_user)):
    return await services.list_files(current_user, client_id, db=db)


MAX_FILES_PER_BATCH = 20
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50MB
MAX_BATCH_SIZE_BYTES = 100 * 1024 * 1024  # 100MB


def validate_file_content(filename: str, content: bytes) -> str:
    """Validate file extension, MIME magic signature, and reject executables/disguised binaries.

    Returns the normalized extension string on success, or raises HTTPException(400).
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if not ext or ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            400,
            f"Unsupported file extension: '.{ext}'. Supported extensions: {sorted(SUPPORTED_EXTENSIONS)}",
        )

    if not content:
        raise HTTPException(400, f"File '{filename}' is empty (0 bytes).")

    # 1. Reject binary executables disguised as documents
    # Windows PE / DOS MZ executable
    if content.startswith(b"MZ"):
        raise HTTPException(400, f"Security violation: File '{filename}' appears to be a Windows binary executable.")
    # Linux / Unix ELF
    if content.startswith(b"\x7fELF"):
        raise HTTPException(400, f"Security violation: File '{filename}' appears to be an ELF binary executable.")
    # Mach-O (macOS)
    if content.startswith((b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")):
        raise HTTPException(400, f"Security violation: File '{filename}' appears to be a Mach-O binary executable.")

    # 2. Content signature verification per extension
    if ext == "pdf":
        if not content.startswith(b"%PDF"):
            raise HTTPException(400, f"Content mismatch: File '{filename}' has a .pdf extension but lacks a valid PDF header.")
    elif ext == "png":
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(400, f"Content mismatch: File '{filename}' has a .png extension but lacks a valid PNG header.")
    elif ext in ("jpg", "jpeg"):
        if not content.startswith(b"\xff\xd8\xff"):
            raise HTTPException(400, f"Content mismatch: File '{filename}' has a .jpg/.jpeg extension but lacks a valid JPEG header.")
    elif ext in ("tiff", "tif"):
        if not (content.startswith(b"II*\x00") or content.startswith(b"MM\x00*")):
            raise HTTPException(400, f"Content mismatch: File '{filename}' has a .tiff extension but lacks a valid TIFF header.")
    elif ext in ("docx", "xlsx"):
        # Office Open XML files are ZIP archives starting with PK\x03\x04
        if not content.startswith(b"PK\x03\x04"):
            raise HTTPException(400, f"Content mismatch: File '{filename}' has a .{ext} extension but lacks a valid ZIP/Office header.")
    elif ext == "csv":
        # CSV must be plain text without null bytes or binary control characters
        sample = content[:4096]
        if b"\x00" in sample:
            raise HTTPException(400, f"Content mismatch: File '{filename}' has a .csv extension but contains binary null bytes.")
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError:
            try:
                sample.decode("latin-1")
            except Exception:
                raise HTTPException(400, f"Content mismatch: File '{filename}' is not valid text for a CSV document.")

    return ext


@api_router.post("/clients/{client_id}/files")
async def upload_files(
    client_id: str,
    files: List[UploadFile] = File(...),
    current_user: AuthedUser = Depends(get_current_user),
):
    if not file_upload_limiter.check_and_record(current_user.firm_id):
        raise HTTPException(429, "File upload rate limit exceeded. Please wait a moment.")

    if not files:
        raise HTTPException(400, "No files uploaded")
    if len(files) > MAX_FILES_PER_BATCH:
        raise HTTPException(400, f"Maximum {MAX_FILES_PER_BATCH} files allowed per upload batch.")

    total_batch_size = 0
    staged = []
    # Pass 1: read + validate EVERY file before persisting anything. This keeps the
    # batch atomic: a single invalid/oversized file aborts with a clear error and
    # leaves NO partially-uploaded files behind (which previously would duplicate on retry).
    for uf in files:
        fname = os.path.basename(uf.filename or "unnamed")

        chunks = []
        file_size = 0
        while True:
            chunk = await uf.read(1024 * 1024)
            if not chunk:
                break
            file_size += len(chunk)
            total_batch_size += len(chunk)
            if file_size > MAX_FILE_SIZE_BYTES:
                raise HTTPException(413, f"File '{fname}' exceeds maximum allowed size of 50MB.")
            if total_batch_size > MAX_BATCH_SIZE_BYTES:
                raise HTTPException(413, "Total upload batch exceeds maximum allowed size of 100MB.")
            chunks.append(chunk)

        content = b"".join(chunks)
        # Validate extension, magic bytes, and reject disguised executables BEFORE any storage write
        validate_file_content(fname, content)
        staged.append((fname, content))

    # Pass 2: all files validated OK -> now persist them.
    saved = []
    for fname, content in staged:
        doc = await services.save_client_file(
            current_user, client_id, fname, content, db=db
        )
        saved.append(doc)
    return {"uploaded": len(saved), "files": saved}



@api_router.delete("/clients/{client_id}/files/{file_id}")
async def delete_file(client_id: str, file_id: str, current_user: AuthedUser = Depends(get_current_user)):
    return await services.delete_file(current_user, client_id, file_id, db=db)


# -------------------------- checklists -----------------------------
@api_router.get("/checklist-templates")
async def list_templates(current_user: AuthedUser = Depends(get_current_user)):
    return await services.list_templates(current_user, db=db)


@api_router.post("/checklist-templates")
async def create_template(body: TemplateBody, current_user: AuthedUser = Depends(get_current_user)):
    return await services.create_template(current_user, body.model_dump(), db=db)


@api_router.put("/checklist-templates/{template_id}")
async def update_template(
    template_id: str,
    body: TemplateBody,
    current_user: AuthedUser = Depends(get_current_user),
):
    return await services.update_template(current_user, template_id, body.model_dump(), db=db)


@api_router.delete("/checklist-templates/{template_id}")
async def delete_template(template_id: str, current_user: AuthedUser = Depends(get_current_user)):
    return await services.delete_template(current_user, template_id, db=db)


# ------------------------------ scan -------------------------------
@api_router.post("/scans/{scan_id}/cancel")
async def cancel_scan(scan_id: str, current_user: AuthedUser = Depends(get_current_user)):
    return await services.cancel_scan(current_user, scan_id, db=db)


@api_router.post("/clients/{client_id}/scan")
async def start_scan(client_id: str, body: ScanBody, current_user: AuthedUser = Depends(get_current_user)):
    if not scan_start_limiter.check_and_record(current_user.firm_id):
        raise HTTPException(429, "Scan creation rate limit exceeded. Please wait a moment.")
    return await services.start_scan(current_user, client_id, body.template_id, body.expected_period, db=db)


@api_router.get("/clients/{client_id}/scans")
async def list_scans(client_id: str, current_user: AuthedUser = Depends(get_current_user)):
    return await services.list_scans(current_user, client_id, db=db)


@api_router.get("/scans/{scan_id}")
async def get_scan(scan_id: str, current_user: AuthedUser = Depends(get_current_user)):
    return await services.get_scan(current_user, scan_id, db=db)


@api_router.get("/scans/{scan_id}/findings")
async def get_findings(
    scan_id: str,
    category: Optional[str] = None,
    status: Optional[str] = None,
    current_user: AuthedUser = Depends(get_current_user),
):
    return await services.get_findings(current_user, scan_id, category=category, status=status, db=db)


@api_router.patch("/findings/{finding_id}")
async def update_finding(
    finding_id: str,
    body: FindingUpdate,
    current_user: AuthedUser = Depends(get_current_user),
):
    return await services.update_finding(current_user, finding_id, body.model_dump(), db=db)


# ----------------------------- reports -----------------------------
@api_router.get("/scans/{scan_id}/report")
async def export_report(
    scan_id: str,
    format: str = "csv",
    current_user: AuthedUser = Depends(get_current_user),
):
    report = await services.export_report(current_user, scan_id, format=format, db=db)
    return StreamingResponse(
        iter([report["data"]]),
        media_type=report["media_type"],
        headers={"Content-Disposition": f"attachment; filename={report['filename']}"},
    )


# ----------------------------- agent -------------------------------
@api_router.post("/agent/messages", status_code=202)
async def post_agent_message(
    body: AgentMessageRequest,
    current_user: AuthedUser = Depends(get_current_user),
    idempotency_key_header: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    effective_idempotency_key = body.idempotency_key or idempotency_key_header
    if effective_idempotency_key:
        clean_key = str(effective_idempotency_key).strip()
        if clean_key:
            existing_run = await db.agent_runs.find_one(
                scoped(db.agent_runs, current_user, {"idempotency_key": clean_key}),
                {"_id": 0},
            )
            if existing_run:
                return {
                    "run_id": existing_run["id"],
                    "thread_id": existing_run["thread_id"],
                    "status": existing_run.get("status", services.RUN_STATUS_QUEUED),
                    "created_at": existing_run.get("created_at"),
                }

    if not agent_msg_limiter.check_and_record(current_user.firm_id):
        raise HTTPException(429, "Agent message rate limit exceeded. Please wait a moment.")

    # Check per-firm AI quota limit if configured
    firm = await db.firm.find_one({"id": current_user.firm_id})
    if firm:
        settings = firm.get("settings") or {}
        quota = settings.get("ai_quota_limit")
        usage = settings.get("ai_usage_count", 0)
        if quota is not None and usage >= quota:
            raise HTTPException(429, "Firm AI usage quota exceeded. Contact firm administrator.")
        await db.firm.update_one({"id": current_user.firm_id}, {"$inc": {"settings.ai_usage_count": 1}})

    return await services.post_agent_message(
        current_user,
        text=body.text,
        thread_id=body.thread_id,
        idempotency_key=effective_idempotency_key,
        db=db,
    )


@api_router.get("/agent/runs/{run_id}")
async def get_agent_run(
    run_id: str,
    current_user: AuthedUser = Depends(get_current_user),
):
    clean_id = (run_id or "").strip()
    if not clean_id:
        raise HTTPException(400, "run_id is required")
    return await services.get_agent_run(current_user, clean_id, db=db)


@api_router.get("/agent/runs/{run_id}/steps")
async def list_agent_run_steps(
    run_id: str,
    current_user: AuthedUser = Depends(get_current_user),
):
    steps = await services.list_agent_run_steps(current_user, run_id, db=db)
    # Hide internal thoughts and ensure safe exposure
    safe_steps = []
    for s in steps:
        stype = s.get("step_type")
        if stype == "thought":
            continue
        safe_steps.append(s)
    return safe_steps


@api_router.get("/agent/threads/{thread_id}/messages")
async def list_agent_messages(
    thread_id: str,
    current_user: AuthedUser = Depends(get_current_user),
):
    return await services.list_agent_messages(current_user, thread_id, db=db)


@api_router.post("/agent/runs/{run_id}/cancel")
async def cancel_agent_run(
    run_id: str,
    current_user: AuthedUser = Depends(get_current_user),
):
    return await services.cancel_agent_run(current_user, run_id, db=db)


@api_router.get("/agent/approvals")
async def list_agent_approvals(
    run_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    status: Optional[str] = None,
    current_user: AuthedUser = Depends(get_current_user),
):
    return await services.list_agent_approvals(
        current_user,
        run_id=run_id,
        thread_id=thread_id,
        status=status,
        db=db,
    )


@api_router.get("/agent/approvals/{approval_id}")
async def get_agent_approval(
    approval_id: str,
    current_user: AuthedUser = Depends(get_current_user),
):
    return await services.get_agent_approval(current_user, approval_id, db=db)


@api_router.post("/agent/approvals/{approval_id}/approve", status_code=202)
async def approve_agent_approval(
    approval_id: str,
    current_user: AuthedUser = Depends(get_current_user),
):
    from agent.loop import resume_agent_run_background
    updated = await services.approve_agent_approval(current_user, approval_id, db=db)
    resume_agent_run_background(current_user, approval_id, is_approved=True, db=db)
    return updated


@api_router.post("/agent/approvals/{approval_id}/reject", status_code=202)
async def reject_agent_approval(
    approval_id: str,
    body: Optional[ApprovalRejectRequest] = None,
    current_user: AuthedUser = Depends(get_current_user),
):
    from agent.loop import resume_agent_run_background
    reason = body.reason if body else None
    updated = await services.reject_agent_approval(current_user, approval_id, reason=reason, db=db)
    resume_agent_run_background(current_user, approval_id, is_approved=False, reason=reason, db=db)
    return updated


# Public Root & Health Endpoints
@app.get("/")
async def app_root():
    return {"message": "LedgerLens Cloud Accounting AI API"}


@app.get("/healthz")
async def health_check():
    """Safe liveness and readiness probe for cloud orchestrators (Kubernetes, Render, Fly.io).

    Does NOT expose database credentials, secrets, or internal server paths.
    """
    db_healthy = False
    if db is not None:
        try:
            if hasattr(db, "count_documents"):
                # Ping database collection without querying confidential data
                await db.firm.count_documents({})
                db_healthy = True
            elif hasattr(db, "users"):
                await db.users.count_documents({})
                db_healthy = True
        except Exception:
            db_healthy = False

    return {
        "status": "healthy" if db_healthy else "degraded",
        "service": "ledgerlens",
        "database": "connected" if db_healthy else "unreachable",
    }



# Mount auth routes (public /api/auth/*) and protected routes (/api/*)
app.include_router(auth_router)
app.include_router(api_router)

# Configure CORS with strict explicit origins
if config_error_cors is None and "CORS_ALLOWED_ORIGINS" in locals():
    app.add_middleware(
        CORSMiddleware,
        allow_credentials=True,
        allow_origins=CORS_ALLOWED_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.on_event("startup")
async def seed_defaults():
    if config_error:
        raise RuntimeError(f"Startup configuration error: {config_error}")
    if db is None:
        raise RuntimeError("Database instance is not initialized.")
    if hasattr(db, "init"):
        await db.init()
    try:
        storage.init_storage()
        logger.info("Object storage initialized")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Storage init deferred/unavailable: {e}")

    # Backfill legacy ownerless records safely into designated default firm
    default_firm_id = "firm"
    if hasattr(db, "backfill_legacy_firm"):
        await db.backfill_legacy_firm(default_firm_id)
        logger.info(f"Backfilled any legacy ownerless records to firm '{default_firm_id}'")

    # Ensure default checklist templates are seeded for default firm if none exist
    if await db.templates.count_documents({"firm_id": default_firm_id}) == 0:
        for tpl in default_templates():
            await db.templates.insert_one({
                "id": new_id(),
                "firm_id": default_firm_id,
                "created_at": now_iso(),
                **tpl,
            })
        logger.info("Seeded default checklist templates for default firm")

    # Reconcile orphaned runs and scans from prior server restarts/deployments.
    # LEASE-AWARE durable recovery: only runs whose worker lease has EXPIRED are
    # requeued for retry (or failed once max attempts are exhausted). A live lease held
    # by another running worker is never clobbered, so this is safe under concurrency and
    # after a rolling restart.
    try:
        recovery = await services.recover_stale_agent_runs(db=db)
        if recovery.get("requeued") or recovery.get("failed"):
            logger.info("Durable agent run recovery on startup: %s", recovery)
    except Exception as e:
        logger.warning("Durable agent run recovery skipped: %s", e)

    try:
        from datetime import timedelta
        boot_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()

        all_active_scans = await db.scans.find(
            {"status": {"$in": ["queued", "scanning", "cancelling"]}}
        ).to_list(10000)
        orphaned_scans = [s for s in all_active_scans if (s.get("started_at") or s.get("created_at") or "") < boot_cutoff]
        for s in orphaned_scans:
            await db.scans.update_one(
                {"id": s["id"]},
                {"$set": {
                    "status": "error",
                    "error": "Scan interrupted by server restart",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }},
            )
        if orphaned_scans:
            logger.info("Reconciled %d orphaned scan(s) as error", len(orphaned_scans))
    except Exception as e:
        logger.warning("Orphaned scan reconciliation skipped: %s", e)

    # Start the durable agent background worker (opt-in; enabled for PostgreSQL backend).
    try:
        from agent.worker import start_worker_if_enabled
        started = await start_worker_if_enabled(db=db)
        if started:
            logger.info("Durable agent worker started.")
    except Exception as e:
        logger.warning("Durable agent worker failed to start: %s", e)



@app.on_event("shutdown")
async def shutdown_db_client():
    try:
        from agent.worker import stop_worker
        await stop_worker()
    except Exception as e:
        logger.warning("Agent worker shutdown error: %s", e)
    if db is not None and hasattr(db, "close"):
        res = db.close()
        if asyncio.iscoroutine(res):
            await res


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8001"))
    host = os.environ.get("HOST", "127.0.0.1")
    logger.info(f"Starting LedgerLens backend on {host}:{port} (DATA_BACKEND={DATA_BACKEND})")
    uvicorn.run(app, host=host, port=port, log_level="info")
