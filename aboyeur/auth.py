"""Password hashing + JWT session tokens for multi-tenant auth.

Invite-only: accounts are created via `aboyeur create-user <email>` (see
cli.py), never a public signup route. Sessions are a JWT in an httpOnly
cookie, verified by backend/main.py's get_current_user dependency.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time

import bcrypt
import jwt

logger = logging.getLogger("aboyeur")

# bcrypt's own hard limit — truncate consistently in both directions rather
# than letting an over-length password raise ValueError deep in bcrypt.
_MAX_PASSWORD_BYTES = 72

JWT_ALGORITHM = "HS256"
JWT_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days

_INSECURE_DEFAULT_SECRET = "dev-only-insecure-secret-change-me"
JWT_SECRET = os.environ.get("JWT_SECRET", _INSECURE_DEFAULT_SECRET)
if JWT_SECRET == _INSECURE_DEFAULT_SECRET:
    logger.warning(
        "JWT_SECRET not set — using an insecure default. Set it in .env before "
        "deploying anywhere real; every issued session is forgeable without it."
    )


class AuthError(Exception):
    """Bad credentials, unknown user, or an invalid/expired token."""


def _password_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_MAX_PASSWORD_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(_password_bytes(password), password_hash.encode("ascii"))
    except ValueError:
        return False


def create_user(conn: sqlite3.Connection, email: str, password: str) -> int:
    """Create a new account. Raises AuthError if the email is already taken.

    Operator-provisioned (CLI) accounts are born email-verified: the person
    typing the address is the operator, not a stranger on a signup form.
    Web signups go through aboyeur.account.signup and verify by link."""
    email = email.strip().lower()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing is not None:
        raise AuthError(f"an account already exists for {email}")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, created_at, email_verified_at) "
        "VALUES (?, ?, ?, ?)",
        (email, hash_password(password), now, now),
    )
    conn.commit()
    return cur.lastrowid


def verify_login(conn: sqlite3.Connection, email: str, password: str) -> int:
    """Returns the user's id on success. Raises AuthError otherwise."""
    row = conn.execute(
        "SELECT id, password_hash FROM users WHERE email = ?", (email.strip().lower(),)
    ).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        raise AuthError("invalid email or password")
    return row["id"]


def get_or_create_user(conn: sqlite3.Connection, email: str) -> int:
    """CLI-only convenience: resolve a user by email, creating a password-less
    account if none exists yet. Never used by the web auth path — web login
    always goes through verify_login's password check."""
    email = email.strip().lower()
    row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if row is not None:
        return row["id"]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, created_at, email_verified_at) "
        "VALUES (?, NULL, ?, ?)",
        (email, now, now),
    )
    conn.commit()
    return cur.lastrowid


def issue_token(user_id: int, session_version: int = 0) -> str:
    payload = {
        "sub": str(user_id),  # RFC 7519: sub must be a StringOrURI, not a number
        "iat": int(time.time()),
        "exp": int(time.time()) + JWT_TTL_SECONDS,
        "sv": int(session_version),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def issue_session(conn: sqlite3.Connection, user_id: int) -> str:
    """issue_token stamped with the user's current session_version — the
    only way a login/signup should mint a cookie."""
    row = conn.execute("SELECT session_version FROM users WHERE id = ?", (user_id,)).fetchone()
    return issue_token(user_id, row["session_version"] if row else 0)


def decode_token(token: str) -> tuple[int, int]:
    """(user_id, session_version) from a token. Raises AuthError if invalid/expired."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as e:
        raise AuthError(f"invalid session: {e}") from e
    return int(payload["sub"]), int(payload.get("sv", 0))


def verify_token(token: str) -> int:
    """Returns the user_id encoded in the token. Raises AuthError if invalid/expired.
    Does NOT check session_version — callers that gate access must compare
    it against the users row (backend get_current_user does)."""
    return decode_token(token)[0]


def revoke_sessions(conn: sqlite3.Connection, user_id: int) -> None:
    """Invalidate every outstanding cookie for the account. Stateless JWTs
    can't be recalled individually, so bump the version they're checked
    against. Called on password change/reset and sign-out-everywhere."""
    conn.execute(
        "UPDATE users SET session_version = session_version + 1 WHERE id = ?", (user_id,)
    )
    conn.commit()
