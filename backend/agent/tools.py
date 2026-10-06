"""Read-only agent tool implementations for LedgerLens.

Guarantees:
- Every tool handler receives AuthedUser as the first parameter.
- Handlers call backend/services.py directly without HTTP hops.
- Results are strictly sanitized: NO raw OCR/text, NO internal paths, NO secrets.
- All operations are tenant-scoped via services.py and scoped().
"""
import asyncio
import base64
import os
from collections import Counter
from typing import Any, Dict, List, Optional

import storage
from auth_dep import AuthedUser
import services
from agent.registry import Tool, ToolRegistry, default_registry


# ---------------------------------------------------------------------------
# 1. list_clients
# ---------------------------------------------------------------------------
LIST_CLIENTS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


async def handle_list_clients(user: AuthedUser, args: Dict[str, Any], db=None) -> List[Dict[str, Any]]:
    """Return clients belonging to the authenticated user's firm."""
    raw_clients = await services.list_clients(user, db=db)
    return [
        {
            "id": c["id"],
            "name": c.get("name"),
            "client_type": c.get("client_type"),
            "notes": c.get("notes", ""),
            "created_at": c.get("created_at"),
        }
        for c in raw_clients
    ]


# ---------------------------------------------------------------------------
# 2. list_files
# ---------------------------------------------------------------------------
LIST_FILES_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_id": {
            "type": "string",
            "description": "Unique identifier of the client whose files should be listed.",
        },
    },
    "required": ["client_id"],
    "additionalProperties": False,
}


async def handle_list_files(user: AuthedUser, args: Dict[str, Any], db=None) -> List[Dict[str, Any]]:
    """Return files belonging to that client, stripped of filesystem paths and OCR."""
    client_id = str(args["client_id"]).strip()
    raw_files = await services.list_files(user, client_id, db=db)
    result = []
    for f in raw_files:
        fname = f.get("filename") or f.get("name") or "unnamed"
        ftype = f.get("file_type") or f.get("ext") or (fname.rsplit(".", 1)[-1] if "." in fname else "")
        result.append({
            "id": f["id"],
            "client_id": f.get("client_id"),
            "filename": fname,
            "size": f.get("size", 0),
            "created_at": f.get("created_at") or f.get("uploaded_at"),
            "file_type": ftype,
            "period": f.get("period"),
        })
    return result


# ---------------------------------------------------------------------------
# 3. list_templates
# ---------------------------------------------------------------------------
LIST_TEMPLATES_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


async def handle_list_templates(user: AuthedUser, args: Dict[str, Any], db=None) -> List[Dict[str, Any]]:
    """Return available checklist templates for the user's firm."""
    raw_templates = await services.list_templates(user, db=db)
    return [
        {
            "id": t["id"],
            "name": t.get("name"),
            "client_type": t.get("client_type"),
            "description": t.get("description", ""),
            "is_default": t.get("is_default", False),
            "items": [
                {
                    "name": item.get("name") if isinstance(item, dict) else str(item),
                    "aliases": item.get("aliases", []) if isinstance(item, dict) else [],
                    "required": item.get("required", True) if isinstance(item, dict) else True,
                }
                for item in t.get("items", [])
            ],
        }
        for t in raw_templates
    ]


# ---------------------------------------------------------------------------
# 4. get_scan_status
# ---------------------------------------------------------------------------
GET_SCAN_STATUS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "scan_id": {
            "type": "string",
            "description": "Unique identifier of the scan to inspect.",
        },
    },
    "required": ["scan_id"],
    "additionalProperties": False,
}


async def handle_get_scan_status(user: AuthedUser, args: Dict[str, Any], db=None) -> Dict[str, Any]:
    """Return scan status, progress, and execution summary."""
    scan_id = str(args["scan_id"]).strip()
    scan = await services.get_scan(user, scan_id, db=db)
    return {
        "id": scan["id"],
        "client_id": scan.get("client_id"),
        "client_name": scan.get("client_name"),
        "status": scan.get("status"),
        "progress": scan.get("progress", 0),
        "processed_files": scan.get("processed_files", 0),
        "total_files": scan.get("total_files", 0),
        "total_findings": scan.get("total_findings", 0),
        "expected_period": scan.get("expected_period"),
        "started_at": scan.get("started_at"),
        "completed_at": scan.get("completed_at"),
        "error": scan.get("error"),
        "summary": scan.get("summary", {}),
    }


