import asyncio
import logging
import os
import uuid
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, APIRouter, UploadFile, File, HTTPException, Request
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

# Production Data Backend: PostgreSQL (configurable via DATABASE_URL)
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ledgerlens")
DATA_BACKEND = os.environ.get("DATA_BACKEND", "postgres").lower()

if DATA_BACKEND == "mongo":
    from motor.motor_asyncio import AsyncIOMotorClient
    mongo_url = os.environ.get("MONGO_URL", "mongodb://127.0.0.1:27017")
    client = AsyncIOMotorClient(mongo_url)
    db = client[os.environ.get("DB_NAME", "ledgerlens")]
else:
    # PostgreSQL / Cloud Database layer
    from database import get_database
    client = None
    db = get_database(DATABASE_URL)

app = FastAPI(title="LedgerLens Cloud Accounting AI API")
api_router = APIRouter(prefix="/api")



logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# In-memory set of scan ids the user asked to cancel (single-process app).
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
async def get_firm():
    doc = await db.firm.find_one({"id": "firm"})
    if not doc:
        doc = {
            "id": "firm",
            "name": "",
            "contact_email": "",
            "retention_note": "Documents are securely processed in isolated cloud environments.",
            "settings": {"privacy_mode": True},
            "created_at": now_iso(),
        }
        await db.firm.insert_one(dict(doc))
    return clean(doc)


@api_router.put("/firm")
async def update_firm(body: FirmUpdate):
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    update["updated_at"] = now_iso()
    await db.firm.update_one({"id": "firm"}, {"$set": update}, upsert=True)
    doc = await db.firm.find_one({"id": "firm"})
    return clean(doc)


# ---------------------------- clients ------------------------------
@api_router.get("/clients")
async def list_clients():
    clients = await db.clients.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    for c in clients:
        c["file_count"] = await db.files.count_documents({"client_id": c["id"]})
        last = await db.scans.find_one({"client_id": c["id"]}, {"_id": 0}, sort=[("started_at", -1)])
        c["last_scan"] = last
    return clients


@api_router.post("/clients")
async def create_client(body: ClientCreate):
    doc = {
        "id": new_id(),
        "name": body.name,
        "client_type": body.client_type,
        "notes": body.notes or "",
        "created_at": now_iso(),
    }
    await db.clients.insert_one(dict(doc))
    return clean(doc)


