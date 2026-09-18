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

CANCEL_REQUESTS: set = set()


# ----------------------------- helpers -----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def clean(doc: dict) -> dict:
    doc.pop("_id", None)
    return doc


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


# ----------------------------- firm --------------------------------
@api_router.get("/firm")
async def get_firm(current_user: AuthedUser = Depends(get_current_user)):
    doc = await db.firm.find_one({"id": current_user.firm_id})
    if not doc:
        doc = {
            "id": current_user.firm_id,
            "name": "My Firm",
            "contact_email": current_user.email,
            "retention_note": "Documents are securely processed in isolated cloud environments.",
            "settings": {"privacy_mode": True},
            "created_at": now_iso(),
        }
        await db.firm.insert_one(dict(doc))
    return clean(doc)


@api_router.put("/firm")
async def update_firm(body: FirmUpdate, current_user: AuthedUser = Depends(get_current_user)):
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    update["updated_at"] = now_iso()
    await db.firm.update_one({"id": current_user.firm_id}, {"$set": update}, upsert=True)
    doc = await db.firm.find_one({"id": current_user.firm_id})
    return clean(doc)


# ---------------------------- clients ------------------------------
@api_router.get("/clients")
async def list_clients(current_user: AuthedUser = Depends(get_current_user)):
    filt = scoped(db.clients, current_user, {})
    clients = await db.clients.find(filt, {"_id": 0}).sort("created_at", -1).to_list(1000)
    for c in clients:
        c_filt = scoped(db.files, current_user, {"client_id": c["id"], "is_deleted": {"$ne": True}})
        c["file_count"] = await db.files.count_documents(c_filt)
        last_scan_filt = scoped(db.scans, current_user, {"client_id": c["id"]})
        last = await db.scans.find_one(last_scan_filt, {"_id": 0}, sort=[("started_at", -1)])
        c["last_scan"] = last
    return clients


@api_router.post("/clients")
async def create_client(body: ClientCreate, current_user: AuthedUser = Depends(get_current_user)):
    doc = {
        "id": new_id(),
        "firm_id": current_user.firm_id,
        "name": body.name,
        "client_type": body.client_type,
        "notes": body.notes or "",
        "created_at": now_iso(),
    }
    await db.clients.insert_one(dict(doc))
    return clean(doc)


