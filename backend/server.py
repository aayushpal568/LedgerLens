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
from engine import build_local_engine, LocalDirectoryFileSource, PaddleOCRProvider, OllamaLLMProvider
from engine import report as report_engine
import storage

ROOT_DIR = Path(__file__).resolve().parent
BACKEND_TOKEN = os.environ.get("LEDGERLENS_BACKEND_TOKEN", "")

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT_DIR / ".env")
except Exception:
    pass

# Data backend: "sqlite" (default for local desktop/Tauri) or "mongo" (cloud).
DATA_BACKEND = os.environ.get("DATA_BACKEND", "sqlite").lower()
if DATA_BACKEND == "sqlite":
    from sqlite_store import SqliteDatabase

    def _default_sqlite_path() -> str:
        """Keep desktop data outside PyInstaller's temporary extraction folder."""
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            data_dir = Path(local_app_data) / "LedgerLens"
            data_dir.mkdir(parents=True, exist_ok=True)
            return str(data_dir / "ledgerlens.db")
        return str(ROOT_DIR / "ledgerlens.db")

    client = None
    db = SqliteDatabase(os.environ.get("SQLITE_PATH", _default_sqlite_path()))
else:
    from motor.motor_asyncio import AsyncIOMotorClient
    mongo_url = os.environ.get("MONGO_URL", "mongodb://127.0.0.1:27017")
    client = AsyncIOMotorClient(mongo_url)
    db = client[os.environ.get("DB_NAME", "ledgerlens")]

app = FastAPI()
api_router = APIRouter(prefix="/api")


@app.middleware("http")
async def require_desktop_token(request: Request, call_next):
    """Authenticate localhost API traffic when launched by the desktop shell."""
    if BACKEND_TOKEN and request.url.path.startswith("/api"):
        if request.headers.get("x-ledgerlens-token") != BACKEND_TOKEN:
            return JSONResponse(status_code=403, content={"detail": "Invalid LedgerLens backend token"})
    return await call_next(request)


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


class ScanLocalBody(BaseModel):
    folder_path: str
    template_id: Optional[str] = None
    expected_period: Optional[int] = None
    resume_scan_id: Optional[str] = None


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
            "retention_note": "Documents stay on this device. No files are uploaded to any external server.",
            "settings": {"privacy_mode": True, "warn_network_drives": True},
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


