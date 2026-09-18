"""Reusable service layer and business logic for LedgerLens SaaS and AI Agent tools.

Every service function receives an AuthedUser context object as the first parameter,
guaranteeing that tenant identification originates from validated authentication tokens
and is strictly isolated via scoped() queries.
"""
import asyncio
from datetime import datetime, timezone
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional
import uuid

from fastapi import HTTPException

from auth_dep import AuthedUser
from db_access import scoped
from engine import run_detection, default_templates, SUPPORTED_EXTENSIONS
from engine import report as report_engine
import storage

logger = logging.getLogger(__name__)

CANCEL_REQUESTS: set = set()


# ----------------------------- Helpers -----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def clean(doc: dict) -> dict:
    doc.pop("_id", None)
    return doc


def _get_db(db=None):
    if db is not None:
        return db
    import server
    if server.db is None:
        raise HTTPException(500, "Database not initialized")
    return server.db


# ----------------------------- Firm Services -----------------------------
async def get_firm(user: AuthedUser, db=None) -> dict:
    """Retrieve firm profile for the user's firm, seeding defaults if not present."""
    database = _get_db(db)
    doc = await database.firm.find_one({"id": user.firm_id})
    if not doc:
        doc = {
            "id": user.firm_id,
            "name": "My Firm",
            "contact_email": user.email,
            "retention_note": "Documents are securely processed in isolated cloud environments.",
            "settings": {"privacy_mode": True},
            "created_at": now_iso(),
        }
        await database.firm.insert_one(dict(doc))
    return clean(doc)


async def update_firm(user: AuthedUser, update_data: dict, db=None) -> dict:
    """Update firm profile settings."""
    database = _get_db(db)
    update = {k: v for k, v in update_data.items() if v is not None}
    update["updated_at"] = now_iso()
    await database.firm.update_one({"id": user.firm_id}, {"$set": update}, upsert=True)
    doc = await database.firm.find_one({"id": user.firm_id})
    return clean(doc)


# ---------------------------- Client Services ----------------------------
async def list_clients(user: AuthedUser, db=None) -> List[dict]:
    """List all clients for the authenticated firm with aggregate file and scan stats."""
    database = _get_db(db)
    filt = scoped(database.clients, user, {})
    clients = await database.clients.find(filt, {"_id": 0}).sort("created_at", -1).to_list(1000)
    for c in clients:
        c_filt = scoped(database.files, user, {"client_id": c["id"], "is_deleted": {"$ne": True}})
        c["file_count"] = await database.files.count_documents(c_filt)
        last_scan_filt = scoped(database.scans, user, {"client_id": c["id"]})
        last = await database.scans.find_one(last_scan_filt, {"_id": 0}, sort=[("started_at", -1)])
        c["last_scan"] = last
    return clients


