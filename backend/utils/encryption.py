"""
AES (Fernet) encryption for at-rest API credentials.

Each Clerk user gets a unique data-encryption key derived deterministically
from the server's APP_SECRET_KEY and the user's Clerk id. That way one
user's stored credentials cannot be decrypted by another user even if they
somehow gained read access to the SQLite file.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet

_master = os.getenv("APP_SECRET_KEY", "")
ANON = "local-dev"


def _derive_key(user_id: str) -> bytes:
    """HKDF-style derivation: sha256(master || \\0 || user_id), b64-url encoded
    so it satisfies Fernet's 32-byte key requirement."""
    if not _master:
        raise RuntimeError(
            "APP_SECRET_KEY is not set. Generate any 32+ char random string "
            "and put it in your .env before starting the backend."
        )
    digest = hashlib.sha256(f"{_master}\0{user_id or ANON}".encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet(user_id: str) -> Fernet:
    return Fernet(_derive_key(user_id))


class DecryptError(Exception):
    """Raised when a ciphertext cannot be decrypted with the current secret/user."""


def encrypt(value: str, user_id: str = ANON) -> str:
    if not value:
        return ""
    return _fernet(user_id).encrypt(value.encode()).decode()


def decrypt(value: str, user_id: str = ANON) -> str:
    if not value:
        return ""
    try:
        return _fernet(user_id).decrypt(value.encode()).decode()
    except Exception as exc:
        raise DecryptError(
            "Stored credential could not be decrypted. Either APP_SECRET_KEY "
            "changed or the credential was saved by a different user."
        ) from exc
