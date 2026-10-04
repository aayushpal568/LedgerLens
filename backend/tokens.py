"""JWT access tokens and opaque refresh token management for LedgerLens Cloud SaaS.

Uses PyJWT with HS256, enforces short-lived access tokens, and uses SHA-256 hashed
opaque refresh tokens.
"""
import hashlib
from datetime import datetime, timedelta, timezone
import os
import secrets
from typing import Any, Dict, Tuple

import jwt

# Default access token lifetime: 15 minutes
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
# Default refresh token lifetime: 7 days
REFRESH_TOKEN_EXPIRE_DAYS = int(os.environ.get("REFRESH_TOKEN_EXPIRE_DAYS", "7"))


def get_auth_secret_key() -> str:
    """Retrieve AUTH_SECRET_KEY from environment or raise RuntimeError."""
    key = os.environ.get("AUTH_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "AUTH_SECRET_KEY environment variable is required but missing or empty. "
            "Please configure AUTH_SECRET_KEY in backend/.env"
        )
    return key


def create_access_token(user_id: str, firm_id: str, token_version: int) -> str:
    """Generate a signed short-lived JWT access token."""
    secret_key = get_auth_secret_key()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "firm_id": str(firm_id),
        "token_version": int(token_version),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, secret_key, algorithm="HS256")


def decode_access_token(token: str) -> Dict[str, Any]:
    """Decode and validate a JWT access token.

    Raises jwt.ExpiredSignatureError or jwt.PyJWTError on failure.
    """
    secret_key = get_auth_secret_key()
    return jwt.decode(
        token,
        secret_key,
        algorithms=["HS256"],
        options={"require": ["exp"]},
    )


def hash_refresh_token(raw_token: str) -> str:
    """Compute SHA-256 hash of an opaque refresh token for safe storage."""
    return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()


def create_refresh_token() -> Tuple[str, str, str]:
    """Generate a new opaque refresh token, its SHA-256 hash, and ISO expiry timestamp.

    Returns:
        (raw_refresh_token, refresh_token_hash, expires_at_iso)
    """
    raw_token = secrets.token_urlsafe(48)
    token_hash = hash_refresh_token(raw_token)
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)).isoformat()
    return raw_token, token_hash, expires_at
