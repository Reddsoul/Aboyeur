"""Self-serve accounts: signup, email verification, password reset, ToS
acceptance, data export, deletion + purge.

auth.py stays what it was (hashing, JWTs, the CLI's create_user); this is
the web signup surface layered on top. Every function takes a connection
and returns plain data; backend/main.py turns AccountError.status into the
HTTP response.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import time
import zipfile
from io import BytesIO

import httpx

from aboyeur import auth, clients as clients_mod
from aboyeur.legal import LEGAL_VERSION, needs_acceptance

logger = logging.getLogger("aboyeur")

MIN_PASSWORD_LEN = 10
VERIFY_TTL_S = 24 * 3600
RESET_TTL_S = 3600
PURGE_AFTER_DAYS = 30


class AccountError(Exception):
    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _at(seconds_from_now: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds_from_now))


# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------


def password_is_pwned(password: str, timeout: float = 3.0) -> bool:
    """HIBP k-anonymity range check. Best-effort: any network problem
    returns False (allowed) — a flaky third party must not block signup."""
    digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    try:
        res = httpx.get(
            f"https://api.pwnedpasswords.com/range/{prefix}",
            headers={"Add-Padding": "true", "User-Agent": "aboyeur-signup"},
            timeout=timeout,
        )
        res.raise_for_status()
    except httpx.HTTPError:
        return False
    for line in res.text.splitlines():
        hash_suffix, _, count = line.partition(":")
        if hash_suffix.strip() == suffix and count.strip() not in ("", "0"):
            return True
    return False


def check_password(password: str, *, pwned_check=password_is_pwned) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise AccountError(f"use at least {MIN_PASSWORD_LEN} characters", 400)
    if pwned_check(password):
        raise AccountError(
            "that password appears in a known data breach — choose a different one", 400,
            error="pwned",
        )


# ---------------------------------------------------------------------------
# Invite codes
# ---------------------------------------------------------------------------


def create_invite(conn: sqlite3.Connection, created_by: int | None, *, max_uses: int | None = 1,
                  expires_days: int | None = 30, note: str | None = None) -> dict:
    code = secrets.token_urlsafe(9)
    conn.execute(
        "INSERT INTO invite_codes (code, created_by, created_at, expires_at, max_uses, note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (code, created_by, _now(), _at(expires_days * 86400) if expires_days else None,
         max_uses, (note or "").strip() or None),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM invite_codes WHERE code = ?", (code,)).fetchone())


def list_invites(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM invite_codes ORDER BY created_at DESC")]


def revoke_invite(conn: sqlite3.Connection, code: str) -> None:
    conn.execute("DELETE FROM invite_codes WHERE code = ?", (code,))
    conn.commit()


def _consume_invite(conn: sqlite3.Connection, code: str | None) -> None:
    """Inside the caller's transaction. Raises AccountError if unusable."""
    code = (code or "").strip()
    if not code:
        raise AccountError("an invite code is required during the private beta", 403,
                           error="invite_required")
    row = conn.execute("SELECT * FROM invite_codes WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise AccountError("that invite code isn't valid", 403, error="invite_invalid")
    if row["expires_at"] and row["expires_at"] < _now():
        raise AccountError("that invite code has expired", 403, error="invite_expired")
    if row["max_uses"] is not None and row["uses"] >= row["max_uses"]:
        raise AccountError("that invite code has already been used", 403, error="invite_used")
    conn.execute("UPDATE invite_codes SET uses = uses + 1 WHERE code = ?", (code,))


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------


def signup(conn: sqlite3.Connection, *, email: str, password: str, signup_mode: str,
           invite_code: str | None = None, country: str | None = None,
           tos_version: str | None = None, pwned_check=password_is_pwned) -> int:
    """Create the account. Returns the new user id. The caller sends the
    verification email (see issue_token) — separated so a mail failure
    can't roll back the account."""
    email = (email or "").strip().lower()
    if not email or "@" not in email or len(email) > 254:
        raise AccountError("that doesn't look like an email address", 400)
    if not tos_version or tos_version != LEGAL_VERSION:
        raise AccountError("you must accept the current Terms of Service", 400, error="tos")
    check_password(password, pwned_check=pwned_check)
    country = (country or "").strip().upper() or None
    if country and not (len(country) == 2 and country.isalpha()):
        raise AccountError("country must be a two-letter code", 400)

    conn.execute("BEGIN IMMEDIATE")
    try:
        if signup_mode == "invite":
            _consume_invite(conn, invite_code)
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise AccountError("an account already exists for that email", 409, error="taken")
        now = _now()
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, created_at, country, tos_version, "
            "tos_accepted_at) VALUES (?, ?, ?, ?, ?, ?)",
            (email, auth.hash_password(password), now, country, LEGAL_VERSION, now),
        )
        user_id = cur.lastrowid
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    clients_mod.ensure_self_client(conn, user_id)
    return user_id


