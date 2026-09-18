"""Authentication dependencies and context for FastAPI routes."""
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
import jwt

from tokens import decode_access_token


@dataclass(frozen=True)
class AuthedUser:
    """Authenticated user context containing immutable tenant identifiers."""
    user_id: str
    firm_id: str
    token_version: int
    email: str = ""


async def get_current_user(
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> AuthedUser:
    """FastAPI dependency to extract and validate the current user from Bearer JWT.

    Rejects missing, malformed, expired, or revoked tokens with HTTP 401.
    """
    from server import db

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = decode_access_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = claims.get("sub")
    firm_id = claims.get("firm_id")
    token_version = claims.get("token_version")

    if not user_id or not firm_id or token_version is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token claims",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if db is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database not initialized",
        )

    user = await db.users.find_one({"id": user_id})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User no longer exists",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check token revocation
    if user.get("token_version", 1) != token_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Verify firm matches
    if user.get("firm_id") != firm_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tenant mismatch in token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthedUser(
        user_id=str(user["id"]),
        firm_id=str(user["firm_id"]),
        token_version=int(user.get("token_version", 1)),
        email=str(user.get("email", "")),
    )