@api_router.get("/clients/{client_id}")
async def get_client(client_id: str):
    doc = await db.clients.find_one({"id": client_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Client not found")
    return doc


@api_router.delete("/clients/{client_id}")
async def delete_client(client_id: str):
    await db.clients.delete_one({"id": client_id})
    await db.files.delete_many({"client_id": client_id})
    scans = await db.scans.find({"client_id": client_id}, {"id": 1}).to_list(1000)
    for s in scans:
        await db.findings.delete_many({"scan_id": s["id"]})
    await db.scans.delete_many({"client_id": client_id})
    return {"ok": True}


# ----------------------------- files -------------------------------
@api_router.get("/clients/{client_id}/files")
async def list_files(client_id: str):
    files = await db.files.find(
        {"client_id": client_id, "is_deleted": {"$ne": True}},
        {"_id": 0, "storage_path": 0},
    ).sort("uploaded_at", -1).to_list(100000)
    return files


@api_router.post("/clients/{client_id}/files")
async def upload_files(client_id: str, files: List[UploadFile] = File(...)):
    if not await db.clients.find_one({"id": client_id}):
        raise HTTPException(404, "Client not found")
    saved = []
    for uf in files:
        fid = new_id()
        original = os.path.basename(uf.filename or "unnamed")
        ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        content = await uf.read()
        object_path = f"{storage.APP_NAME}/uploads/{client_id}/{fid}.{ext or 'bin'}"
        result = storage.put_object(object_path, content, storage.mime_for(ext))
        doc = {
            "id": fid,
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
async def delete_file(client_id: str, file_id: str):
    await db.files.update_one({"id": file_id}, {"$set": {"is_deleted": True}})
    return {"ok": True}


# -------------------------- checklists -----------------------------
@api_router.get("/checklist-templates")
async def list_templates():
    return await db.templates.find({}, {"_id": 0}).sort("name", 1).to_list(1000)


@api_router.post("/checklist-templates")
async def create_template(body: TemplateBody):
    doc = {"id": new_id(), "created_at": now_iso(), **body.model_dump()}
    await db.templates.insert_one(dict(doc))
    return clean(doc)


@api_router.put("/checklist-templates/{template_id}")
async def update_template(template_id: str, body: TemplateBody):
    await db.templates.update_one({"id": template_id}, {"$set": body.model_dump()})
    return await db.templates.find_one({"id": template_id}, {"_id": 0})


@api_router.delete("/checklist-templates/{template_id}")
async def delete_template(template_id: str):
    await db.templates.delete_one({"id": template_id})
    return {"ok": True}


# ------------------------------ scan -------------------------------
def _make_progress(scan_id: str, loop):
    """Build a thread-safe on_progress callback that streams progress to the DB."""
    state = {"skipped": []}

    async def _push(processed, pct, skipped):
        await db.scans.update_one(
            {"id": scan_id},
            {"$set": {"processed_files": processed, "progress": pct, "skipped_files": skipped}},
        )

    def on_progress(processed, tot, skip):
        if skip:
            state["skipped"].append(skip)
        pct = int((processed / tot) * 100) if tot else 100
        asyncio.run_coroutine_threadsafe(_push(processed, pct, list(state["skipped"])), loop)

    return on_progress


async def _finalize_scan(scan_id: str, client_id: str, result: dict):
    """Persist findings + mark the scan cancelled/completed. Shared by both runners."""
    file_states = result.get("file_states") or {}
    if result.get("cancelled"):
        CANCEL_REQUESTS.discard(scan_id)
        await db.scans.update_one(
            {"id": scan_id},
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
            "id": new_id(), "scan_id": scan_id, "client_id": client_id,
            "status": "unreviewed", "note": "", "created_at": now_iso(), **fnd,
        })
    if finding_docs:
        await db.findings.insert_many([dict(d) for d in finding_docs])

    await db.scans.update_one(
        {"id": scan_id},
        {"$set": {
            "status": "completed", "progress": 100,
            "processed_files": result["processed"],
            "total_files": result["total"],
            "counts": result["counts"],
            "skipped_files": result["skipped"],
            "file_states": file_states,
            "total_findings": len(finding_docs),
            "completed_at": now_iso(),
        }},
    )


async def _load_items(template_id: Optional[str]):
    if not template_id:
        return []
    template = await db.templates.find_one({"id": template_id}, {"_id": 0})
    return template["items"] if template else []


async def _run_scan(scan_id: str, client_id: str, template_id: Optional[str], expected_period: Optional[int]):
    """Web build: files come from object storage, downloaded to a temp dir."""
    tmpdir = tempfile.mkdtemp(prefix="scan_")
    try:
        file_docs = await db.files.find({"client_id": client_id, "is_deleted": {"$ne": True}}).to_list(100000)
        records = []
        for f in file_docs:
            local_path = os.path.join(tmpdir, f"{f['id']}.{f.get('ext') or 'bin'}")
            try:
                data = await asyncio.to_thread(storage.get_object, f["storage_path"])
                with open(local_path, "wb") as out:
                    out.write(data)
            except Exception:  # noqa: BLE001 - treated as inaccessible, engine will skip
                pass
            records.append({"id": f["id"], "name": f["name"], "ext": f["ext"], "size": f["size"], "path": local_path})

        items = await _load_items(template_id)
        await db.scans.update_one(
            {"id": scan_id},
            {"$set": {"status": "scanning", "total_files": len(records), "processed_files": 0, "progress": 0}},
        )
        on_progress = _make_progress(scan_id, asyncio.get_event_loop())
        result = await asyncio.to_thread(
            run_detection, records, items, expected_period, on_progress,
            lambda: scan_id in CANCEL_REQUESTS,
        )
        await _finalize_scan(scan_id, client_id, result)
    except Exception as e:  # noqa: BLE001
        logger.exception("scan failed")
        await db.scans.update_one({"id": scan_id}, {"$set": {"status": "error", "error": str(e)}})
    finally:
        CANCEL_REQUESTS.discard(scan_id)
        import shutil as _shutil
        _shutil.rmtree(tmpdir, ignore_errors=True)


@api_router.post("/scans/{scan_id}/cancel")
async def cancel_scan(scan_id: str):
    scan = await db.scans.find_one({"id": scan_id}, {"_id": 0})
    if not scan:
        raise HTTPException(404, "Scan not found")
    if scan["status"] in ("queued", "scanning"):
        CANCEL_REQUESTS.add(scan_id)
        await db.scans.update_one({"id": scan_id}, {"$set": {"status": "cancelling"}})
        return {"ok": True, "status": "cancelling"}
    return {"ok": False, "status": scan["status"]}


@api_router.post("/clients/{client_id}/scan")
async def start_scan(client_id: str, body: ScanBody):
    c = await db.clients.find_one({"id": client_id})
    if not c:
        raise HTTPException(404, "Client not found")
    scan = {
        "id": new_id(),
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
    asyncio.create_task(_run_scan(scan["id"], client_id, body.template_id, body.expected_period))
    return clean(scan)


@api_router.get("/clients/{client_id}/scans")
async def list_scans(client_id: str):
    return await db.scans.find({"client_id": client_id}, {"_id": 0}).sort("started_at", -1).to_list(1000)


@api_router.get("/scans/{scan_id}")
async def get_scan(scan_id: str):
    doc = await db.scans.find_one({"id": scan_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Scan not found")
    return doc


@api_router.get("/scans/{scan_id}/findings")
async def get_findings(scan_id: str, category: Optional[str] = None, status: Optional[str] = None):
    q = {"scan_id": scan_id}
    if category:
        q["category"] = category
    if status:
        q["status"] = status
    return await db.findings.find(q, {"_id": 0}).sort("confidence", -1).to_list(100000)


@api_router.patch("/findings/{finding_id}")
async def update_finding(finding_id: str, body: FindingUpdate):
    if not await db.findings.find_one({"id": finding_id}):
        raise HTTPException(404, "Finding not found")
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    update["updated_at"] = now_iso()
    await db.findings.update_one({"id": finding_id}, {"$set": update})
    return await db.findings.find_one({"id": finding_id}, {"_id": 0})


# ----------------------------- reports -----------------------------
@api_router.get("/scans/{scan_id}/report")
async def export_report(scan_id: str, format: str = "csv"):
    scan = await db.scans.find_one({"id": scan_id}, {"_id": 0})
    if not scan:
        raise HTTPException(404, "Scan not found")
    findings = await db.findings.find({"scan_id": scan_id}, {"_id": 0}).sort("category", 1).to_list(100000)

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


@app.get("/")
async def app_root():
    return {"message": "LedgerLens Cloud Accounting AI API"}


@api_router.get("/")
async def root():
    return {"message": "LedgerLens Cloud Accounting AI API"}



app.include_router(api_router)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def seed_defaults():
    if hasattr(db, "init_pg"):
        await db.init_pg()
    try:
        storage.init_storage()
        logger.info("Object storage initialized")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Storage init deferred/unavailable: {e}")
    if await db.templates.count_documents({}) == 0:
        for tpl in default_templates():
            await db.templates.insert_one({"id": new_id(), "created_at": now_iso(), **tpl})
        logger.info("Seeded default checklist templates")



@app.on_event("shutdown")
async def shutdown_db_client():
    if client is not None:
        client.close()
    elif hasattr(db, "close"):
        db.close()


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8001"))
    host = os.environ.get("HOST", "127.0.0.1")
    logger.info(f"Starting LedgerLens backend on {host}:{port} (DATA_BACKEND={DATA_BACKEND})")
    uvicorn.run(app, host=host, port=port, log_level="info")