# ---------------------------------------------------------------------------
# 5. get_findings
# ---------------------------------------------------------------------------
GET_FINDINGS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "scan_id": {
            "type": "string",
            "description": "Optional scan ID to filter findings.",
        },
        "category": {
            "type": "string",
            "description": "Optional category filter (e.g., exact_duplicate, possible_duplicate, missing_doc, wrong_period).",
        },
        "status": {
            "type": "string",
            "description": "Optional review status filter (e.g., unreviewed, keep, ignore).",
        },
    },
    "additionalProperties": False,
}


async def handle_get_findings(user: AuthedUser, args: Dict[str, Any], db=None) -> List[Dict[str, Any]]:
    """Return tenant-scoped findings without raw OCR text or filesystem paths."""
    scan_id = args.get("scan_id")
    category = args.get("category")
    status = args.get("status")

    raw_findings = await services.get_findings(
        user,
        scan_id=scan_id.strip() if isinstance(scan_id, str) and scan_id.strip() else None,
        category=category.strip() if isinstance(category, str) and category.strip() else None,
        status=status.strip() if isinstance(status, str) and status.strip() else None,
        db=db,
    )
    result = []
    for f in raw_findings:
        # Canonical file extraction from files list or explicit keys
        raw_files = f.get("files") or []
        file_ids = f.get("file_ids") or [file_info.get("file_id") for file_info in raw_files if isinstance(file_info, dict) and file_info.get("file_id")]
        filenames = f.get("filenames") or [file_info.get("name") for file_info in raw_files if isinstance(file_info, dict) and file_info.get("name")]

        result.append({
            "id": f["id"],
            "scan_id": f.get("scan_id"),
            "client_id": f.get("client_id"),
            "category": f.get("category"),
            "severity": f.get("severity", "medium"),
            "confidence": f.get("confidence", 80),
            "confidence_level": f.get("confidence_level", "medium"),
            "title": f.get("title"),
            "description": f.get("description") or (f.get("evidence", {}).get("summary") if isinstance(f.get("evidence"), dict) else None),
            "evidence": f.get("evidence", {}),
            "file_ids": file_ids,
            "filenames": filenames,
            "status": f.get("status", "unreviewed"),
            "note": f.get("note") or f.get("review_note") or "",
            "review_note": f.get("review_note") or f.get("note") or "",
            "detected_at": f.get("detected_at") or f.get("created_at"),
            "period": f.get("period"),
        })
    return result


# ---------------------------------------------------------------------------
# 6. summarize_findings
# ---------------------------------------------------------------------------
SUMMARIZE_FINDINGS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "scan_id": {
            "type": "string",
            "description": "Unique identifier of the scan whose findings will be summarized.",
        },
    },
    "required": ["scan_id"],
    "additionalProperties": False,
}


async def handle_summarize_findings(user: AuthedUser, args: Dict[str, Any], db=None) -> Dict[str, Any]:
    """Generate a structured findings summary from actual database records (no LLM, no hallucinations)."""
    scan_id = str(args["scan_id"]).strip()

    # Validate scan exists and is scoped to user's firm
    scan = await services.get_scan(user, scan_id, db=db)

    # Fetch all findings for this scan
    findings = await services.get_findings(user, scan_id=scan_id, db=db)

    category_counter = Counter(f.get("category", "unknown") for f in findings)
    status_counter = Counter(f.get("status", "unreviewed") for f in findings)

    # Identify important findings (high/critical severity, duplicates, missing documents)
    important = []
    for f in findings:
        severity = str(f.get("severity", "")).lower()
        category = str(f.get("category", "")).lower()
        is_high_severity = severity in ("high", "critical") or f.get("confidence_level") == "high"
        is_key_category = category in ("exact_duplicate", "missing_doc", "wrong_period")

        raw_files = f.get("files") or []
        file_ids = f.get("file_ids") or [fi.get("file_id") for fi in raw_files if isinstance(fi, dict) and fi.get("file_id")]
        filenames = f.get("filenames") or [fi.get("name") for fi in raw_files if isinstance(fi, dict) and fi.get("name")]

        if is_high_severity or (is_key_category and f.get("status") == "unreviewed"):
            important.append({
                "id": f["id"],
                "category": f.get("category"),
                "severity": f.get("severity", "medium"),
                "confidence": f.get("confidence", 80),
                "title": f.get("title"),
                "status": f.get("status", "unreviewed"),
                "file_ids": file_ids,
                "filenames": filenames,
                "evidence": f.get("evidence", {}),
            })

    return {
        "scan_id": scan_id,
        "client_id": scan.get("client_id"),
        "client_name": scan.get("client_name"),
        "scan_status": scan.get("status"),
        "total": len(findings),
        "total_findings": len(findings),
        "by_category": dict(category_counter),
        "by_status": dict(status_counter),
        "important_findings": important[:15],
    }