@api_router.get("/clients/{client_id}")
async def get_client(client_id: str, current_user: AuthedUser = Depends(get_current_user)):
    filt = scoped(db.clients, current_user, {"id": client_id})
    doc = await db.clients.find_one(filt, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Client not found")
    return doc


@api_router.delete("/clients/{client_id}")
async def delete_client(client_id: str, current_user: AuthedUser = Depends(get_current_user)):
    client = await db.clients.find_one(scoped(db.clients, current_user, {"id": client_id}))
    if not client:
        raise HTTPException(404, "Client not found")
    await db.delete_client_cascade(client_id, firm_id=current_user.firm_id)
    return {"ok": True}


# ----------------------------- files -------------------------------
@api_router.get("/clients/{client_id}/files")
async def list_files(client_id: str, current_user: AuthedUser = Depends(get_current_user)):
    client = await db.clients.find_one(scoped(db.clients, current_user, {"id": client_id}))
    if not client:
        raise HTTPException(404, "Client not found")
    filt = scoped(db.files, current_user, {"client_id": client_id, "is_deleted": {"$ne": True}})
    files = await db.files.find(filt, {"_id": 0, "storage_path": 0}).sort("uploaded_at", -1).to_list(100000)
    return files


@api_router.post("/clients/{client_id}/files")
async def upload_files(
    client_id: str,
    files: List[UploadFile] = File(...),
    current_user: AuthedUser = Depends(get_current_user),
):
    client = await db.clients.find_one(scoped(db.clients, current_user, {"id": client_id}))
    if not client:
        raise HTTPException(404, "Client not found")
    saved = []
    for uf in files:
        fid = new_id()
        original = os.path.basename(uf.filename or "unnamed")
        ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        content = await uf.read()
        # Tenant partitioned storage key
        object_path = f"{storage.APP_NAME}/uploads/{current_user.firm_id}/{client_id}/{fid}.{ext or 'bin'}"
        result = storage.put_object(object_path, content, storage.mime_for(ext))
        doc = {
            "id": fid,
            "firm_id": current_user.firm_id,
            "client_id": client_id,
            "name": original,
            "ext": ext,
            "size": result.get("size", len(content)),
            "supported": ext in SUPPORTED_EXTENSIONS,
            "storage_path": result["path"],
            "is_deleted": False,
            "uploaded_at": now_iso(),
        }
        await db.files.insert_one(dict(doc))
        doc.pop("storage_path", None)
        saved.append(clean(doc))
    return {"uploaded": len(saved), "files": saved}


@api_router.delete("/clients/{client_id}/files/{file_id}")
async def delete_file(client_id: str, file_id: str, current_user: AuthedUser = Depends(get_current_user)):
    filt = scoped(db.files, current_user, {"id": file_id, "client_id": client_id})
    file_doc = await db.files.find_one(filt)
    if not file_doc:
        raise HTTPException(404, "File not found")
    sp = file_doc.get("storage_path")
    if sp:
        storage.delete_object(sp)
    await db.files.delete_one(filt)
    return {"ok": True}


# -------------------------- checklists -----------------------------
@api_router.get("/checklist-templates")
async def list_templates(current_user: AuthedUser = Depends(get_current_user)):
    filt = scoped(db.templates, current_user, {})
    return await db.templates.find(filt, {"_id": 0}).sort("name", 1).to_list(1000)


@api_router.post("/checklist-templates")
async def create_template(body: TemplateBody, current_user: AuthedUser = Depends(get_current_user)):
    doc = {
        "id": new_id(),
        "firm_id": current_user.firm_id,
        "created_at": now_iso(),
        **body.model_dump(),
    }
    await db.templates.insert_one(dict(doc))
    return clean(doc)


@api_router.put("/checklist-templates/{template_id}")
async def update_template(
    template_id: str,
    body: TemplateBody,
    current_user: AuthedUser = Depends(get_current_user),
):
    filt = scoped(db.templates, current_user, {"id": template_id})
    existing = await db.templates.find_one(filt)
    if not existing:
        raise HTTPException(404, "Template not found")
    await db.templates.update_one(filt, {"$set": body.model_dump()})
    return await db.templates.find_one(filt, {"_id": 0})


@api_router.delete("/checklist-templates/{template_id}")
async def delete_template(template_id: str, current_user: AuthedUser = Depends(get_current_user)):
    filt = scoped(db.templates, current_user, {"id": template_id})
    existing = await db.templates.find_one(filt)
    if not existing:
        raise HTTPException(404, "Template not found")
    await db.templates.delete_one(filt)
    return {"ok": True}


# ------------------------------ scan -------------------------------
def _make_progress(scan_id: str, firm_id: str, loop):
    """Build a thread-safe on_progress callback that streams progress to the DB."""
    state = {"skipped": []}

    async def _push(processed, pct, skipped):
        await db.scans.update_one(
            {"id": scan_id, "firm_id": firm_id},
            {"$set": {"processed_files": processed, "progress": pct, "skipped_files": skipped}},
        )

    def on_progress(processed, tot, skip):
        if skip:
            state["skipped"].append(skip)
        pct = int((processed / tot) * 100) if tot else 100
        asyncio.run_coroutine_threadsafe(_push(processed, pct, list(state["skipped"])), loop)

    return on_progress


async def _finalize_scan(scan_id: str, firm_id: str, client_id: str, result: dict):
    """Persist findings + mark the scan cancelled/completed. Shared by both runners."""
    file_states = result.get("file_states") or {}
    if result.get("cancelled"):
        CANCEL_REQUESTS.discard(scan_id)
        await db.scans.update_one(
            {"id": scan_id, "firm_id": firm_id},
            {"$set": {
                "status": "cancelled",
                "processed_files": result["processed"],
                "total_files": result["total"],
                "counts": result["counts"],
                "skipped_files": result["skipped"],
                "file_states": file_states,
                "completed_at": now_iso(),
            }},
        )
        return

    finding_docs = []
    for fnd in result["findings"]:
        finding_docs.append({
            "id": new_id(),
            "firm_id": firm_id,
            "scan_id": scan_id,
            "client_id": client_id,
            "status": "unreviewed",
            "note": "",
            "created_at": now_iso(),
            **fnd,
        })
    scan_update = {
        "status": "completed",
        "progress": 100,
        "processed_files": result["processed"],
        "total_files": result["total"],
        "counts": result["counts"],
        "skipped_files": result["skipped"],
        "file_states": file_states,
        "total_findings": len(finding_docs),
        "completed_at": now_iso(),
    }
    if hasattr(db, "finalize_scan_atomic"):
        await db.finalize_scan_atomic(scan_id, scan_update, finding_docs)
    else:
        if finding_docs:
            await db.findings.insert_many([dict(d) for d in finding_docs])
        await db.scans.update_one({"id": scan_id, "firm_id": firm_id}, {"$set": scan_update})


async def _load_items(template_id: Optional[str], firm_id: str):
    if not template_id:
        return []
    template = await db.templates.find_one({"id": template_id, "firm_id": firm_id}, {"_id": 0})
    return template["items"] if template else []


async def _run_scan(scan_id: str, firm_id: str, client_id: str, template_id: Optional[str], expected_period: Optional[int]):
    """Web build: files come from object storage, downloaded to a temp dir."""
    tmpdir = tempfile.mkdtemp(prefix="scan_")
    try:
        file_docs = await db.files.find(
            {"client_id": client_id, "firm_id": firm_id, "is_deleted": {"$ne": True}}
        ).to_list(100000)
        records = []
        for f in file_docs:
            local_path = os.path.join(tmpdir, f"{f['id']}.{f.get('ext') or 'bin'}")
            try:
                data = await asyncio.to_thread(storage.get_object, f["storage_path"])
                with open(local_path, "wb") as out:
                    out.write(data)
            except Exception:  # noqa: BLE001
                pass
            records.append({"id": f["id"], "name": f["name"], "ext": f["ext"], "size": f["size"], "path": local_path})

        items = await _load_items(template_id, firm_id)
        await db.scans.update_one(
            {"id": scan_id, "firm_id": firm_id},
            {"$set": {"status": "scanning", "total_files": len(records), "processed_files": 0, "progress": 0}},
        )
        on_progress = _make_progress(scan_id, firm_id, asyncio.get_event_loop())
        result = await asyncio.to_thread(
            run_detection, records, items, expected_period, on_progress,
            lambda: scan_id in CANCEL_REQUESTS,
        )
        await _finalize_scan(scan_id, firm_id, client_id, result)
    except Exception as e:  # noqa: BLE001
        logger.exception("scan failed")
        await db.scans.update_one({"id": scan_id, "firm_id": firm_id}, {"$set": {"status": "error", "error": str(e)}})
    finally:
        CANCEL_REQUESTS.discard(scan_id)
        import shutil as _shutil
        _shutil.rmtree(tmpdir, ignore_errors=True)


@api_router.post("/scans/{scan_id}/cancel")
async def cancel_scan(scan_id: str, current_user: AuthedUser = Depends(get_current_user)):
    scan = await db.scans.find_one(scoped(db.scans, current_user, {"id": scan_id}), {"_id": 0})
    if not scan:
        raise HTTPException(404, "Scan not found")
    if scan["status"] in ("queued", "scanning"):
        CANCEL_REQUESTS.add(scan_id)
        await db.scans.update_one(scoped(db.scans, current_user, {"id": scan_id}), {"$set": {"status": "cancelling"}})
        return {"ok": True, "status": "cancelling"}
    return {"ok": False, "status": scan["status"]}


@api_router.post("/clients/{client_id}/scan")
async def start_scan(client_id: str, body: ScanBody, current_user: AuthedUser = Depends(get_current_user)):
    c = await db.clients.find_one(scoped(db.clients, current_user, {"id": client_id}))
    if not c:
        raise HTTPException(404, "Client not found")
    if body.template_id:
        tpl = await db.templates.find_one(scoped(db.templates, current_user, {"id": body.template_id}))
        if not tpl:
            raise HTTPException(404, "Checklist template not found")
    scan = {
        "id": new_id(),
        "firm_id": current_user.firm_id,
        "client_id": client_id,
        "client_name": c["name"],
        "template_id": body.template_id,
        "expected_period": body.expected_period,
        "status": "queued",
        "progress": 0,
        "total_files": 0,
        "processed_files": 0,
        "skipped_files": [],
        "counts": {},
        "total_findings": 0,
        "started_at": now_iso(),
    }
    await db.scans.insert_one(dict(scan))
    asyncio.create_task(_run_scan(scan["id"], current_user.firm_id, client_id, body.template_id, body.expected_period))
    return clean(scan)


@api_router.get("/clients/{client_id}/scans")
async def list_scans(client_id: str, current_user: AuthedUser = Depends(get_current_user)):
    client = await db.clients.find_one(scoped(db.clients, current_user, {"id": client_id}))
    if not client:
        raise HTTPException(404, "Client not found")
    filt = scoped(db.scans, current_user, {"client_id": client_id})
    return await db.scans.find(filt, {"_id": 0}).sort("started_at", -1).to_list(1000)


@api_router.get("/scans/{scan_id}")
async def get_scan(scan_id: str, current_user: AuthedUser = Depends(get_current_user)):
    doc = await db.scans.find_one(scoped(db.scans, current_user, {"id": scan_id}), {"_id": 0})
    if not doc:
        raise HTTPException(404, "Scan not found")
    return doc


@api_router.get("/scans/{scan_id}/findings")
async def get_findings(
    scan_id: str,
    category: Optional[str] = None,
    status: Optional[str] = None,
    current_user: AuthedUser = Depends(get_current_user),
):
    scan = await db.scans.find_one(scoped(db.scans, current_user, {"id": scan_id}))
    if not scan:
        raise HTTPException(404, "Scan not found")
    q = scoped(db.findings, current_user, {"scan_id": scan_id})
    if category:
        q["category"] = category
    if status:
        q["status"] = status
    return await db.findings.find(q, {"_id": 0}).sort("confidence", -1).to_list(100000)


@api_router.patch("/findings/{finding_id}")
async def update_finding(
    finding_id: str,
    body: FindingUpdate,
    current_user: AuthedUser = Depends(get_current_user),
):
    filt = scoped(db.findings, current_user, {"id": finding_id})
    if not await db.findings.find_one(filt):
        raise HTTPException(404, "Finding not found")
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    update["updated_at"] = now_iso()
    await db.findings.update_one(filt, {"$set": update})
    return await db.findings.find_one(filt, {"_id": 0})


# ----------------------------- reports -----------------------------
@api_router.get("/scans/{scan_id}/report")
async def export_report(
    scan_id: str,
    format: str = "csv",
    current_user: AuthedUser = Depends(get_current_user),
):
    scan = await db.scans.find_one(scoped(db.scans, current_user, {"id": scan_id}), {"_id": 0})
    if not scan:
        raise HTTPException(404, "Scan not found")
    findings = await db.findings.find(
        scoped(db.findings, current_user, {"scan_id": scan_id}), {"_id": 0}
    ).sort("category", 1).to_list(100000)

    fmt = format.lower()
    if fmt == "csv":
        data = report_engine.to_csv(findings)
        media = "text/csv"
        ext = "csv"
    elif fmt in ("xlsx", "excel"):
        data = report_engine.to_xlsx(findings)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ext = "xlsx"
    elif fmt == "pdf":
        data = report_engine.to_pdf(findings, {
            "client_name": scan.get("client_name", "-"),
            "expected_period": scan.get("expected_period", "-"),
        })
        media = "application/pdf"
        ext = "pdf"
    else:
        raise HTTPException(400, "Unsupported format")

    fname = f"review_report_{scan.get('client_name', 'client')}.{ext}".replace(" ", "_")
    return StreamingResponse(
        iter([data]),
        media_type=media,
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


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