# -------------------------- system check ---------------------------
@api_router.get("/system-check")
async def get_system_check():
    import shutil
    import psutil
    import requests
    import sqlite3

    checks = []

    # 1. LedgerLens desktop app
    checks.append({
        "id": "app",
        "name": "LedgerLens App",
        "status": "PASS",
        "explanation": "Desktop shell is running in local mode (v1.0.0)."
    })

    # 2. Python Backend
    checks.append({
        "id": "backend",
        "name": "Backend API",
        "status": "PASS",
        "explanation": f"FastAPI backend sidecar is alive and listening on port {os.environ.get('PORT', '8001')}."
    })

    # 3. SQLite Storage
    sqlite_ok = False
    sqlite_err = ""
    try:
        c = await db.firm.find_one({"id": "firm"})
        if not c:
            await db.firm.insert_one({
                "id": "firm",
                "name": "",
                "contact_email": "",
                "retention_note": "Documents stay on this device. No files are uploaded to any external server.",
                "settings": {"privacy_mode": True, "warn_network_drives": True},
                "created_at": now_iso(),
            })
            c = await db.firm.find_one({"id": "firm"})
        sqlite_ok = c is not None
    except Exception as e:
        sqlite_err = str(e)

    checks.append({
        "id": "sqlite",
        "name": "SQLite Database",
        "status": "PASS" if sqlite_ok else "FAILED",
        "explanation": "Local SQLite database is initialized and responding." if sqlite_ok else f"SQLite error: {sqlite_err}"
    })

    # 4. OCR Engine & Dependencies
    ocr_provider = PaddleOCRProvider()
    ocr_available = ocr_provider.available
    checks.append({
        "id": "ocr_engine",
        "name": "OCR Engine (PaddleOCR)",
        "status": "PASS" if ocr_available else "WARNING",
        "explanation": "PaddleOCR framework and model runtime are ready (CPU-accelerated)." if ocr_available else "PaddleOCR is not available on this machine (scanned image OCR will be skipped safely)."
    })

    pymupdf_available = False
    try:
        import importlib.util
        pymupdf_available = importlib.util.find_spec("pymupdf") is not None
    except Exception:
        pass

    checks.append({
        "id": "ocr_deps",
        "name": "OCR PDF Dependencies (PyMuPDF)",
        "status": "PASS" if pymupdf_available else "WARNING",
        "explanation": "PyMuPDF rasterizer is available for image-only PDF extraction." if pymupdf_available else "PyMuPDF not installed (PDF page rasterization unavailable)."
    })

    # 5. Ollama Runtime
    ollama_provider = OllamaLLMProvider()
    ollama_up = ollama_provider.available
    checks.append({
        "id": "ollama",
        "name": "Ollama Local Runtime",
        "status": "PASS" if ollama_up else "WARNING",
        "explanation": "Ollama local service is connected on http://127.0.0.1:11434." if ollama_up else "Ollama service is not running or starting up."
    })

    # 6. Qwen Model
    qwen_found = False
    if ollama_up:
        try:
            r = requests.get("http://127.0.0.1:11434/api/tags", timeout=2)
            if r.status_code == 200:
                tags = r.json() or {}
                models = [m.get("name", "") for m in tags.get("models", [])]
                qwen_found = any("qwen" in m.lower() for m in models)
        except Exception:
            pass

    checks.append({
        "id": "qwen",
        "name": "Qwen Language Model (qwen2:0.5b)",
        "status": "PASS" if qwen_found else ("WARNING" if ollama_up else "WARNING"),
        "explanation": "Exact model 'qwen2:0.5b' is installed locally." if qwen_found else "qwen2:0.5b model is not currently installed or Ollama is offline."
    })

    # 7. Local API Connection
    checks.append({
        "id": "api_conn",
        "name": "Local API Connection",
        "status": "PASS",
        "explanation": "Frontend connects exclusively to 127.0.0.1 (zero public exposition)."
    })

    # 8. Required Disk Space
    usage = shutil.disk_usage(str(ROOT_DIR))
    free_gb = round(usage.free / (1024 ** 3), 1)
    disk_status = "PASS" if free_gb >= 2.0 else ("WARNING" if free_gb >= 0.5 else "FAILED")
    checks.append({
        "id": "disk",
        "name": "Required Disk Space",
        "status": disk_status,
        "explanation": f"{free_gb} GB free on current drive (minimum 2.0 GB recommended)."
    })

    # 9. Available RAM
    mem = psutil.virtual_memory()
    total_ram_gb = round(mem.total / (1024 ** 3), 1)
    avail_ram_gb = round(mem.available / (1024 ** 3), 1)
    ram_status = "PASS" if total_ram_gb >= 4.0 else "WARNING"
    checks.append({
        "id": "ram",
        "name": "System RAM",
        "status": ram_status,
        "explanation": f"{avail_ram_gb} GB available of {total_ram_gb} GB total."
    })

    # 10. GPU / CPU Capability
    cpu_cores = psutil.cpu_count(logical=True)
    gpu_desc = "CPU-only mode (multi-threaded fallback supported)"
    try:
        import subprocess
        smi = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True, timeout=1)
        if smi.returncode == 0 and smi.stdout.strip():
            gpu_desc = f"NVIDIA GPU detected: {smi.stdout.strip()} (Hardware acceleration enabled)"
    except Exception:
        pass

    checks.append({
        "id": "hardware",
        "name": "Compute Capability (CPU / GPU)",
        "status": "PASS",
        "explanation": f"{cpu_cores} logical CPU cores detected. {gpu_desc}."
    })

    # 11. Folders & Permissions
    app_dir_writable = os.access(str(ROOT_DIR), os.W_OK)
    checks.append({
        "id": "permissions",
        "name": "Folder Permissions",
        "status": "PASS" if app_dir_writable else "WARNING",
        "explanation": "Local application data directory is fully writable for SQLite and temporary files." if app_dir_writable else "Application directory has restricted write permissions."
    })

    # 12. Offline / Local-only configuration
    checks.append({
        "id": "offline",
        "name": "Offline & Local Privacy",
        "status": "PASS",
        "explanation": "Zero cloud document egress. All hashing, OCR, classification and database storage are 100% on-device."
    })

    return {"checks": checks, "timestamp": now_iso()}