# ---------------------------------------------------------------------------
# 7. get_agent_run_status
# ---------------------------------------------------------------------------
GET_AGENT_RUN_STATUS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "run_id": {
            "type": "string",
            "description": "Unique identifier of the agent run.",
        },
    },
    "required": ["run_id"],
    "additionalProperties": False,
}


async def handle_get_agent_run_status(user: AuthedUser, args: Dict[str, Any], db=None) -> Dict[str, Any]:
    """Return the current status and execution metadata of an agent run."""
    run_id = str(args["run_id"]).strip()
    run = await services.get_agent_run(user, run_id, db=db)
    return {
        "id": run["id"],
        "thread_id": run.get("thread_id"),
        "status": run.get("status"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "error": run.get("error"),
    }


# ---------------------------------------------------------------------------
# 8. export_report (Read-only tool returning secure download reference)
# ---------------------------------------------------------------------------
EXPORT_REPORT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "scan_id": {
            "type": "string",
            "description": "Unique identifier of the completed scan to export.",
        },
        "format": {
            "type": "string",
            "enum": ["csv", "xlsx", "pdf", "CSV", "XLSX", "PDF"],
            "description": "Export format: 'csv', 'xlsx', or 'pdf'. Defaults to 'csv'.",
        },
    },
    "required": ["scan_id"],
    "additionalProperties": False,
}


async def handle_export_report(user: AuthedUser, args: Dict[str, Any], db=None) -> Dict[str, Any]:
    """Generate a formatted report export reference without exposing filesystem paths."""
    scan_id = str(args["scan_id"]).strip()
    fmt = str(args.get("format") or "csv").strip().lower()
    if fmt not in ("csv", "xlsx", "pdf"):
        fmt = "csv"

    # Calls existing report service; verifies tenant ownership
    report_res = await services.export_report(user, scan_id=scan_id, format=fmt, db=db)
    return {
        "scan_id": scan_id,
        "format": fmt,
        "filename": report_res["filename"],
        "media_type": report_res["media_type"],
        "download_url": f"/api/scans/{scan_id}/report?format={fmt}",
        "status": "ready",
    }


# ---------------------------------------------------------------------------
# 9. run_scan (Action Tool - requires approval)
# ---------------------------------------------------------------------------

RUN_SCAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_id": {
            "type": "string",
            "description": "Unique identifier of the client to audit.",
        },
        "template_id": {
            "type": "string",
            "description": "Unique identifier of the checklist template to scan against.",
        },
        "expected_period": {
            "type": "string",
            "description": "Optional period identifier, e.g. '2024' or 'Q1 2024'.",
        },
    },
    "required": ["client_id", "template_id"],
    "additionalProperties": False,
}


