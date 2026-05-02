"""
Clerk JWT verification.
=======================
Validates the `Authorization: Bearer <jwt>` header against Clerk's published
JWKS. Falls back to a single anonymous user when CLERK_JWKS_URL is unset, so
the app still works in pure-local development without a Clerk account.
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx
import jwt
from fastapi import Header, HTTPException, Request, status
from jwt import PyJWKClient

CLERK_JWKS_URL = os.getenv("CLERK_JWKS_URL", "").strip()
CLERK_ISSUER = os.getenv("CLERK_ISSUER", "").strip()
CLERK_AUDIENCE = os.getenv("CLERK_AUDIENCE", "").strip() or None
ANONYMOUS_USER_ID = "local-dev"

_jwks_client: PyJWKClient | None = None
_jwks_warmed_at: float = 0.0


def clerk_enabled() -> bool:
    return bool(CLERK_JWKS_URL)


def _get_jwks() -> PyJWKClient:
    global _jwks_client, _jwks_warmed_at
    # Refresh the JWKS client every 6 hours so rotated keys are picked up.
    if _jwks_client is None or (time.time() - _jwks_warmed_at) > 6 * 3600:
        _jwks_client = PyJWKClient(CLERK_JWKS_URL)
        _jwks_warmed_at = time.time()
    return _jwks_client


def _verify(token: str) -> dict[str, Any]:
    from datetime import timedelta
    signing_key = _get_jwks().get_signing_key_from_jwt(token).key
    options = {"verify_aud": bool(CLERK_AUDIENCE)}
    return jwt.decode(
        token,
        signing_key,
        algorithms=["RS256"],
        audience=CLERK_AUDIENCE,
        issuer=CLERK_ISSUER or None,
        options=options,
        leeway=timedelta(seconds=60),   # tolerate up to 60s clock skew
    )


def get_user_id(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI dependency. Returns Clerk user id (the `sub` claim) or the
    anonymous user id when Clerk is not configured. Also stashes the id on
    request.state so the rate limiter can key off it."""
    if not clerk_enabled():
        request.state.user_id = ANONYMOUS_USER_ID
        return ANONYMOUS_USER_ID
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = _verify(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {e}")
    except Exception as e:
        # Network error fetching JWKS, etc. — surface as 401 so the
        # frontend shows a friendly message rather than a raw 500.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Auth error: {e}")
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token missing sub claim")
    request.state.user_id = sub
    return sub
