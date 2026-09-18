"""Authentication API endpoints for LedgerLens Cloud SaaS.

Implements /api/auth:
- POST /signup: atomic firm + user creation with seeded templates; 409 on duplicate.
- POST /login: bcrypt verify, IP + email throttling (429 on 6th attempt), constant-time dummy verification.
- POST /refresh: opaque refresh token validation and rotation.
- POST /logout: token_version revocation and refresh clearing.
- GET /me: authenticated user profile and firm context.
"""
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import logging
import re
import threading
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, EmailStr

from auth_dep import AuthedUser, get_current_user
from engine import default_templates
from passwords import hash_password, verify_password, verify_dummy_password
from tokens import (
    create_access_token,
    create_refresh_token,
    hash_refresh_token,
    ACCESS_TOKEN_EXPIRE_MINUTES,
)

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# In-Memory Login Rate Limiter (Throttling)
# ---------------------------------------------------------------------------
class LoginRateLimiter:
    """Thread-safe in-memory rate limiter tracking failed login attempts per (email, IP)."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 900):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._failures = defaultdict(list)
        self._lock = threading.Lock()

    def is_throttled(self, key: str) -> bool:
        with self._lock:
            now = time.time()
            # Clean expired timestamps
            attempts = [t for t in self._failures[key] if now - t < self.window_seconds]
            self._failures[key] = attempts
            return len(attempts) >= self.max_attempts

    def record_failure(self, key: str):
        with self._lock:
            now = time.time()
            attempts = [t for t in self._failures[key] if now - t < self.window_seconds]
            attempts.append(now)
            self._failures[key] = attempts

    def reset(self, key: str):
        with self._lock:
            self._failures.pop(key, None)


login_limiter = LoginRateLimiter(max_attempts=5, window_seconds=900)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, description="Password must be at least 8 characters")
    firm_name: str = Field(..., min_length=1, max_length=200)
    name: Optional[str] = Field(default="", max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _client_ip(request: Request) -> str:
    """Extract client IP address safely from request."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _throttle_key(email: str, ip: str) -> str:
    return f"{email.lower().strip()}:{ip}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@auth_router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(body: SignupRequest, request: Request):
    """Register a new accounting firm and root administrator user atomically."""
    from server import db

    if db is None:
        raise HTTPException(status_code=500, detail="Database not initialized")

    email_clean = body.email.lower().strip()
    if not body.firm_name.strip():
        raise HTTPException(status_code=400, detail="Firm name cannot be empty")

    firm_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    now = _now_iso()

    # Create firm record
    firm_doc = {
        "id": firm_id,
        "name": body.firm_name.strip(),
        "contact_email": email_clean,
        "retention_note": "Documents are securely processed in isolated cloud environments.",
        "settings": {"privacy_mode": True},
        "created_at": now,
    }

    # Hash password with bcrypt cost 12
    pwd_hash = hash_password(body.password)

    # Generate initial refresh token
    raw_refresh, refresh_hash, refresh_expires_at = create_refresh_token()

    # Create user record
    user_doc = {
        "id": user_id,
        "email": email_clean,
        "password_hash": pwd_hash,
        "name": (body.name or "").strip(),
        "firm_id": firm_id,
        "created_at": now,
        "token_version": 1,
        "refresh_hash": refresh_hash,
        "refresh_expires_at": refresh_expires_at,
    }

    # Generate templates for this new firm
    template_docs = []
    for tpl in default_templates():
        template_docs.append({
            "id": str(uuid.uuid4()),
            "firm_id": firm_id,
            "created_at": now,
            **tpl,
        })

    try:
        await db.create_firm_and_user_atomic(firm_doc, user_doc, template_docs)
    except ValueError as e:
        # Duplicate email conflict
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email address already exists.",
        ) from e

    # Generate access token
    access_token = create_access_token(user_id=user_id, firm_id=firm_id, token_version=1)

    return {
        "access_token": access_token,
        "refresh_token": raw_refresh,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "user": {
            "id": user_id,
            "email": email_clean,
            "name": user_doc["name"],
            "firm_id": firm_id,
            "created_at": now,
        },
        "firm": {
            "id": firm_id,
            "name": firm_doc["name"],
        },
    }


