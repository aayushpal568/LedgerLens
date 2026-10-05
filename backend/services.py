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

# A3: explicit ceiling on the number of rows a single list/query fetch materializes into RAM.
# The default preserves the previous implicit 100000-row behavior exactly; it is now a named,
# ops-tunable constant, and read endpoints additionally accept optional limit/offset so a
# caller can request a bounded page without changing the default (full) response shape.
MAX_ROWS_PER_QUERY = int(os.environ.get("MAX_ROWS_PER_QUERY", "100000"))


def _paginate_bounds(limit: Optional[int], offset: Optional[int]):
    """Normalize optional (limit, offset) into (fetch_len, off).

    ``fetch_len is None`` means "no explicit page — fetch up to the global cap", i.e. byte-for-byte
    the previous behavior. When a limit is given, the DB fetch is bounded to ``off + limit`` rows and
    sliced in Python, so huge result sets never fully materialize in memory.
    """
    off = max(0, int(offset)) if offset else 0
    if limit is None:
        return None, off
    lim = min(max(1, int(limit)), MAX_ROWS_PER_QUERY)
    return lim, off


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


async def purge_firm(user: AuthedUser, confirm: bool, db=None) -> dict:
    """Permanently delete the caller's ENTIRE firm: every record + object (A7, self-service).

    This is the tenant-wide counterpart to delete_client (which only cascades one client's
    clients/files/scans/findings and leaves templates, agent history, users, and the firm
    record behind). Strictly scoped to the caller's OWN firm_id — it can never target another
    tenant. Destructive and irreversible, so an explicit `confirm` is required. Deleting the
    firm's users also revokes their sessions (auth revalidates token_version against the DB).
    """
    if not confirm:
        raise HTTPException(400, "Set confirm=true to permanently delete this firm and ALL of its data. This cannot be undone.")

    database = _get_db(db)
    firm = await database.firm.find_one({"id": user.firm_id})
    if not firm:
        raise HTTPException(404, "Firm not found")

    # Atomically remove every firm-scoped record + the firm document; returns removed counts
    # and the storage paths of the firm's files (collected before the delete committed).
    result = await database.purge_firm_data(user.firm_id)

    # Purge backing objects AFTER the DB delete committed (no dangling DB references). Failures
    # are logged and counted, not raised — orphaned objects are a cost/privacy cleanup, not a
    # correctness break, and can be retried by an operator.
    storage_failures = 0
    for sp in result.get("storage_paths", []):
        try:
            await asyncio.to_thread(storage.delete_object, sp)
        except Exception as e:  # noqa: BLE001
            storage_failures += 1
            logger.warning("firm purge %s: failed to delete storage object %s: %s", user.firm_id, sp, e)

    counts = result.get("counts", {})
    total_objects = len(result.get("storage_paths", []))
    logger.warning(
        "Firm %s purged: collections=%s objects=%d failures=%d", user.firm_id, counts, total_objects, storage_failures
    )
    return {
        "ok": True,
        "firm_id": user.firm_id,
        "removed": counts,
        "objects_purged": total_objects - storage_failures,
        "storage_failures": storage_failures,
        "message": "Firm and all associated data permanently deleted.",
    }