async def get_client(user: AuthedUser, client_id: str, db=None) -> dict:
    """Retrieve a single client by ID with tenant isolation."""
    database = _get_db(db)
    filt = scoped(database.clients, user, {"id": client_id})
    doc = await database.clients.find_one(filt, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Client not found")
    return doc


async def create_client(
    user: AuthedUser,
    name: str,
    client_type: str = "Small Business",
    notes: Optional[str] = "",
    tax_id: Optional[str] = None,
    db=None,
) -> dict:
    """Create a new client entity scoped to the authenticated firm."""
    database = _get_db(db)
    doc = {
        "id": new_id(),
        "firm_id": user.firm_id,
        "name": name,
        "client_type": client_type,
        "notes": notes or "",
        "created_at": now_iso(),
    }
    if tax_id is not None:
        doc["tax_id"] = tax_id
    await database.clients.insert_one(dict(doc))
    return clean(doc)


async def delete_client(user: AuthedUser, client_id: str, db=None) -> dict:
    """Cascade-delete a client, its files, scans, findings, and storage objects."""
    database = _get_db(db)
    client = await database.clients.find_one(scoped(database.clients, user, {"id": client_id}))
    if not client:
        raise HTTPException(404, "Client not found")
    await database.delete_client_cascade(client_id, firm_id=user.firm_id)
    return {"ok": True}


# ----------------------------- File Services -----------------------------
async def list_files(user: AuthedUser, client_id: str, db=None) -> List[dict]:
    """List all active non-deleted files for a client."""
    database = _get_db(db)
    client = await database.clients.find_one(scoped(database.clients, user, {"id": client_id}))
    if not client:
        raise HTTPException(404, "Client not found")
    filt = scoped(database.files, user, {"client_id": client_id, "is_deleted": {"$ne": True}})
    return await database.files.find(filt, {"_id": 0, "storage_path": 0}).sort("uploaded_at", -1).to_list(100000)


async def save_client_file(
    user: AuthedUser,
    client_id: str,
    filename: str,
    content: bytes,
    db=None,
) -> dict:
    """Save raw document bytes into tenant storage and register in files collection."""
    database = _get_db(db)
    client = await database.clients.find_one(scoped(database.clients, user, {"id": client_id}))
    if not client:
        raise HTTPException(404, "Client not found")

    fid = new_id()
    original = os.path.basename(filename or "unnamed")
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
    object_path = f"{storage.APP_NAME}/uploads/{user.firm_id}/{client_id}/{fid}.{ext or 'bin'}"
    result = storage.put_object(object_path, content, storage.mime_for(ext))

    doc = {
        "id": fid,
        "firm_id": user.firm_id,
        "client_id": client_id,
        "name": original,
        "ext": ext,
        "size": result.get("size", len(content)),
        "supported": ext in SUPPORTED_EXTENSIONS,
        "storage_path": result["path"],
        "is_deleted": False,
        "uploaded_at": now_iso(),
    }
    await database.files.insert_one(dict(doc))
    doc.pop("storage_path", None)
    return clean(doc)


async def delete_file(user: AuthedUser, client_id: str, file_id: str, db=None) -> dict:
    """Delete a client file record and its backing storage object."""
    database = _get_db(db)
    filt = scoped(database.files, user, {"id": file_id, "client_id": client_id})
    file_doc = await database.files.find_one(filt)
    if not file_doc:
        raise HTTPException(404, "File not found")
    sp = file_doc.get("storage_path")
    if sp:
        storage.delete_object(sp)
    await database.files.delete_one(filt)
    return {"ok": True}


# ----------------------- Checklist Template Services -----------------------
async def list_templates(user: AuthedUser, db=None) -> List[dict]:
    """List checklist templates scoped to the user's firm."""
    database = _get_db(db)
    filt = scoped(database.templates, user, {})
    return await database.templates.find(filt, {"_id": 0}).sort("name", 1).to_list(1000)


async def create_template(user: AuthedUser, template_data: dict, db=None) -> dict:
    """Create a new checklist template for the firm."""
    database = _get_db(db)
    doc = {
        "id": new_id(),
        "firm_id": user.firm_id,
        "created_at": now_iso(),
        **template_data,
    }
    await database.templates.insert_one(dict(doc))
    return clean(doc)


async def update_template(user: AuthedUser, template_id: str, template_data: dict, db=None) -> dict:
    """Update an existing checklist template."""
    database = _get_db(db)
    filt = scoped(database.templates, user, {"id": template_id})
    existing = await database.templates.find_one(filt)
    if not existing:
        raise HTTPException(404, "Template not found")
    await database.templates.update_one(filt, {"$set": template_data})
    return await database.templates.find_one(filt, {"_id": 0})


async def delete_template(user: AuthedUser, template_id: str, db=None) -> dict:
    """Delete a checklist template."""
    database = _get_db(db)
    filt = scoped(database.templates, user, {"id": template_id})
    existing = await database.templates.find_one(filt)
    if not existing:
        raise HTTPException(404, "Template not found")
    await database.templates.delete_one(filt)
    return {"ok": True}


# ----------------------------- Scan Engine Helpers -----------------------------
def _make_progress(scan_id: str, firm_id: str, loop, db):
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


async def _finalize_scan(scan_id: str, firm_id: str, client_id: str, result: dict, db):
    """Persist findings + mark the scan cancelled/completed."""
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


async def _load_items(template_id: Optional[str], firm_id: str, db):
    if not template_id:
        return []
    template = await db.templates.find_one({"id": template_id, "firm_id": firm_id}, {"_id": 0})
    return template["items"] if template else []


async def _run_scan(
    scan_id: str,
    firm_id: str,
    client_id: str,
    template_id: Optional[str],
    expected_period: Optional[int],
    db,
):
    """Download files from object storage to temp dir, invoke run_detection, and finalize."""
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

        items = await _load_items(template_id, firm_id, db)
        await db.scans.update_one(
            {"id": scan_id, "firm_id": firm_id},
            {"$set": {"status": "scanning", "total_files": len(records), "processed_files": 0, "progress": 0}},
        )
        on_progress = _make_progress(scan_id, firm_id, asyncio.get_event_loop(), db)
        result = await asyncio.to_thread(
            run_detection, records, items, expected_period, on_progress,
            lambda: scan_id in CANCEL_REQUESTS,
        )
        await _finalize_scan(scan_id, firm_id, client_id, result, db)
    except Exception as e:  # noqa: BLE001
        logger.exception("scan failed")
        await db.scans.update_one({"id": scan_id, "firm_id": firm_id}, {"$set": {"status": "error", "error": str(e)}})
    finally:
        CANCEL_REQUESTS.discard(scan_id)
        import shutil as _shutil
        _shutil.rmtree(tmpdir, ignore_errors=True)


# ----------------------------- Scan Services -----------------------------
async def list_scans(user: AuthedUser, client_id: str, db=None) -> List[dict]:
    """List all scans conducted for a specific client."""
    database = _get_db(db)
    client = await database.clients.find_one(scoped(database.clients, user, {"id": client_id}))
    if not client:
        raise HTTPException(404, "Client not found")
    filt = scoped(database.scans, user, {"client_id": client_id})
    return await database.scans.find(filt, {"_id": 0}).sort("started_at", -1).to_list(1000)


async def get_scan(user: AuthedUser, scan_id: str, db=None) -> dict:
    """Retrieve status and metadata for a specific scan."""
    database = _get_db(db)
    doc = await database.scans.find_one(scoped(database.scans, user, {"id": scan_id}), {"_id": 0})
    if not doc:
        raise HTTPException(404, "Scan not found")
    return doc


async def cancel_scan(user: AuthedUser, scan_id: str, db=None) -> dict:
    """Cancel an ongoing or queued scan job."""
    database = _get_db(db)
    scan = await database.scans.find_one(scoped(database.scans, user, {"id": scan_id}), {"_id": 0})
    if not scan:
        raise HTTPException(404, "Scan not found")
    if scan["status"] in ("queued", "scanning"):
        CANCEL_REQUESTS.add(scan_id)
        await database.scans.update_one(scoped(database.scans, user, {"id": scan_id}), {"$set": {"status": "cancelling"}})
        return {"ok": True, "status": "cancelling"}
    return {"ok": False, "status": scan["status"]}


async def start_scan(
    user: AuthedUser,
    client_id: str,
    template_id: Optional[str] = None,
    expected_period: Optional[int] = None,
    db=None,
) -> dict:
    """Initiate a document audit scan for a client against a checklist template."""
    database = _get_db(db)
    c = await database.clients.find_one(scoped(database.clients, user, {"id": client_id}))
    if not c:
        raise HTTPException(404, "Client not found")
    if template_id:
        tpl = await database.templates.find_one(scoped(database.templates, user, {"id": template_id}))
        if not tpl:
            raise HTTPException(404, "Checklist template not found")
    scan = {
        "id": new_id(),
        "firm_id": user.firm_id,
        "client_id": client_id,
        "client_name": c["name"],
        "template_id": template_id,
        "expected_period": expected_period,
        "status": "queued",
        "progress": 0,
        "total_files": 0,
        "processed_files": 0,
        "skipped_files": [],
        "counts": {},
        "total_findings": 0,
        "started_at": now_iso(),
    }
    await database.scans.insert_one(dict(scan))
    asyncio.create_task(_run_scan(scan["id"], user.firm_id, client_id, template_id, expected_period, database))
    return clean(scan)


# ---------------------------- Findings Services ----------------------------
async def get_findings(
    user: AuthedUser,
    scan_id: str,
    category: Optional[str] = None,
    status: Optional[str] = None,
    db=None,
) -> List[dict]:
    """Retrieve findings/exceptions generated by a scan."""
    database = _get_db(db)
    scan = await database.scans.find_one(scoped(database.scans, user, {"id": scan_id}))
    if not scan:
        raise HTTPException(404, "Scan not found")
    q = scoped(database.findings, user, {"scan_id": scan_id})
    if category:
        q["category"] = category
    if status:
        q["status"] = status
    return await database.findings.find(q, {"_id": 0}).sort("confidence", -1).to_list(100000)


async def update_finding(
    user: AuthedUser,
    finding_id: str,
    update_data: dict,
    db=None,
) -> dict:
    """Review and update finding status/note."""
    database = _get_db(db)
    filt = scoped(database.findings, user, {"id": finding_id})
    if not await database.findings.find_one(filt):
        raise HTTPException(404, "Finding not found")
    update = {k: v for k, v in update_data.items() if v is not None}
    update["updated_at"] = now_iso()
    await database.findings.update_one(filt, {"$set": update})
    return await database.findings.find_one(filt, {"_id": 0})


# ----------------------------- Report Services -----------------------------
async def export_report(
    user: AuthedUser,
    scan_id: str,
    format: str = "csv",
    db=None,
) -> dict:
    """Generate formatted report data (CSV, XLSX, or PDF) for a completed scan."""
    database = _get_db(db)
    scan = await database.scans.find_one(scoped(database.scans, user, {"id": scan_id}), {"_id": 0})
    if not scan:
        raise HTTPException(404, "Scan not found")
    findings = await database.findings.find(
        scoped(database.findings, user, {"scan_id": scan_id}), {"_id": 0}
    ).sort("category", 1).to_list(100000)

    fmt = (format or "csv").lower()
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
    return {
        "data": data,
        "media_type": media,
        "filename": fname,
        "ext": ext,
    }


# ----------------------------- Agent Foundation Services -----------------------------
MAX_AGENT_MESSAGE_LENGTH = 10000

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_WAITING_FOR_APPROVAL = "waiting_for_approval"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"

VALID_RUN_STATUSES = {
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_FOR_APPROVAL,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_CANCELLED,
}


async def create_agent_thread(
    user: AuthedUser,
    title: Optional[str] = None,
    metadata: Optional[dict] = None,
    db=None,
) -> dict:
    """Create a new agent thread strictly scoped to the user's firm."""
    database = _get_db(db)
    thread_id = new_id()
    t_now = now_iso()
    doc = {
        "id": thread_id,
        "firm_id": user.firm_id,
        "title": (title or "New Conversation").strip()[:100],
        "created_by": user.id,
        "created_at": t_now,
        "updated_at": t_now,
        "metadata": metadata or {},
    }
    await database.agent_threads.insert_one(doc)
    return clean(doc)


async def get_agent_thread(user: AuthedUser, thread_id: str, db=None) -> dict:
    """Retrieve an agent thread ensuring strict tenant isolation."""
    database = _get_db(db)
    clean_id = (thread_id or "").strip()
    if not clean_id:
        raise HTTPException(400, "thread_id is required")
    thread = await database.agent_threads.find_one(
        scoped(database.agent_threads, user, {"id": clean_id}),
        {"_id": 0},
    )
    if not thread:
        raise HTTPException(404, "Agent thread not found")
    return thread


async def list_agent_messages(user: AuthedUser, thread_id: str, db=None) -> List[dict]:
    """List all messages in a thread in chronological order."""
    database = _get_db(db)
    # Verify thread exists in firm
    await get_agent_thread(user, thread_id, db=database)
    return await database.agent_messages.find(
        scoped(database.agent_messages, user, {"thread_id": thread_id.strip()}),
        {"_id": 0},
    ).sort("created_at", 1).to_list(10000)


async def post_agent_message(
    user: AuthedUser,
    text: str,
    thread_id: Optional[str] = None,
    db=None,
) -> dict:
    """Validate, record user message, and enqueue an agent run strictly within user's firm.

    Returns HTTP 202 payload with run_id, thread_id, and status.
    Does NOT invoke LLM loop or tools yet.
    """
    database = _get_db(db)

    # 1. Validate message text
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(400, "Message text cannot be empty.")

    clean_text = text.strip()
    if len(clean_text) > MAX_AGENT_MESSAGE_LENGTH:
        raise HTTPException(
            400,
            f"Message text exceeds maximum allowed length of {MAX_AGENT_MESSAGE_LENGTH} characters."
        )

    t_now = now_iso()

    # 2. Verify or create thread
    target_thread_id = (thread_id or "").strip()
    if target_thread_id:
        thread = await database.agent_threads.find_one(
            scoped(database.agent_threads, user, {"id": target_thread_id}),
            {"_id": 0},
        )
        if not thread:
            raise HTTPException(404, "Agent thread not found")
        await database.agent_threads.update_one(
            scoped(database.agent_threads, user, {"id": target_thread_id}),
            {"$set": {"updated_at": t_now}},
        )
    else:
        target_thread_id = new_id()
        thread_title = clean_text.replace("\n", " ")[:60].strip() or "New Conversation"
        thread = {
            "id": target_thread_id,
            "firm_id": user.firm_id,
            "title": thread_title,
            "created_by": user.id,
            "created_at": t_now,
            "updated_at": t_now,
            "metadata": {},
        }
        await database.agent_threads.insert_one(thread)

    # 3. Create user message record
    msg_id = new_id()
    msg_doc = {
        "id": msg_id,
        "firm_id": user.firm_id,
        "thread_id": target_thread_id,
        "sender_id": user.id,
        "role": "user",
        "text": clean_text,
        "created_at": t_now,
        "metadata": {},
    }
    await database.agent_messages.insert_one(msg_doc)

    # 4. Create agent run record in queued state
    run_id = new_id()
    run_doc = {
        "id": run_id,
        "firm_id": user.firm_id,
        "thread_id": target_thread_id,
        "status": RUN_STATUS_QUEUED,
        "created_by": user.id,
        "created_at": t_now,
        "updated_at": t_now,
        "started_at": None,
        "completed_at": None,
        "error": None,
        "metadata": {"initial_message_id": msg_id},
    }
    await database.agent_runs.insert_one(run_doc)

    # 5. Launch background agent loop execution
    try:
        from agent.loop import start_agent_run_background
        start_agent_run_background(user, run_id, target_thread_id, db=database)
    except Exception as e:
        logger.warning(f"Could not immediately start background agent loop: {e}")

    return {
        "run_id": run_id,
        "thread_id": target_thread_id,
        "status": RUN_STATUS_QUEUED,
        "created_at": t_now,
    }


async def get_agent_run(user: AuthedUser, run_id: str, db=None) -> dict:
    """Retrieve run metadata strictly scoped to user's firm, hiding internal thoughts."""
    database = _get_db(db)
    clean_run_id = (run_id or "").strip()
    if not clean_run_id:
        raise HTTPException(400, "run_id is required")

    run = await database.agent_runs.find_one(
        scoped(database.agent_runs, user, {"id": clean_run_id}),
        {"_id": 0},
    )
    if not run:
        raise HTTPException(404, "Agent run not found")
    return {
        "id": run["id"],
        "firm_id": run["firm_id"],
        "thread_id": run["thread_id"],
        "status": run.get("status"),
        "created_by": run.get("created_by"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "error": run.get("error"),
    }


async def cancel_agent_run(user: AuthedUser, run_id: str, db=None) -> dict:
    """Cancel an active or queued agent run."""
    import agent.loop as loop
    loop.request_run_cancellation(run_id)
    return await update_agent_run(
        user,
        run_id,
        {"status": RUN_STATUS_CANCELLED, "completed_at": now_iso()},
        db=db,
    )


async def update_agent_run(
    user: AuthedUser,
    run_id: str,
    update_data: dict,
    db=None,
) -> dict:
    """Update agent run state and metadata (for Step 4 agent loop)."""
    database = _get_db(db)
    clean_run_id = (run_id or "").strip()
    await get_agent_run(user, clean_run_id, db=database)

    status = update_data.get("status")
    if status is not None and status not in VALID_RUN_STATUSES:
        raise HTTPException(400, f"Invalid run status: '{status}'. Must be one of {sorted(VALID_RUN_STATUSES)}")

    fields = dict(update_data)
    fields["updated_at"] = now_iso()
    await database.agent_runs.update_one(
        scoped(database.agent_runs, user, {"id": clean_run_id}),
        {"$set": fields},
    )
    return await get_agent_run(user, clean_run_id, db=database)


async def create_agent_run_step(
    user: AuthedUser,
    run_id: str,
    thread_id: str,
    step_type: str,
    input_data: Optional[dict] = None,
    output_data: Optional[dict] = None,
    error: Optional[str] = None,
    status: str = "completed",
    db=None,
) -> dict:
    """Record an agent execution step (for Step 4 tool/thought persistence)."""
    database = _get_db(db)
    await get_agent_run(user, run_id, db=database)

    step_id = new_id()
    t_now = now_iso()
    step_doc = {
        "id": step_id,
        "firm_id": user.firm_id,
        "run_id": run_id.strip(),
        "thread_id": thread_id.strip(),
        "step_type": step_type.strip(),
        "status": status,
        "input_data": input_data or {},
        "output_data": output_data or {},
        "error": error,
        "created_at": t_now,
        "completed_at": t_now if status in ("completed", "failed") else None,
    }
    await database.agent_run_steps.insert_one(step_doc)
    return clean(step_doc)


async def list_agent_run_steps(user: AuthedUser, run_id: str, db=None) -> List[dict]:
    """List execution steps for an agent run in chronological order."""
    database = _get_db(db)
    await get_agent_run(user, run_id, db=database)
    return await database.agent_run_steps.find(
        scoped(database.agent_run_steps, user, {"run_id": run_id.strip()}),
        {"_id": 0},
    ).sort("created_at", 1).to_list(10000)


# ---------------------------- Approval Services ----------------------------
async def list_agent_approvals(
    user: AuthedUser,
    run_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    db=None,
) -> List[dict]:
    """List approvals scoped to user's firm."""
    import agent.approvals as approvals
    return await approvals.list_approvals(
        user, run_id=run_id, thread_id=thread_id, status=status, limit=limit, db=db
    )


async def get_agent_approval(user: AuthedUser, approval_id: str, db=None) -> dict:
    """Get single approval scoped to user's firm."""
    import agent.approvals as approvals
    return await approvals.get_approval(user, approval_id, db=db)


async def approve_agent_approval(user: AuthedUser, approval_id: str, db=None) -> dict:
    """Approve a pending approval."""
    import agent.approvals as approvals
    return await approvals.approve_approval(user, approval_id, db=db)


async def reject_agent_approval(user: AuthedUser, approval_id: str, reason: Optional[str] = None, db=None) -> dict:
    """Reject a pending approval."""
    import agent.approvals as approvals
    return await approvals.reject_approval(user, approval_id, reason=reason, db=db)