async def handle_run_scan(user: AuthedUser, args: Dict[str, Any], db=None) -> Dict[str, Any]:
    """Initiate an automated document audit scan for a client against a checklist template."""
    client_id = str(args["client_id"]).strip()
    template_id = str(args["template_id"]).strip()
    raw_period = args.get("expected_period")
    period_arg = int(raw_period) if (raw_period and str(raw_period).isdigit()) else raw_period

    # Duplicate-execution guard: reuse an identical scan for this client that is still active.
    try:
        existing_scans = await services.list_scans(user, client_id, db=db)
    except Exception:  # noqa: BLE001
        existing_scans = []
    for s in existing_scans:
        same_template = (s.get("template_id") or None) == (template_id or None)
        same_period = str(s.get("expected_period") or "") == str(raw_period or "")
        if same_template and same_period and s.get("status") in ("queued", "scanning", "cancelling"):
            return {
                "scan_id": s["id"],
                "client_id": client_id,
                "client_name": s.get("client_name"),
                "template_id": template_id,
                "status": s.get("status"),
                "reused_active": True,
                "message": "An identical audit scan is already in progress; reused its reference.",
            }

    scan = await services.start_scan(user, client_id, template_id=template_id, expected_period=period_arg, db=db)
    return {
        "scan_id": scan["id"],
        "client_id": client_id,
        "client_name": scan.get("client_name"),
        "template_id": template_id,
        "status": scan.get("status", "queued"),
        "reused_active": False,
        "message": "Audit scan initiated successfully.",
    }


# ---------------------------------------------------------------------------
# 9. create_client (Action Tool - requires approval)
# ---------------------------------------------------------------------------
CREATE_CLIENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Name of the new client organization or individual.",
        },
        "tax_id": {
            "type": "string",
            "description": "Optional tax identification number or EIN.",
        },
        "notes": {
            "type": "string",
            "description": "Optional notes or description for the client.",
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}


async def handle_create_client(user: AuthedUser, args: Dict[str, Any], db=None) -> Dict[str, Any]:
    """Create a new client entity in the authenticated accounting firm."""
    name = str(args["name"]).strip()
    tax_id = str(args["tax_id"]).strip() if args.get("tax_id") else None
    notes = str(args.get("notes") or "").strip()
    client = await services.create_client(user, name=name, notes=notes, tax_id=tax_id, db=db)
    return {
        "client_id": client["id"],
        "name": client["name"],
        "tax_id": client.get("tax_id"),
        "notes": client.get("notes", ""),
        "created_at": client.get("created_at"),
        "message": f"Client '{client['name']}' created successfully.",
    }


# ---------------------------------------------------------------------------
# 10. set_finding_review (Action Tool - requires approval)
# ---------------------------------------------------------------------------
# Review states MUST use the application's existing valid finding statuses
# (see server.FindingUpdate / services.update_finding): do NOT invent a parallel system.
VALID_REVIEW_STATUSES = ["keep", "keep_both", "ignore", "review_later", "unreviewed"]

SET_FINDING_REVIEW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "finding_id": {
            "type": "string",
            "description": "Unique identifier of the finding to review.",
        },
        "review_status": {
            "type": "string",
            "enum": VALID_REVIEW_STATUSES,
            "description": (
                "Review disposition using LedgerLens' existing finding statuses: "
                "'keep' (valid exception), 'keep_both' (keep both duplicates), "
                "'ignore' (false positive), 'review_later', or 'unreviewed'."
            ),
        },
        "review_notes": {
            "type": "string",
            "description": "Optional notes or rationale for the review decision.",
        },
    },
    "required": ["finding_id", "review_status"],
    "additionalProperties": False,
}


async def handle_set_finding_review(user: AuthedUser, args: Dict[str, Any], db=None) -> Dict[str, Any]:
    """Update the review status/note for a finding using the existing finding service.

    Existence + firm ownership are enforced by the tenant-scoped services.update_finding
    (raises 404 when the finding is missing or belongs to another firm).
    """
    finding_id = str(args["finding_id"]).strip()
    review_status = str(args["review_status"]).strip()
    if review_status not in VALID_REVIEW_STATUSES:
        raise ValueError(
            f"Invalid review_status '{review_status}'. Allowed: {VALID_REVIEW_STATUSES}."
        )
    review_notes = str(args.get("review_notes") or "").strip()
    updated = await services.update_finding(
        user,
        finding_id,
        {
            "status": review_status,
            "review_status": review_status,
            "note": review_notes,
            "review_note": review_notes,
        },
        db=db,
    )
    return {
        "finding_id": finding_id,
        "review_status": review_status,
        "status": updated.get("status"),
        "review_notes": review_notes,
        "message": f"Finding review status set to '{review_status}'.",
    }