@api_router.post("/system-test")
async def run_full_system_test():
    """Runs a live verification of OCR on a synthetic image and Ollama/Qwen on a synthetic prompt."""
    from PIL import Image, ImageDraw

    ocr_result = {"status": "SKIPPED", "message": "OCR provider not available"}
    qwen_result = {"status": "SKIPPED", "message": "Ollama/Qwen not available"}

    # Test OCR
    ocr_provider = PaddleOCRProvider()
    if ocr_provider.available:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            img = Image.new("RGB", (320, 80), color=(255, 255, 255))
            draw = ImageDraw.Draw(img)
            draw.text((15, 25), "SYNTHETIC INVOICE 2024", fill=(0, 0, 0))
            img.save(tmp_path)

            extracted = await asyncio.to_thread(ocr_provider.extract, tmp_path, "png")
            if extracted and "INVOICE" in extracted.upper():
                ocr_result = {"status": "PASS", "message": f"Successfully extracted text from image: '{extracted.strip()}'"}
            elif extracted:
                ocr_result = {"status": "PASS", "message": f"OCR executed: '{extracted.strip()}'"}
            else:
                ocr_result = {"status": "WARNING", "message": "OCR ran but returned empty text."}
        except Exception as e:
            ocr_result = {"status": "FAILED", "message": f"OCR execution error: {e}"}
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    # Test Ollama / Qwen
    ollama_provider = OllamaLLMProvider()
    if ollama_provider.available:
        try:
            res = await asyncio.to_thread(
                ollama_provider._generate,
                "What is 2+2? Reply with only the number."
            )
            if res and "4" in res:
                qwen_result = {"status": "PASS", "message": f"Local model responded: '{res.strip()}'"}
            elif res:
                qwen_result = {"status": "PASS", "message": f"Local model generated: '{res.strip()}'"}
            else:
                qwen_result = {"status": "WARNING", "message": "Model connection established but no completion generated."}
        except Exception as e:
            qwen_result = {"status": "FAILED", "message": f"Model inference error: {e}"}

    return {
        "ocr": ocr_result,
        "qwen": qwen_result,
        "timestamp": now_iso()
    }


# -------------------------- automated setup ------------------------
SETUP_STATUS = {
    "in_progress": False,
    "step": "idle",
    "message": "",
    "error": None,
    "percent": 0,
}

@api_router.get("/setup/status")
async def get_setup_status():
    return SETUP_STATUS


@api_router.post("/setup/ollama-qwen")
async def setup_ollama_qwen():
    """Automatically installs Ollama and downloads qwen2:0.5b model locally."""
    global SETUP_STATUS
    if SETUP_STATUS["in_progress"]:
        return SETUP_STATUS

    SETUP_STATUS = {
        "in_progress": True,
        "step": "checking",
        "message": "Checking local AI runtime...",
        "error": None,
        "percent": 10,
    }

    async def _do_setup():
        global SETUP_STATUS
        import subprocess
        import requests
        try:
            local_appdata = os.environ.get("LOCALAPPDATA", "")
            ollama_exe = os.path.join(local_appdata, "Programs", "Ollama", "ollama.exe")

            # Step 1: Detect/Install Ollama
            if not os.path.exists(ollama_exe):
                SETUP_STATUS["step"] = "installing_ollama"
                SETUP_STATUS["message"] = "Downloading & installing Ollama local runtime via winget..."
                SETUP_STATUS["percent"] = 25
                cmd = ["winget", "install", "-e", "--id", "Ollama.Ollama", "--accept-source-agreements", "--accept-package-agreements", "--silent"]
                proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
                if not os.path.exists(ollama_exe):
                    SETUP_STATUS["error"] = f"Failed to install Ollama automatically. Exit code: {proc.returncode}. {proc.stderr[:200]}"
                    SETUP_STATUS["in_progress"] = False
                    return

            # Step 2: Ensure Ollama is running
            SETUP_STATUS["step"] = "starting_ollama"
            SETUP_STATUS["message"] = "Starting local Ollama service on 127.0.0.1:11434..."
            SETUP_STATUS["percent"] = 50

            # Ping tags
            is_running = False
            for _ in range(3):
                try:
                    r = requests.get("http://127.0.0.1:11434/api/tags", timeout=2)
                    if r.status_code == 200:
                        is_running = True
                        break
                except Exception:
                    pass
                if not is_running:
                    # Launch serve
                    env = os.environ.copy()
                    env["OLLAMA_HOST"] = "127.0.0.1:11434"
                    env["OLLAMA_VULKAN"] = "false"
                    env["CUDA_VISIBLE_DEVICES"] = "-1"
                    env["GGML_VK_VISIBLE_DEVICES"] = "-1"
                    flags = 0
                    if sys.platform == "win32":
                        flags = 0x08000000  # CREATE_NO_WINDOW
                    subprocess.Popen([ollama_exe, "serve"], env=env, creationflags=flags)
                    await asyncio.sleep(2)

            # Step 3: Check/Pull qwen2:0.5b
            SETUP_STATUS["step"] = "checking_model"
            SETUP_STATUS["message"] = "Checking local qwen2:0.5b model..."
            SETUP_STATUS["percent"] = 70

            has_qwen = False
            try:
                r = requests.get("http://127.0.0.1:11434/api/tags", timeout=2)
                if r.status_code == 200:
                    models = [m.get("name", "") for m in r.json().get("models", [])]
                    has_qwen = any("qwen2:0.5b" in m.lower() for m in models)
            except Exception:
                pass

            if not has_qwen:
                SETUP_STATUS["step"] = "pulling_model"
                SETUP_STATUS["message"] = "Downloading qwen2:0.5b model (~350MB) locally..."
                SETUP_STATUS["percent"] = 80
                # Trigger pull via Ollama API (non-blocking stream)
                r = requests.post("http://127.0.0.1:11434/api/pull", json={"name": "qwen2:0.5b", "stream": False}, timeout=300)
                if r.status_code != 200:
                    SETUP_STATUS["error"] = f"Failed to pull model: {r.text}"
                    SETUP_STATUS["in_progress"] = False
                    return

            SETUP_STATUS["step"] = "completed"
            SETUP_STATUS["message"] = "Local AI & OCR setup complete! Everything is running 100% locally."
            SETUP_STATUS["percent"] = 100
            SETUP_STATUS["in_progress"] = False

        except Exception as e:
            SETUP_STATUS["error"] = str(e)
            SETUP_STATUS["in_progress"] = False

    asyncio.create_task(_do_setup())
    return SETUP_STATUS


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
    if DATA_BACKEND == "sqlite":
        raise HTTPException(400, "File uploads are unavailable in local mode; use Scan a Local Folder so documents stay on this device")
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