# ---------------------------------------------------------------------------
# Email tokens: verify + reset
# ---------------------------------------------------------------------------


def issue_token(conn: sqlite3.Connection, user_id: int, purpose: str) -> str:
    ttl = VERIFY_TTL_S if purpose == "verify" else RESET_TTL_S
    token = secrets.token_urlsafe(32)
    # One live token per purpose: a second "resend" invalidates the first.
    conn.execute(
        "DELETE FROM email_tokens WHERE user_id = ? AND purpose = ? AND used_at IS NULL",
        (user_id, purpose),
    )
    conn.execute(
        "INSERT INTO email_tokens (token, user_id, purpose, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, purpose, _at(ttl)),
    )
    conn.commit()
    return token


def _take_token(conn: sqlite3.Connection, token: str, purpose: str) -> int:
    """Validate + burn. Returns the user id."""
    row = conn.execute(
        "SELECT user_id, expires_at, used_at FROM email_tokens WHERE token = ? AND purpose = ?",
        ((token or "").strip(), purpose),
    ).fetchone()
    if row is None or row["used_at"] is not None:
        raise AccountError("that link is invalid or was already used", 400, error="token")
    if row["expires_at"] < _now():
        raise AccountError("that link has expired — request a new one", 400, error="expired")
    conn.execute("UPDATE email_tokens SET used_at = ? WHERE token = ?", (_now(), token))
    return row["user_id"]


def verify_email(conn: sqlite3.Connection, token: str) -> int:
    user_id = _take_token(conn, token, "verify")
    conn.execute(
        "UPDATE users SET email_verified_at = COALESCE(email_verified_at, ?) WHERE id = ?",
        (_now(), user_id),
    )
    conn.commit()
    return user_id


def request_reset(conn: sqlite3.Connection, email: str) -> tuple[int, str] | None:
    """(user_id, token) or None when no such account — the route answers
    the same either way so the form can't be used to enumerate emails."""
    row = conn.execute(
        "SELECT id FROM users WHERE email = ? AND deleted_at IS NULL",
        ((email or "").strip().lower(),),
    ).fetchone()
    if row is None:
        return None
    return row["id"], issue_token(conn, row["id"], "reset")


def reset_password(conn: sqlite3.Connection, token: str, new_password: str,
                   pwned_check=password_is_pwned) -> int:
    check_password(new_password, pwned_check=pwned_check)
    user_id = _take_token(conn, token, "reset")
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (auth.hash_password(new_password), user_id),
    )
    conn.commit()
    return user_id


# ---------------------------------------------------------------------------
# ToS
# ---------------------------------------------------------------------------


