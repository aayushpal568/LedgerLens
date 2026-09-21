"""Agent action approval engine for LedgerLens.

Guarantees:
- Action tools require explicit human approval before mutating data.
- 10-minute TTL expiry enforced strictly.
- SHA-256 canonical hashing of proposed arguments guarantees anti-tampering.
- Multi-tenancy: approvals are strictly scoped to the authenticated user's firm.
- One-time execution enforced SERVER-SIDE via an atomic status compare-and-swap
  (approved -> executing -> executed/failed), so a double approval, a concurrent resume,
  or a recovered/stale worker can never execute the same consequential action twice.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from auth_dep import AuthedUser
from db_access import scoped
import services

logger = logging.getLogger(__name__)

# Approval statuses
APPROVAL_STATUS_PENDING = "pending"
APPROVAL_STATUS_APPROVED = "approved"
APPROVAL_STATUS_REJECTED = "rejected"
APPROVAL_STATUS_EXECUTING = "executing"  # internal: an executor holds the atomic claim
APPROVAL_STATUS_EXECUTED = "executed"
APPROVAL_STATUS_FAILED = "failed"
APPROVAL_STATUS_EXPIRED = "expired"

VALID_APPROVAL_STATUSES = {
    APPROVAL_STATUS_PENDING,
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_REJECTED,
    APPROVAL_STATUS_EXECUTING,
    APPROVAL_STATUS_EXECUTED,
    APPROVAL_STATUS_FAILED,
    APPROVAL_STATUS_EXPIRED,
}

# 10 minutes time-to-live
APPROVAL_TTL_SECONDS = 600


def compute_args_hash(arguments: Optional[Dict[str, Any]]) -> str:
    """Compute deterministic SHA-256 hash of canonical JSON arguments."""
    payload = arguments if arguments is not None else {}
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _is_expired(doc: dict) -> bool:
    """Check whether a pending approval has passed its expiry timestamp."""
    expires_at_str = doc.get("expires_at")
    if not expires_at_str:
        return False
    try:
        expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return now >= expires_at
    except Exception:
        return False


async def create_approval(
    user: AuthedUser,
    run_id: str,
    thread_id: str,
    tool_name: str,
    proposed_args: Dict[str, Any],
    db=None,
) -> dict:
    """Create a new pending approval record scoped to the user's firm."""
    database = services._get_db(db)
    now_dt = datetime.now(timezone.utc)
    expires_dt = now_dt + timedelta(seconds=APPROVAL_TTL_SECONDS)

    approval_id = services.new_id()
    args_hash = compute_args_hash(proposed_args)

    doc = {
        "id": approval_id,
        "firm_id": user.firm_id,
        "run_id": run_id,
        "thread_id": thread_id,
        "user_id": user.user_id,
        "tool_name": tool_name,
        "proposed_args": proposed_args or {},
        "args_hash": args_hash,
        "status": APPROVAL_STATUS_PENDING,
        "created_at": now_dt.isoformat(),
        "expires_at": expires_dt.isoformat(),
        "approved_at": None,
        "rejected_at": None,
        "executed_at": None,
    }

    await database.agent_approvals.insert_one(dict(doc))
    return services.clean(doc)


async def get_approval(user: AuthedUser, approval_id: str, db=None) -> dict:
    """Retrieve an approval record by ID, strictly scoped to the user's firm."""
    database = services._get_db(db)
    filt = scoped(database.agent_approvals, user, {"id": approval_id})
    doc = await database.agent_approvals.find_one(filt, {"_id": 0})
    if not doc:
        raise HTTPException(404, f"Approval '{approval_id}' not found")

    # Check for TTL expiry on read
    if doc.get("status") == APPROVAL_STATUS_PENDING and _is_expired(doc):
        doc["status"] = APPROVAL_STATUS_EXPIRED
        await database.agent_approvals.update_one(filt, {"$set": {"status": APPROVAL_STATUS_EXPIRED}})

    return services.clean(doc)


async def list_approvals(
    user: AuthedUser,
    run_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    db=None,
) -> List[dict]:
    """List approval records for the authenticated firm with optional filters."""
    database = services._get_db(db)
    query: Dict[str, Any] = {}
    if run_id:
        query["run_id"] = run_id
    if thread_id:
        query["thread_id"] = thread_id
    if status:
        query["status"] = status

    filt = scoped(database.agent_approvals, user, query)
    cursor = database.agent_approvals.find(filt, {"_id": 0}).sort("created_at", -1)
    raw_docs = await cursor.to_list(limit)

    results = []
    for doc in raw_docs:
        if doc.get("status") == APPROVAL_STATUS_PENDING and _is_expired(doc):
            doc["status"] = APPROVAL_STATUS_EXPIRED
            filt_doc = scoped(database.agent_approvals, user, {"id": doc["id"]})
            await database.agent_approvals.update_one(filt_doc, {"$set": {"status": APPROVAL_STATUS_EXPIRED}})
        results.append(services.clean(doc))

    return results


