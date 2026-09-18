"""Tenant isolation and database query scoping for LedgerLens Cloud SaaS.

Enforces fail-closed multi-tenancy rules:
- firm_id must ALWAYS be present in user context; if missing, raises ValueError.
- Automatically injects user.firm_id into all collection filters.
- Rejects any query attempting to override or mismatch user.firm_id with PermissionError.
"""
from typing import Any, Dict, Optional

from auth_dep import AuthedUser


def scoped(collection: Any, user: AuthedUser, filt: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Scope a collection query filter to the authenticated user's firm.

    Fails closed:
    - Raises ValueError if user or user.firm_id is missing or empty.
    - Raises PermissionError if filter contains an explicit firm_id that does not match user.firm_id.
    - Returns a new filter dictionary containing {"firm_id": user.firm_id}.
    """
    if not user or not getattr(user, "firm_id", None) or not str(user.firm_id).strip():
        raise ValueError(
            "Security Violation: firm_id must always be present and non-empty in user context. "
            "Refusing to execute un-scoped multi-tenant database query."
        )

    clean_firm_id = str(user.firm_id).strip()
    result = dict(filt or {})

    # Check for cross-tenant tampering in explicit filter
    if "firm_id" in result:
        existing_firm = result["firm_id"]
        if existing_firm != clean_firm_id:
            raise PermissionError(
                f"Cross-Tenant Security Violation: Query requested firm_id='{existing_firm}', "
                f"but authenticated user belongs to firm_id='{clean_firm_id}'."
            )

    result["firm_id"] = clean_firm_id
    return result
