"""Symmetric encryption for secrets stored in the DB (per-user LLM API keys).

Fernet (AES-128-CBC + HMAC) over a key resolved from the environment. The
threat model this covers is a leaked DB file or a backup dump — not a
compromised host, where the key sits in the same .env as everything else.
See FRONTEND_PLAN.md's "Key at rest" note.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from aboyeur import auth

logger = logging.getLogger("aboyeur")

__all__ = ["encrypt", "decrypt", "InvalidToken"]


def _derive_key(secret: str) -> bytes:
    """A valid Fernet key (urlsafe-base64 of 32 bytes) from an arbitrary string."""
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    configured = os.environ.get("LLM_SETTINGS_KEY")
    if configured:
        try:
            return Fernet(configured.encode("ascii"))
        except (ValueError, TypeError) as e:
            raise RuntimeError(
                "LLM_SETTINGS_KEY is not a valid Fernet key. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from e
    # Zero-config for dev. Deliberately derived from JWT_SECRET rather than
    # generated per-process — a random key would decrypt nothing after a
    # restart. Rotating JWT_SECRET then orphans every stored LLM key, which
    # is exactly why a deploy should set LLM_SETTINGS_KEY of its own.
    logger.warning(
        "LLM_SETTINGS_KEY not set — deriving the LLM-key encryption key from "
        "JWT_SECRET. Set LLM_SETTINGS_KEY in .env before deploying; rotating "
        "JWT_SECRET otherwise makes every stored LLM API key undecryptable."
    )
    return Fernet(_derive_key(auth.JWT_SECRET))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Raises cryptography.fernet.InvalidToken if the key changed or the
    ciphertext is corrupt — callers treat that as "no usable stored key"."""
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
