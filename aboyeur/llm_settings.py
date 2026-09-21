"""Per-user LLM endpoint + API key, stored server-side (llm_settings table).

The browser can't reach a Tailscale LLM address, so users hand the endpoint
to the backend and the backend makes the call. The key is Fernet-encrypted
at rest (aboyeur/crypto.py) and never leaves the server: the public shape
below is what every API response uses.
"""

from __future__ import annotations

import logging
import sqlite3
import time

from cryptography.fernet import InvalidToken

from aboyeur import crypto, urlguard

logger = logging.getLogger("aboyeur")


def _row(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT base_url, model, api_key_encrypted, updated_at "
        "FROM llm_settings WHERE user_id = ?",
        (user_id,),
    ).fetchone()


def get_llm_settings(conn: sqlite3.Connection, user_id: int) -> dict | None:
    """Full settings including the decrypted key, or None if unconfigured.

    Server-side only — never return this straight to a client. A row whose
    ciphertext no longer decrypts (LLM_SETTINGS_KEY or JWT_SECRET rotated)
    counts as unconfigured: the user re-enters their key.
    """
    row = _row(conn, user_id)
    if row is None:
        return None
    try:
        api_key = crypto.decrypt(row["api_key_encrypted"])
    except InvalidToken:
        logger.warning(
            "stored LLM key for user %s no longer decrypts — encryption key "
            "rotated? treating the user as unconfigured", user_id,
        )
        return None
    return {"base_url": row["base_url"], "model": row["model"], "api_key": api_key}


def get_llm_settings_public(conn: sqlite3.Connection, user_id: int) -> dict:
    """The client-safe shape. Carries no key material, not even its length."""
    row = _row(conn, user_id)
    if row is None:
        return {"base_url": "", "model": "", "configured": False}
    return {"base_url": row["base_url"], "model": row["model"], "configured": True}


def save_llm_settings(
    conn: sqlite3.Connection, user_id: int, base_url: str, model: str,
    api_key: str | None,
) -> dict:
    """Upsert. A blank/omitted api_key keeps the stored one, so the Settings
    form can edit base_url/model without making the user re-paste the key.
    Raises ValueError if there's no key to keep.
    """
    base_url = (base_url or "").strip().rstrip("/")
    model = (model or "").strip()
    api_key = (api_key or "").strip()
    if not base_url or not model:
        raise ValueError("base_url and model are required")
    # SSRF guard: the server connects to this URL. UnsafeUrlError is a
    # ValueError so the route's existing 400 mapping covers it.
    base_url = urlguard.check_llm_url(base_url)

    row = _row(conn, user_id)
    if api_key:
        encrypted = crypto.encrypt(api_key)
    elif row is not None:
        encrypted = row["api_key_encrypted"]
    else:
        # Keyless endpoints are real (a self-hosted LiteLLM with no auth), so
        # store the placeholder the OpenAI client needs rather than rejecting.
        encrypted = crypto.encrypt("unused")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        "INSERT INTO llm_settings (user_id, base_url, model, api_key_encrypted, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET base_url = excluded.base_url, "
        "model = excluded.model, api_key_encrypted = excluded.api_key_encrypted, "
        "updated_at = excluded.updated_at",
        (user_id, base_url, model, encrypted, now),
    )
    conn.commit()
    return get_llm_settings_public(conn, user_id)


def delete_llm_settings(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM llm_settings WHERE user_id = ?", (user_id,))
    conn.commit()
