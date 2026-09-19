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

__all__ = [
    "encrypt", "decrypt", "InvalidToken",
    "encrypt_field", "decrypt_field", "is_encrypted_field", "FIELD_PREFIX",
]


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


# ---------------------------------------------------------------------------
# PII at rest — clients.name/email/phone/notes and profile.data_json.
#
# A second Fernet under DATA_KEY, deliberately not LLM_SETTINGS_KEY: the two
# guard different things (a user's BYOK credentials vs. their clients' health
# data) and rotating one must not orphan the other's ciphertext. Values carry
# an `enc:` prefix so the migration that encrypts existing rows is idempotent
# and a reader can tell a legacy plaintext row from an encrypted one.
# ---------------------------------------------------------------------------

FIELD_PREFIX = "enc:"


@lru_cache(maxsize=1)
def _data_fernet() -> Fernet:
    configured = os.environ.get("DATA_KEY")
    if configured:
        try:
            return Fernet(configured.encode("ascii"))
        except (ValueError, TypeError) as e:
            raise RuntimeError(
                "DATA_KEY is not a valid Fernet key. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from e
    logger.warning(
        "DATA_KEY not set — deriving the PII encryption key from JWT_SECRET. "
        "Set DATA_KEY in .env before deploying (config.check_deploy_secrets "
        "refuses to boot without it behind HTTPS)."
    )
    return Fernet(_derive_key("data:" + auth.JWT_SECRET))


def is_encrypted_field(value: str | None) -> bool:
    return bool(value) and value.startswith(FIELD_PREFIX)


def encrypt_field(value: str | None) -> str | None:
    """None stays None (a nullable column stays nullable); anything else
    becomes `enc:<fernet token>`. Already-encrypted input is returned as is,
    which is what makes the backfill migration safe to re-run."""
    if value is None:
        return None
    if is_encrypted_field(value):
        return value
    return FIELD_PREFIX + _data_fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_field(value: str | None) -> str | None:
    """The inverse. A value without the prefix is a legacy plaintext row (the
    migration hasn't run, or ran on a different DB) and is returned untouched
    rather than failing a read. A prefixed value that doesn't decrypt raises
    InvalidToken — DATA_KEY rotated without re-encrypting, which is a deploy
    error worth surfacing, not a row worth hiding."""
    if value is None or not is_encrypted_field(value):
        return value
    return _data_fernet().decrypt(value[len(FIELD_PREFIX):].encode("ascii")).decode("utf-8")