# ---------------------------------------------------------------------------
# 11. analyze_document — send the firm's OWN single document to Claude as image input
# ---------------------------------------------------------------------------
ANALYZE_DOCUMENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_id": {"type": "string", "description": "ID of a file belonging to this firm to analyze."},
        "question": {"type": "string", "description": "What to extract or assess from the document (optional)."},
    },
    "required": ["file_id"],
    "additionalProperties": False,
}

_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "bmp", "tiff", "tif"}
_DOC_CLAUDE_MAX_BYTES = int(os.environ.get("DOC_CLAUDE_MAX_BYTES", str(20 * 1024 * 1024)))   # 20 MB input cap
_DOC_CLAUDE_MAX_PAGES = int(os.environ.get("DOC_CLAUDE_MAX_PAGES", "8"))                      # PDF pages sent to Claude
_DOC_CLAUDE_MAX_B64 = int(os.environ.get("DOC_CLAUDE_MAX_B64", str(12 * 1024 * 1024)))        # base64 payload cap


def _build_document_images(data: bytes, ext: str) -> "list[tuple[str,str]]":
    """Return list[(mime, base64)] image parts for Claude. Images pass through; PDFs render
    to PNG up to a page/byte cap. Raises ValueError on unsupported type or oversize input."""
    ext = (ext or "").lower().lstrip(".")
    if len(data) > _DOC_CLAUDE_MAX_BYTES:
        raise ValueError("Document too large to analyze.")
    if ext in _IMAGE_EXTS:
        return [(storage.mime_for(ext) if ext != "tif" else "image/tiff", base64.b64encode(data).decode())]
    if ext == "pdf":
        import fitz  # PyMuPDF — same renderer used by the OCR path
        images: "list[tuple[str,str]]" = []
        total = 0
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            for i in range(min(len(doc), _DOC_CLAUDE_MAX_PAGES)):
                pix = doc.load_page(i).get_pixmap(dpi=130)
                png = pix.tobytes("png")
                b64 = base64.b64encode(png).decode()
                total += len(b64)
                if total > _DOC_CLAUDE_MAX_B64:
                    break
                images.append(("image/png", b64))
        finally:
            doc.close()
        if not images:
            raise ValueError("No pages to analyze.")
        return images
    raise ValueError("analyze_document supports image and PDF documents only.")


async def handle_analyze_document(user: AuthedUser, args: Dict[str, Any], db=None) -> Dict[str, Any]:
    """Analyze ONE of the authenticated firm's documents by sending it to Claude as image input.

    Tenant-scoped (404 if the file is not this firm's). Sends only the requested file. Keeps the
    deterministic engine and PaddleOCR intact; this is an additional AI path. Returns no paths/secrets.
    """
    from fastapi import HTTPException
    file_id = str(args.get("file_id", "")).strip()
    question = str(args.get("question") or "Summarize this document and list key accounting line items, dates, and amounts.").strip()
    if not file_id:
        raise HTTPException(400, "file_id is required")

    database = services._get_db(db)
    from db_access import scoped
    doc = await database.files.find_one(scoped(database.files, user, {"id": file_id}))  # firm-scoped; else None
    if not doc:
        raise HTTPException(404, "File not found")  # also blocks other-firm files
    sp = doc.get("storage_path")
    ext = doc.get("ext") or ""
    if not sp:
        raise HTTPException(400, "File has no stored content")

    data = await asyncio.to_thread(storage.get_object, sp)
    try:
        images = _build_document_images(data, ext)
    except ValueError as ve:
        raise HTTPException(400, str(ve))

    from engine.providers.llm import ClaudeOpusFalProvider
    provider = ClaudeOpusFalProvider()
    if not provider.available:
        return {"file_id": file_id, "analysis": "", "note": "AI provider not configured (FAL_KEY missing)."}
    analysis = await asyncio.to_thread(provider.generate_multimodal, question, images, None, 1024)
    return {
        "file_id": file_id,
        "filename": doc.get("name"),
        "pages_sent": len(images),
        "analysis": (analysis or "").strip(),
        "note": "" if analysis else "AI provider returned no content.",
    }