async def approve_approval(user: AuthedUser, approval_id: str, db=None) -> dict:
    """Mark an approval approved after validating TTL and arguments integrity."""
    approval = await get_approval(user, approval_id, db=db)
    if approval.get("status") == APPROVAL_STATUS_EXPIRED or _is_expired(approval):
        raise HTTPException(400, "Approval has expired (10-minute TTL exceeded).")
    if approval.get("status") != APPROVAL_STATUS_PENDING:
        raise HTTPException(
            400, f"Cannot approve action with status '{approval.get('status')}'. Must be 'pending'."
        )

    # Verify argument integrity
    stored_hash = approval.get("args_hash")
    computed_hash = compute_args_hash(approval.get("proposed_args"))
    if stored_hash != computed_hash:
        raise HTTPException(400, "Security violation: proposed arguments hash mismatch.")

    t_now = services.now_iso()
    database = services._get_db(db)
    # Atomic pending -> approved so two concurrent approvals cannot both take effect.
    ok = await database.cas_agent_approval(
        approval_id,
        str(user.firm_id),
        [APPROVAL_STATUS_PENDING],
        APPROVAL_STATUS_APPROVED,
        {"approved_at": t_now, "approved_by": user.user_id},
    )
    if not ok:
        fresh = await get_approval(user, approval_id, db=db)
        if fresh.get("status") == APPROVAL_STATUS_APPROVED:
            return fresh  # idempotent: already approved by a racing request
        raise HTTPException(
            400, f"Cannot approve action with status '{fresh.get('status')}'. Must be 'pending'."
        )
    return await get_approval(user, approval_id, db=db)


async def reject_approval(user: AuthedUser, approval_id: str, reason: Optional[str] = None, db=None) -> dict:
    """Mark an approval rejected."""
    approval = await get_approval(user, approval_id, db=db)
    if approval.get("status") == APPROVAL_STATUS_EXPIRED or _is_expired(approval):
        raise HTTPException(400, "Approval has expired (10-minute TTL exceeded).")
    if approval.get("status") != APPROVAL_STATUS_PENDING:
        raise HTTPException(
            400, f"Cannot reject action with status '{approval.get('status')}'. Must be 'pending'."
        )

    t_now = services.now_iso()
    database = services._get_db(db)
    ok = await database.cas_agent_approval(
        approval_id,
        str(user.firm_id),
        [APPROVAL_STATUS_PENDING],
        APPROVAL_STATUS_REJECTED,
        {"rejected_at": t_now, "rejected_by": user.user_id, "rejection_reason": reason or "Rejected by user"},
    )
    if not ok:
        fresh = await get_approval(user, approval_id, db=db)
        if fresh.get("status") == APPROVAL_STATUS_REJECTED:
            return fresh  # idempotent: already rejected by a racing request
        raise HTTPException(
            400, f"Cannot reject action with status '{fresh.get('status')}'. Must be 'pending'."
        )
    return await get_approval(user, approval_id, db=db)


async def claim_for_execution(user: AuthedUser, approval_id: str, db=None) -> bool:
    """Atomically claim an APPROVED approval for single execution.

    Returns True ONLY for the caller that wins the approved -> executing transition. Any
    concurrent/duplicate/stale attempt sees a non-approved status and returns False, so the
    consequential tool is executed at most once.
    """
    database = services._get_db(db)
    return await database.cas_agent_approval(
        approval_id,
        str(user.firm_id),
        [APPROVAL_STATUS_APPROVED],
        APPROVAL_STATUS_EXECUTING,
        {"execution_claimed_at": services.now_iso()},
    )


async def mark_approval_executed(user: AuthedUser, approval_id: str, db=None) -> dict:
    """Finalize a claimed approval as executed (atomic executing/approved -> executed)."""
    database = services._get_db(db)
    t_now = services.now_iso()
    await database.cas_agent_approval(
        approval_id,
        str(user.firm_id),
        [APPROVAL_STATUS_EXECUTING, APPROVAL_STATUS_APPROVED],
        APPROVAL_STATUS_EXECUTED,
        {"executed_at": t_now},
    )
    return await get_approval(user, approval_id, db=db)


async def mark_approval_failed(user: AuthedUser, approval_id: str, error: Optional[str] = None, db=None) -> dict:
    """Mark a claimed approval as failed (atomic executing -> failed) with a safe message."""
    database = services._get_db(db)
    t_now = services.now_iso()
    await database.cas_agent_approval(
        approval_id,
        str(user.firm_id),
        [APPROVAL_STATUS_EXECUTING],
        APPROVAL_STATUS_FAILED,
        {"failed_at": t_now, "execution_error": (str(error)[:500] if error else "Execution failed")},
    )
    return await get_approval(user, approval_id, db=db)
