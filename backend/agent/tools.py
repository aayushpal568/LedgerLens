"""Read-only agent tool implementations for LedgerLens.

Guarantees:
- Every tool handler receives AuthedUser as the first parameter.
- Handlers call backend/services.py directly without HTTP hops.
- Results are strictly sanitized: NO raw OCR/text, NO internal paths, NO secrets.
- All operations are tenant-scoped via services.py and scoped().
"""
from collections import Counter
from typing import Any, Dict, List, Optional

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
    return [
        {
            "id": f["id"],
            "scan_id": f.get("scan_id"),
            "client_id": f.get("client_id"),
            "category": f.get("category"),
            "severity": f.get("severity", "medium"),
            "title": f.get("title"),
            "description": f.get("description"),
            "file_ids": f.get("file_ids", []),
            "filenames": f.get("filenames", []),
            "status": f.get("status", "unreviewed"),
            "review_note": f.get("review_note"),
            "detected_at": f.get("detected_at"),
            "period": f.get("period"),
        }
        for f in raw_findings
    ]


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
        is_high_severity = severity in ("high", "critical")
        is_key_category = category in ("exact_duplicate", "missing_doc", "wrong_period")

        if is_high_severity or (is_key_category and f.get("status") == "unreviewed"):
            important.append({
                "id": f["id"],
                "category": f.get("category"),
                "severity": f.get("severity", "medium"),
                "title": f.get("title"),
                "status": f.get("status", "unreviewed"),
                "filenames": f.get("filenames", []),
            })

    return {
        "scan_id": scan_id,
        "client_id": scan.get("client_id"),
        "client_name": scan.get("client_name"),
        "scan_status": scan.get("status"),
        "total": len(findings),
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
# Registration Helper
# ---------------------------------------------------------------------------
READ_ONLY_TOOLS = [
    Tool(
        name="list_clients",
        description="Return clients belonging to the authenticated user's firm.",
        parameters=LIST_CLIENTS_SCHEMA,
        handler=handle_list_clients,
        read_only=True,
    ),
    Tool(
        name="list_files",
        description="List files uploaded for a client with metadata (without raw contents or internal storage paths).",
        parameters=LIST_FILES_SCHEMA,
        handler=handle_list_files,
        read_only=True,
    ),
    Tool(
        name="list_templates",
        description="Return available checklist templates (system defaults and firm custom templates).",
        parameters=LIST_TEMPLATES_SCHEMA,
        handler=handle_list_templates,
        read_only=True,
    ),
    Tool(
        name="get_scan_status",
        description="Return scan execution status, progress percentage, and summary metrics.",
        parameters=GET_SCAN_STATUS_SCHEMA,
        handler=handle_get_scan_status,
        read_only=True,
    ),
    Tool(
        name="get_findings",
        description="Query tenant-scoped audit findings for a scan with optional category and review status filters.",
        parameters=GET_FINDINGS_SCHEMA,
        handler=handle_get_findings,
        read_only=True,
    ),
    Tool(
        name="summarize_findings",
        description="Generate a structured statistical summary of audit findings for a scan (total, by category, by status, key items).",
        parameters=SUMMARIZE_FINDINGS_SCHEMA,
        handler=handle_summarize_findings,
        read_only=True,
    ),
    Tool(
        name="get_agent_run_status",
        description="Check the current status and execution lifecycle of an agent run.",
        parameters=GET_AGENT_RUN_STATUS_SCHEMA,
        handler=handle_get_agent_run_status,
        read_only=True,
    ),
]


def register_read_only_tools(registry: ToolRegistry) -> None:
    """Register all 7 standard read-only tools into the specified registry."""
    for tool in READ_ONLY_TOOLS:
        if not registry.contains(tool.name):
            registry.register(tool)


# Auto-register standard read-only tools into default_registry
register_read_only_tools(default_registry)