# ----------------------------- File Services -----------------------------
async def list_files(user: AuthedUser, client_id: str, db=None, limit: Optional[int] = None, offset: Optional[int] = None) -> List[dict]:
    """List active non-deleted files for a client (optional limit/offset page; default = all)."""
    database = _get_db(db)
    client = await database.clients.find_one(scoped(database.clients, user, {"id": client_id}))
    if not client:
        raise HTTPException(404, "Client not found")
    filt = scoped(database.files, user, {"client_id": client_id, "is_deleted": {"$ne": True}})
    fetch_len, off = _paginate_bounds(limit, offset)
    if fetch_len is not None:
        rows = await database.files.find(filt, {"_id": 0, "storage_path": 0}).sort("uploaded_at", -1).to_list(off + fetch_len)
        return rows[off:off + fetch_len]
    rows = await database.files.find(filt, {"_id": 0, "storage_path": 0}).sort("uploaded_at", -1).to_list(MAX_ROWS_PER_QUERY)
    if len(rows) >= MAX_ROWS_PER_QUERY:
        logger.warning("list_files capped at %d rows for client %s; pass limit/offset to page", MAX_ROWS_PER_QUERY, client_id)
    if off:
        rows = rows[off:]
    return rows


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
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file extension: '.{ext}'. Supported types: {sorted(SUPPORTED_EXTENSIONS)}")
    object_path = f"{storage.APP_NAME}/uploads/{user.firm_id}/{client_id}/{fid}.{ext or 'bin'}"
    result = await asyncio.to_thread(storage.put_object, object_path, content, storage.mime_for(ext))

    doc = {
        "id": fid,
        "firm_id": user.firm_id,
        "client_id": client_id,
        "name": original,
        "ext": ext,
        "size": result.get("size", len(content)),
        "supported": True,
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
        await asyncio.to_thread(storage.delete_object, sp)
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
        try:
            doc = await db.scans.find_one({"id": scan_id, "firm_id": firm_id}, {"status": 1})
            if doc and doc.get("status") in ("cancelling", "cancelled"):
                CANCEL_REQUESTS.add(scan_id)
        except Exception:
            pass
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
        ).to_list(MAX_ROWS_PER_QUERY)
        if len(file_docs) >= MAX_ROWS_PER_QUERY:
            logger.warning("scan %s: client %s hit the %d-file cap; some files may be excluded from this scan", scan_id, client_id, MAX_ROWS_PER_QUERY)
        records = []
        download_failures = []
        for f in file_docs:
            fname = f.get("name") or f["id"]
            local_path = os.path.join(tmpdir, f"{f['id']}.{f.get('ext') or 'bin'}")
            data = None
            # Retry TRANSIENT storage errors a few times; a genuinely missing object
            # (FileNotFoundError) is not retried. On persistent failure we MUST NOT append
            # a phantom record: writing nothing to local_path and still listing the file makes
            # the detection engine treat an unreadable document as an empty file, producing
            # FALSE 'missing'/'duplicate' findings. Excluding it (and surfacing it as skipped)
            # is the correctness-critical part of this fix.
            for attempt in range(3):
                try:
                    data = await asyncio.to_thread(storage.get_object, f["storage_path"])
                    break
                except FileNotFoundError:
                    download_failures.append({"name": fname, "reason": "Document not found in storage"})
                    logger.warning("scan %s: file %s (%s) missing in storage; excluding from detection", scan_id, f["id"], fname)
                    break
                except Exception as e:  # noqa: BLE001 - transient storage/network error
                    if attempt == 2:
                        download_failures.append({"name": fname, "reason": "Could not read document from storage"})
                        logger.exception("scan %s: file %s (%s) storage read failed after retries; excluding from detection", scan_id, f["id"], fname)
                    else:
                        await asyncio.sleep(0.5 * (attempt + 1))
            if data is None:
                continue
            with open(local_path, "wb") as out:
                out.write(data)
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
        # Surface documents we could not read from storage as skipped (same {name, reason}
        # shape the engine uses), so firms see why a file was excluded instead of a silent gap.
        if download_failures:
            result.setdefault("skipped", [])
            result["skipped"].extend(download_failures)
            result["total"] = result.get("total", 0) + len(download_failures)
        await _finalize_scan(scan_id, firm_id, client_id, result, db)
    except Exception as e:  # noqa: BLE001
        logger.exception("scan failed: %s", e)
        safe_error_msg = "An error occurred during document audit scanning. Please review server logs."
        await db.scans.update_one({"id": scan_id, "firm_id": firm_id}, {"$set": {"status": "error", "error": safe_error_msg}})

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
    scan_id: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    db=None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> List[dict]:
    """Retrieve findings/exceptions generated by a scan or all firm findings if scan_id is omitted."""
    database = _get_db(db)
    q_filter = {}
    if scan_id:
        scan = await database.scans.find_one(scoped(database.scans, user, {"id": scan_id}))
        if not scan:
            raise HTTPException(404, "Scan not found")
        q_filter["scan_id"] = scan_id
    if category:
        q_filter["category"] = category
    if status:
        q_filter["status"] = status
    q = scoped(database.findings, user, q_filter)
    fetch_len, off = _paginate_bounds(limit, offset)
    if fetch_len is not None:
        rows = await database.findings.find(q, {"_id": 0}).sort("confidence", -1).to_list(off + fetch_len)
        return rows[off:off + fetch_len]
    rows = await database.findings.find(q, {"_id": 0}).sort("confidence", -1).to_list(MAX_ROWS_PER_QUERY)
    if len(rows) >= MAX_ROWS_PER_QUERY:
        logger.warning("get_findings capped at %d rows; pass limit/offset to page", MAX_ROWS_PER_QUERY)
    if off:
        rows = rows[off:]
    return rows



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
    ).sort("category", 1).to_list(MAX_ROWS_PER_QUERY)
    if len(findings) >= MAX_ROWS_PER_QUERY:
        logger.warning("export_report: scan %s hit the %d-finding cap; the report may be truncated", scan_id, MAX_ROWS_PER_QUERY)

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
    idempotency_key: Optional[str] = None,
    db=None,
) -> dict:
    """Validate, record user message, and enqueue an agent run strictly within user's firm.

    Returns HTTP 202 payload with run_id, thread_id, and status.
    Supports idempotent submission via idempotency_key.
    Does NOT invoke LLM loop or tools synchronously.
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

    # 2. Idempotency validation and replay check
    clean_idempotency_key: Optional[str] = None
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str):
            raise HTTPException(400, "idempotency_key must be a string")
        s = idempotency_key.strip()
        if s:
            if len(s) > 256:
                raise HTTPException(400, "idempotency_key exceeds maximum length of 256 characters")
            clean_idempotency_key = s

    if clean_idempotency_key:
        existing_run = await database.agent_runs.find_one(
            scoped(database.agent_runs, user, {"idempotency_key": clean_idempotency_key}),
            {"_id": 0},
        )
        if existing_run:
            return {
                "run_id": existing_run["id"],
                "thread_id": existing_run["thread_id"],
                "status": existing_run.get("status", RUN_STATUS_QUEUED),
                "created_at": existing_run.get("created_at"),
            }

    t_now = now_iso()

    # 3. Verify or create thread
    target_thread_id: Optional[str] = None
    if thread_id is not None:
        if not isinstance(thread_id, str):
            raise HTTPException(400, "thread_id must be a string")
        s_thread = thread_id.strip()
        if s_thread:
            if len(s_thread) > 256:
                raise HTTPException(400, "thread_id exceeds maximum length of 256 characters")
            target_thread_id = s_thread

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

    # 4. Create user message record
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

    # 5. Create agent run record in queued state
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
        "idempotency_key": clean_idempotency_key,
        "attempt": 0,
        "worker_id": None,
        "lease_expires_at": None,
        "heartbeat_at": None,
        "metadata": {"initial_message_id": msg_id},
    }
    await database.agent_runs.insert_one(run_doc)

    # 6. Launch background agent loop execution (unless a durable worker owns draining).
    if _inline_dispatch_enabled():
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
    """Retrieve run metadata strictly scoped to user's firm, exposing safe progress only."""
    database = _get_db(db)
    if not isinstance(run_id, str):
        raise HTTPException(400, "run_id must be a string")
    clean_run_id = run_id.strip()
    if not clean_run_id:
        raise HTTPException(400, "run_id is required")
    if len(clean_run_id) > 256:
        raise HTTPException(400, "run_id exceeds maximum length of 256 characters")

    run = await database.agent_runs.find_one(
        scoped(database.agent_runs, user, {"id": clean_run_id}),
        {"_id": 0},
    )
    if not run:
        raise HTTPException(404, "Agent run not found")

    # Expose only safe metadata; never leak internal thoughts, system prompts, or private tokens
    safe_metadata = {}
    for k, v in (run.get("metadata") or {}).items():
        if k in ("thought", "prompt", "system_prompt", "token", "raw_response", "internal"):
            continue
        safe_metadata[k] = v

    raw_error = run.get("error")
    safe_error = str(raw_error)[:500] if raw_error else None

    return {
        "id": run["id"],
        "firm_id": run["firm_id"],
        "thread_id": run["thread_id"],
        "status": run.get("status"),
        "created_by": run.get("created_by"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "error": safe_error,
        "metadata": safe_metadata,
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


# ----------------------- Durable Agent Worker Primitives -----------------------
# Stable per-process worker identity for durable run claiming / lease ownership.
import socket as _socket
WORKER_ID = f"{_socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except (ValueError, TypeError):
        return default


def agent_lease_seconds() -> int:
    """Lease (heartbeat) window before a running run is considered dead."""
    return _env_int("AGENT_LEASE_SECONDS", 120)


def agent_run_timeout_seconds() -> int:
    """Wall-clock maximum execution time for a single agent run."""
    return _env_int("AGENT_RUN_TIMEOUT_SECONDS", 180)


def agent_max_attempts() -> int:
    """Maximum durable-execution attempts before a stale run is failed permanently."""
    return _env_int("AGENT_MAX_ATTEMPTS", 3)


def agent_max_tool_result_chars() -> int:
    """Maximum characters of a single tool result retained / fed back to Claude."""
    return _env_int("AGENT_MAX_TOOL_RESULT_CHARS", 12000)


def agent_max_total_output_tokens() -> int:
    """Aggregate output-token budget across all Claude turns in a single run."""
    return _env_int("AGENT_MAX_TOTAL_OUTPUT_TOKENS", 8192)


def _inline_dispatch_enabled() -> bool:
    """Whether post_agent_message should also execute the run inline.

    Explicit override via AGENT_INLINE_DISPATCH. Otherwise, when a durable poll worker
    owns the queue we skip inline dispatch to avoid double execution; in single-process
    dev mode (worker disabled) we keep the inline fast path.
    """
    raw = os.environ.get("AGENT_INLINE_DISPATCH")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() not in ("0", "false", "no", "off")
    try:
        from agent.worker import worker_enabled
        return not worker_enabled()
    except Exception:
        return True


async def claim_agent_run(user: AuthedUser, run_id: str, db=None) -> Optional[dict]:
    """Atomically claim a queued (or dead-leased) run for THIS worker.

    firm_id is taken exclusively from the authenticated user context, never from the
    request/Claude. Returns the claimed run doc, or None if another worker already holds
    a live lease or the run is terminal (prevents duplicate execution).
    """
    database = _get_db(db)
    return await database.claim_agent_run(
        run_id=str(run_id).strip(),
        worker_id=WORKER_ID,
        firm_id=str(user.firm_id),
        lease_seconds=agent_lease_seconds(),
    )


async def renew_agent_run_lease(user: AuthedUser, run_id: str, db=None) -> bool:
    """Heartbeat: extend the lease on a run this worker currently holds."""
    database = _get_db(db)
    return await database.renew_agent_run_lease(
        run_id=str(run_id).strip(),
        worker_id=WORKER_ID,
        firm_id=str(user.firm_id),
        lease_seconds=agent_lease_seconds(),
    )


async def claim_next_queued_agent_run(
    user: AuthedUser,
    db=None,
) -> Optional[dict]:
    """Atomically claim the oldest queued run for the user's firm (worker poll)."""
    database = _get_db(db)
    return await database.claim_next_queued_agent_run(
        worker_id=WORKER_ID,
        firm_id=str(user.firm_id),
        lease_seconds=agent_lease_seconds(),
    )


async def recover_stale_agent_runs(db=None) -> dict:
    """Requeue/fail running runs whose lease expired (crash & restart recovery)."""
    database = _get_db(db)
    result = await database.recover_stale_agent_runs(max_attempts=agent_max_attempts())
    # Also reconcile consequential-action approvals stranded in 'executing' by a crash
    # between claim_for_execution and mark_executed/failed (A5). Best-effort: never block
    # run recovery if this fails.
    try:
        ttl = max(300, agent_lease_seconds())
        approvals = await database.recover_stuck_agent_approvals(ttl_seconds=ttl)
        result["approvals_failed"] = approvals.get("failed", 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("Stuck-approval reconciliation skipped: %s", e)
        result["approvals_failed"] = 0
    return result


async def run_is_cancelled(run_id: str, user: Optional[AuthedUser] = None, db=None) -> bool:
    """Server-side cancellation check via persisted status (cross-process authoritative)."""
    import agent.loop as loop
    if loop.is_run_cancelled(run_id):
        return True
    if user is None:
        return False
    try:
        database = _get_db(db)
        run = await database.agent_runs.find_one(
            {"id": str(run_id).strip(), "firm_id": str(user.firm_id)},
            {"_id": 0},
        )
        return bool(run) and run.get("status") == RUN_STATUS_CANCELLED
    except Exception:
        return False


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


async def claim_agent_approval_for_execution(user: AuthedUser, approval_id: str, db=None) -> bool:
    """Atomically claim an approved action for exactly-once server-side execution."""
    import agent.approvals as approvals
    return await approvals.claim_for_execution(user, approval_id, db=db)


async def mark_agent_approval_failed(user: AuthedUser, approval_id: str, error: Optional[str] = None, db=None) -> dict:
    """Mark a claimed approval as failed with a safe message."""
    import agent.approvals as approvals
    return await approvals.mark_approval_failed(user, approval_id, error=error, db=db)
