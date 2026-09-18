"""Password hashing and verification utilities for LedgerLens Cloud SaaS.

Implements bcrypt cost factor 12, constant-time verification, and dummy hash
verification for non-existent users to defend against user enumeration timing attacks.
Never logs or returns password hashes.
"""
import bcrypt

BCRYPT_ROUNDS = 12

# Pre-computed constant dummy bcrypt hash generated with cost factor 12.
# Used for constant-time dummy verification when an unknown email attempts to log in.
_DUMMY_HASH = bcrypt.hashpw(
    b"ledgerlens-constant-time-dummy-password-timing-defense",
    bcrypt.gensalt(rounds=BCRYPT_ROUNDS),
).decode("utf-8")


def hash_password(plain_password: str) -> str:
    """Hash a plain text password using bcrypt with cost factor 12."""
    if not plain_password:
        raise ValueError("Password cannot be empty")
    pwd_bytes = plain_password.encode("utf-8")
    salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain text password against a stored bcrypt hash in constant time."""
    if not plain_password or not hashed_password:
        return False
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False


def verify_dummy_password(plain_password: str) -> bool:
    """Execute constant-time dummy bcrypt verification for non-existent users.

    Ensures that login attempts for unknown emails consume the same computational
    time as login attempts for existing users, preventing timing-based account enumeration.
    Always returns False.
    """
    try:
        pwd = plain_password.encode("utf-8") if plain_password else b"empty"
        bcrypt.checkpw(pwd, _DUMMY_HASH.encode("utf-8"))
    except Exception:
        pass
    return False
