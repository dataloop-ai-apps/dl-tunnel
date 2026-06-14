"""
Password authentication for dl-tunnel relay.

Credentials are stored in RAM only (never persisted to disk).
Passwords are SHA-256-hashed before storage; comparison is timing-safe.
"""

import hashlib
import hmac
import secrets


class AuthError(Exception):
    """Raised when authentication fails."""


def hash_password(password: str) -> str:
    """SHA-256 hash of a password. Acceptable for ephemeral RAM-only storage."""
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    """Timing-safe password verification."""
    candidate = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(candidate, password_hash)


def generate_password() -> str:
    """Generate a cryptographically secure random password (~32 URL-safe chars)."""
    return secrets.token_urlsafe(24)