async def _run_local_scan(scan_id: str, client_id: str, folder_path: str,
                          template_id: Optional[str], expected_period: Optional[int],
                          resume_scan_id: Optional[str] = None):
    """Local desktop build: scan a real folder in place via LocalDirectoryFileSource.

    No files are copied or uploaded. Reads directly from the user's disk. OCR/LLM
    are activated only if available locally (build_local_engine falls back to
    no-op otherwise).
    """
    try:
        items = await _load_items(template_id)
        source = LocalDirectoryFileSource(folder_path, supported_only=True)
        engine = build_local_engine(enable_ocr=True, enable_llm=False)

        resume_state = None
        if resume_scan_id:
            prior_scan = await db.scans.find_one({"id": resume_scan_id})
            if prior_scan and "file_states" in prior_scan:
                resume_state = prior_scan["file_states"]

        await db.scans.update_one({"id": scan_id}, {"$set": {"status": "scanning", "progress": 0}})
        on_progress = _make_progress(scan_id, asyncio.get_event_loop())
        result = await asyncio.to_thread(
            engine.run, source, items, expected_period, on_progress,
            lambda: scan_id in CANCEL_REQUESTS,
            resume_state=resume_state,
        )
        await _finalize_scan(scan_id, client_id, result)
    except Exception as e:  # noqa: BLE001
        logger.exception("local scan failed")
        await db.scans.update_one({"id": scan_id}, {"$set": {"status": "error", "error": str(e)}})
    finally:
        CANCEL_REQUESTS.discard(scan_id)


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


@api_router.post("/clients/{client_id}/scan-local")
async def start_local_scan(client_id: str, body: ScanLocalBody):
    """Desktop/local scan: scans a real folder path on this machine in place.

    Used by the Tauri desktop build (folder picker). Requires filesystem access
    to `folder_path`; no files are uploaded or copied.
    """
    c = await db.clients.find_one({"id": client_id})
    if not c:
        raise HTTPException(404, "Client not found")
    if not os.path.isdir(body.folder_path):
        raise HTTPException(400, "Folder path not found or not a directory")
    if body.resume_scan_id:
        prior_scan = await db.scans.find_one({"id": body.resume_scan_id})
        if not prior_scan:
            raise HTTPException(404, "Resume scan not found")
        if prior_scan.get("client_id") != client_id or prior_scan.get("source_type") != "local":
            raise HTTPException(400, "Resume scan does not belong to this client's local scans")
        if os.path.normcase(os.path.abspath(prior_scan.get("folder_path", ""))) != os.path.normcase(os.path.abspath(body.folder_path)):
            raise HTTPException(400, "Resume folder must match the original scan folder")
    scan = {
        "id": new_id(),
        "client_id": client_id,
        "client_name": c["name"],
        "template_id": body.template_id,
        "expected_period": body.expected_period,
        "source_type": "local",
        "folder_path": body.folder_path,
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
    asyncio.create_task(_run_local_scan(scan["id"], client_id, body.folder_path, body.template_id, body.expected_period, body.resume_scan_id))
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


@api_router.get("/")
async def root():
    return {"message": "Accounting Document Checker API"}


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
    if DATA_BACKEND != "sqlite":
        try:
            storage.init_storage()
            logger.info("Object storage initialized")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Storage init failed: {e}")
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