@auth_router.post("/login")
async def login(body: LoginRequest, request: Request):
    """Authenticate user with email and password, returning JWT access token and refresh token."""
    from server import db

    if db is None:
        raise HTTPException(status_code=500, detail="Database not initialized")

    email_clean = body.email.lower().strip()
    ip = _client_ip(request)
    key = _throttle_key(email_clean, ip)

    if login_limiter.is_throttled(key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Please try again after 15 minutes.",
        )

    # Lookup user by lowercase email
    user = await db.users.find_one({"email": email_clean})

    if not user:
        # Constant-time dummy verification prevents timing attacks for unknown emails
        verify_dummy_password(body.password)
        login_limiter.record_failure(key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    stored_hash = user.get("password_hash", "")
    if not verify_password(body.password, stored_hash):
        login_limiter.record_failure(key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Authentication succeeded: reset failure counter
    login_limiter.reset(key)

    # Issue new access token and refresh token
    token_version = int(user.get("token_version", 1))
    access_token = create_access_token(
        user_id=user["id"],
        firm_id=user["firm_id"],
        token_version=token_version,
    )
    raw_refresh, new_refresh_hash, new_refresh_exp = create_refresh_token()

    # Update refresh token in user record
    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {
            "refresh_hash": new_refresh_hash,
            "refresh_expires_at": new_refresh_exp,
            "last_login_at": _now_iso(),
        }}
    )

    # Fetch firm details
    firm = await db.firm.find_one({"id": user["firm_id"]})

    return {
        "access_token": access_token,
        "refresh_token": raw_refresh,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user.get("name", ""),
            "firm_id": user["firm_id"],
            "created_at": user.get("created_at"),
        },
        "firm": {
            "id": firm["id"] if firm else user["firm_id"],
            "name": firm.get("name", "") if firm else "",
        },
    }


@auth_router.post("/refresh")
async def refresh(body: RefreshRequest):
    """Rotate refresh token and issue a fresh access token."""
    from server import db

    if db is None:
        raise HTTPException(status_code=500, detail="Database not initialized")

    h = hash_refresh_token(body.refresh_token)
    user = await db.users.find_one({"refresh_hash": h})

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    # Check expiration
    expires_at_str = user.get("refresh_expires_at")
    if not expires_at_str:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    try:
        expires_at = datetime.fromisoformat(expires_at_str)
        if datetime.now(timezone.utc) > expires_at:
            raise HTTPException(status_code=401, detail="Refresh token has expired")
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid refresh token expiration")

    # Rotate refresh token
    raw_refresh, new_hash, new_exp = create_refresh_token()
    token_version = int(user.get("token_version", 1))
    access_token = create_access_token(
        user_id=user["id"],
        firm_id=user["firm_id"],
        token_version=token_version,
    )

    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {
            "refresh_hash": new_hash,
            "refresh_expires_at": new_exp,
        }}
    )

    return {
        "access_token": access_token,
        "refresh_token": raw_refresh,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }


@auth_router.post("/logout")
async def logout(current_user: AuthedUser = Depends(get_current_user)):
    """Revoke user tokens by incrementing token_version and clearing refresh token."""
    from server import db

    if db is None:
        raise HTTPException(status_code=500, detail="Database not initialized")

    # Invalidate existing JWTs by incrementing token_version
    await db.users.update_one(
        {"id": current_user.user_id},
        {"$set": {
            "token_version": current_user.token_version + 1,
            "refresh_hash": None,
            "refresh_expires_at": None,
        }}
    )
    return {"ok": True}


@auth_router.get("/me")
async def me(current_user: AuthedUser = Depends(get_current_user)):
    """Return authenticated user and firm context."""
    from server import db

    if db is None:
        raise HTTPException(status_code=500, detail="Database not initialized")

    user = await db.users.find_one({"id": current_user.user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    firm = await db.firm.find_one({"id": current_user.firm_id})

    return {
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user.get("name", ""),
            "firm_id": user["firm_id"],
            "created_at": user.get("created_at"),
        },
        "firm": {
            "id": firm["id"] if firm else current_user.firm_id,
            "name": firm.get("name", "") if firm else "",
            "contact_email": firm.get("contact_email", "") if firm else "",
            "retention_note": firm.get("retention_note", "") if firm else "",
            "settings": firm.get("settings", {}) if firm else {},
        },
    }
