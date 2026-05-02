"""
Per-user (when authenticated) / per-IP rate limiter built on slowapi.

Defaults are intentionally permissive — tighten in production via env vars:

    RATE_LIMIT_DEFAULT="200/minute"
    RATE_LIMIT_TRADE="20/minute"
    RATE_LIMIT_AI="30/minute"
"""
from __future__ import annotations

import os

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def _key(request: Request) -> str:
    # Prefer the authenticated user id when the bearer token has been
    # validated by clerk_auth.get_user_id (we stash it on request.state).
    user = getattr(request.state, "user_id", None)
    if user:
        return f"user:{user}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=_key,
    default_limits=[os.getenv("RATE_LIMIT_DEFAULT", "200/minute")],
    storage_uri=os.getenv("RATE_LIMIT_STORAGE", "memory://"),
)

TRADE_LIMIT = os.getenv("RATE_LIMIT_TRADE", "20/minute")
AI_LIMIT = os.getenv("RATE_LIMIT_AI", "30/minute")