# ---------------------------------------------------------------------------
# Tool Lists & Registration Helpers
# ---------------------------------------------------------------------------
READ_ONLY_TOOLS = [
    Tool(
        name="list_clients",
        description="Return clients belonging to the authenticated user's firm.",
        parameters=LIST_CLIENTS_SCHEMA,
        handler=handle_list_clients,
        read_only=True,
        approval_required=False,
    ),
    Tool(
        name="list_files",
        description="List files uploaded for a client with metadata (without raw contents or internal storage paths).",
        parameters=LIST_FILES_SCHEMA,
        handler=handle_list_files,
        read_only=True,
        approval_required=False,
    ),
    Tool(
        name="list_templates",
        description="Return available checklist templates (system defaults and firm custom templates).",
        parameters=LIST_TEMPLATES_SCHEMA,
        handler=handle_list_templates,
        read_only=True,
        approval_required=False,
    ),
    Tool(
        name="get_scan_status",
        description="Return scan execution status, progress percentage, and summary metrics.",
        parameters=GET_SCAN_STATUS_SCHEMA,
        handler=handle_get_scan_status,
        read_only=True,
        approval_required=False,
    ),
    Tool(
        name="get_findings",
        description="Query tenant-scoped audit findings for a scan with optional category and review status filters.",
        parameters=GET_FINDINGS_SCHEMA,
        handler=handle_get_findings,
        read_only=True,
        approval_required=False,
    ),
    Tool(
        name="summarize_findings",
        description="Generate a structured statistical summary of audit findings for a scan (total, by category, by status, key items).",
        parameters=SUMMARIZE_FINDINGS_SCHEMA,
        handler=handle_summarize_findings,
        read_only=True,
        approval_required=False,
    ),
    Tool(
        name="get_agent_run_status",
        description="Check the current status and execution lifecycle of an agent run.",
        parameters=GET_AGENT_RUN_STATUS_SCHEMA,
        handler=handle_get_agent_run_status,
        read_only=True,
        approval_required=False,
    ),
    Tool(
        name="export_report",
        description="Generate a formatted export report (CSV, XLSX, or PDF) for a completed audit scan and return a secure download link.",
        parameters=EXPORT_REPORT_SCHEMA,
        handler=handle_export_report,
        read_only=True,
        approval_required=False,
    ),
    Tool(
        name="analyze_document",
        description=(
            "Analyze a single document (image or PDF) that belongs to this firm by sending the actual "
            "file to Claude for vision-based analysis. Returns Claude's analysis text. Use only for the "
            "specific file the user asks about; never bulk-analyzes. Does not require human approval (read-only)."
        ),
        parameters=ANALYZE_DOCUMENT_SCHEMA,
        handler=handle_analyze_document,
        read_only=True,
        approval_required=False,
    ),
]


ACTION_TOOLS = [
    Tool(
        name="run_scan",
        description="Initiate an automated document audit scan for a client against a checklist template. (Requires human approval)",
        parameters=RUN_SCAN_SCHEMA,
        handler=handle_run_scan,
        read_only=False,
        approval_required=True,
    ),
    Tool(
        name="create_client",
        description="Create a new client entity in the authenticated accounting firm. (Requires human approval)",
        parameters=CREATE_CLIENT_SCHEMA,
        handler=handle_create_client,
        read_only=False,
        approval_required=True,
    ),
    Tool(
        name="set_finding_review",
        description="Update the review status disposition and audit notes for a finding. (Requires human approval)",
        parameters=SET_FINDING_REVIEW_SCHEMA,
        handler=handle_set_finding_review,
        read_only=False,
        approval_required=True,
    ),
]

ALL_TOOLS = READ_ONLY_TOOLS + ACTION_TOOLS


def register_read_only_tools(registry: ToolRegistry) -> None:
    """Register all standard read-only tools into the specified registry."""
    for tool in READ_ONLY_TOOLS:
        if not registry.contains(tool.name):
            registry.register(tool)


def register_action_tools(registry: ToolRegistry) -> None:
    """Register all standard action tools into the specified registry."""
    for tool in ACTION_TOOLS:
        if not registry.contains(tool.name):
            registry.register(tool)


def register_all_tools(registry: ToolRegistry) -> None:
    """Register both read-only and action tools into the specified registry."""
    register_read_only_tools(registry)
    register_action_tools(registry)


# Auto-register all tools into default_registry
register_all_tools(default_registry)