def tos_status(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute("SELECT tos_version FROM users WHERE id = ?", (user_id,)).fetchone()
    stored = row["tos_version"] if row else None
    return {"current": LEGAL_VERSION, "accepted": stored,
            "needs_acceptance": needs_acceptance(stored)}


def accept_tos(conn: sqlite3.Connection, user_id: int, version: str) -> dict:
    if version != LEGAL_VERSION:
        raise AccountError("that isn't the current terms version", 409, current=LEGAL_VERSION)
    conn.execute(
        "UPDATE users SET tos_version = ?, tos_accepted_at = ? WHERE id = ?",
        (LEGAL_VERSION, _now(), user_id),
    )
    conn.commit()
    return tos_status(conn, user_id)


# ---------------------------------------------------------------------------
# Data rights: export + delete + purge
# ---------------------------------------------------------------------------


def export_zip(conn: sqlite3.Connection, user_id: int) -> bytes:
    """Everything that is this user's, decrypted, as JSON files in a zip.
    Scoped by user_id -> clients -> per-client tables; nothing joins wider."""
    from aboyeur.tools.profile import get_profile

    account = conn.execute(
        "SELECT id, email, name, practice, country, plan, billing_status, paid_months, "
        "loyalty_tier, llm_mode, credit_cents, created_at, email_verified_at, tos_version, "
        "tos_accepted_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if account is None:
        raise AccountError("account not found", 404)
    files: dict[str, object] = {"account.json": dict(account)}

    client_rows = clients_mod.list_clients(conn, user_id, include_archived=True)
    files["clients.json"] = client_rows
    for c in client_rows:
        cid = c["id"]
        folder = f"clients/{cid}"
        files[f"{folder}/profile.json"] = get_profile(conn, cid)
        files[f"{folder}/pantry.json"] = [
            dict(r) for r in conn.execute("SELECT * FROM pantry WHERE client_id = ?", (cid,))
        ]
        files[f"{folder}/menus.json"] = [
            {**dict(r), "days": json.loads(r["days_json"])}
            for r in conn.execute("SELECT * FROM menus WHERE client_id = ?", (cid,))
        ]
        files[f"{folder}/kits.json"] = [
            {**dict(r), "data": json.loads(r["data_json"])}
            for r in conn.execute("SELECT * FROM kits WHERE client_id = ?", (cid,))
        ]
        files[f"{folder}/favorites.json"] = [
            dict(r) for r in conn.execute(
                "SELECT f.recipe_id, r.title, f.created_at FROM favorites f "
                "LEFT JOIN recipes r ON r.id = f.recipe_id WHERE f.client_id = ?", (cid,)
            )
        ]
        sessions = []
        for s in conn.execute(
            "SELECT id, title, created_at, updated_at FROM chat_sessions WHERE client_id = ?", (cid,)
        ):
            sessions.append({
                **dict(s),
                "messages": [
                    dict(m) for m in conn.execute(
                        "SELECT id, role, content, created_at FROM chat_messages "
                        "WHERE session_id = ? ORDER BY id", (s["id"],)
                    )
                ],
            })
        files[f"{folder}/conversations.json"] = sessions
    files["usage_ledger.json"] = [
        dict(r) for r in conn.execute(
            "SELECT ts, kind, model, tokens_in, tokens_out, charge_cents FROM usage_ledger "
            "WHERE user_id = ? ORDER BY ts", (user_id,)
        )
    ]
    files["support_access.json"] = [
        dict(r) for r in conn.execute(
            "SELECT ts, session_id FROM support_access WHERE user_id = ? ORDER BY ts", (user_id,)
        )
    ]

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in files.items():
            zf.writestr(name, json.dumps(payload, indent=2, default=str))
    return buf.getvalue()


def soft_delete(conn: sqlite3.Connection, user_id: int) -> dict:
    """Mark deleted; the purge job erases after PURGE_AFTER_DAYS. Sessions
    are invalidated by the route (cookie) and by get_current_user refusing
    deleted accounts. Stripe cancellation is the route's job (it has the
    client); the customer record is retained for tax records."""
    row = conn.execute(
        "SELECT deleted_at, stripe_subscription_id FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None:
        raise AccountError("account not found", 404)
    if row["deleted_at"]:
        return {"deleted_at": row["deleted_at"], "purge_after": _purge_date(row["deleted_at"])}
    now = _now()
    conn.execute("UPDATE users SET deleted_at = ? WHERE id = ?", (now, user_id))
    conn.execute("DELETE FROM email_tokens WHERE user_id = ?", (user_id,))
    conn.commit()
    return {"deleted_at": now, "purge_after": _purge_date(now),
            "stripe_subscription_id": row["stripe_subscription_id"]}


def _purge_date(deleted_at: str) -> str:
    t = time.strptime(deleted_at, "%Y-%m-%dT%H:%M:%SZ")
    return time.strftime("%Y-%m-%d", time.gmtime(time.mktime(t) - time.timezone + PURGE_AFTER_DAYS * 86400))


def is_deleted(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute("SELECT deleted_at FROM users WHERE id = ?", (user_id,)).fetchone()
    return row is None or bool(row["deleted_at"])


def purge_deleted(conn: sqlite3.Connection, older_than_days: int = PURGE_AFTER_DAYS) -> list[int]:
    """Hard-delete accounts soft-deleted more than N days ago.

    Done with foreign_keys OFF and explicit deletes rather than relying on
    ON DELETE CASCADE, for one reason: feedback.user_id is NOT NULL with a
    cascade, and the retention policy says feedback rows keep the user id
    (as an opaque number) so thumb counts and triage history survive. With
    FKs off the row stays; its body and client link are scrubbed. PRAGMA
    foreign_keys can't be changed inside a transaction, so it's toggled
    around each account's BEGIN/COMMIT.
    """
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() - older_than_days * 86400))
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM users WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,)
    )]
    for uid in ids:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            client_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM clients WHERE user_id = ?", (uid,)
            )]
            session_ids = [r["id"] for r in conn.execute(
                f"SELECT id FROM chat_sessions WHERE client_id IN ({_marks(client_ids)})",
                client_ids,
            )] if client_ids else []
            if session_ids:
                conn.execute(
                    f"DELETE FROM chat_messages WHERE session_id IN ({_marks(session_ids)})",
                    session_ids,
                )
                conn.execute(
                    f"DELETE FROM chat_sessions WHERE id IN ({_marks(session_ids)})", session_ids
                )
            if client_ids:
                for table in ("profile", "pantry", "menus", "kits", "favorites"):
                    conn.execute(
                        f"DELETE FROM {table} WHERE client_id IN ({_marks(client_ids)})", client_ids
                    )
                conn.execute(f"DELETE FROM clients WHERE id IN ({_marks(client_ids)})", client_ids)
            for table in ("llm_settings", "usage_ledger", "email_tokens", "support_access"):
                conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (uid,))
            # Logs keep no identity; feedback keeps the bare id.
            conn.execute("UPDATE events SET user_id = NULL, client_id = NULL WHERE user_id = ?", (uid,))
            conn.execute("UPDATE feedback SET body = NULL, client_id = NULL WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM users WHERE id = ?", (uid,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    return ids


def _marks(values: list) -> str:
    return ",".join("?" * len(values))
