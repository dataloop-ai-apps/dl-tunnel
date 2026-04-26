"""
JWT authentication for dl-tunnel relay.

Validates a Dataloop JWT locally (no network call). Extracts org and email
claims used to namespace endpoint keys.
"""

import time
from dataclasses import dataclass

import jwt


@dataclass
class Identity:
    email: str
    sub: str  # stable subject id (e.g. "google-oauth2|105...") — survives email changes


class AuthError(Exception):
    """Raised when token validation fails."""


def validate_token(token: str) -> Identity:
    """
    Decode and validate a Dataloop JWT.

    Checks expiry and required identity claims. Does not verify the
    signature by default (options can be tightened once the DL Auth0
    public key is wired in).

    Raises AuthError on any validation failure.
    """
    if not token:
        raise AuthError("missing token")

    try:
        payload = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": True,
            },
            algorithms=["RS256", "HS256"],
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("token expired")
    except jwt.DecodeError as exc:
        raise AuthError(f"invalid token: {exc}")

    email = payload.get("email") or ""
    sub = payload.get("sub") or ""

    if not email and not sub:
        raise AuthError("token missing email/sub claim")

    return Identity(email=email, sub=sub or email)


def endpoint_key(identity: Identity, machine_name: str) -> str:
    """
    Build a namespaced endpoint key: '<sub>:<machine_name>'.

    Keyed by the JWT `sub` claim so each user's endpoints are isolated
    from every other user's. `sub` is stable across email changes.
    """
    return f"{identity.sub}:{machine_name}"
