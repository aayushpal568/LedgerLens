import asyncio
import logging
import os
import uuid
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, APIRouter, UploadFile, File, HTTPException, Request, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ConfigDict
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
AUTH_SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", "").strip()
if not AUTH_SECRET_KEY:
    config_error_auth = "AUTH_SECRET_KEY environment variable is required but missing or empty."
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


@api_router.post("/clients/{client_id}/files")
async def upload_files(
    client_id: str,
    files: List[UploadFile] = File(...),
    current_user: AuthedUser = Depends(get_current_user),
):
    saved = []
    for uf in files:
        content = await uf.read()
        doc = await services.save_client_file(
            current_user, client_id, uf.filename or "unnamed", content, db=db
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
):
    return await services.post_agent_message(
        current_user,
        text=body.text,
        thread_id=body.thread_id,
        db=db,
    )


@api_router.get("/agent/runs/{run_id}")
async def get_agent_run(
    run_id: str,
    current_user: AuthedUser = Depends(get_current_user),
):
    return await services.get_agent_run(current_user, run_id, db=db)


@api_router.post("/agent/runs/{run_id}/cancel")
async def cancel_agent_run(
    run_id: str,
    current_user: AuthedUser = Depends(get_current_user),
):
    return await services.cancel_agent_run(current_user, run_id, db=db)


# Public Root Endpoint
@app.get("/")
async def app_root():
    return {"message": "LedgerLens Cloud Accounting AI API"}


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


@app.on_event("shutdown")
async def shutdown_db_client():
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
