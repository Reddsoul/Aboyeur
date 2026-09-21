"""Hosted LLM providers — the endpoints Aboyeur pays for and re-bills — and
the one function that decides which LLM a user's turn runs on.

All providers are OpenAI-compatible (OpenAI itself, Anthropic's compat
endpoint, a LiteLLM/vLLM box), so aboyeur.llm.make_client works unchanged:
a provider is just a (base_url, model, key) triple with prices attached.
Keys are Fernet-encrypted via aboyeur/crypto.py and never leave the server;
the admin UI sees `key_last4` only.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, replace

from cryptography.fernet import InvalidToken

from aboyeur import billing, crypto, llm_settings, urlguard
from aboyeur.config import Config


class ProviderError(Exception):
    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def _public(row: sqlite3.Row | dict) -> dict:
    d = dict(row)
    enc = d.pop("api_key_encrypted", None)
    last4 = ""
    if enc:
        try:
            last4 = crypto.decrypt(enc)[-4:]
        except InvalidToken:
            last4 = "????"
    d["key_last4"] = last4
    d["is_default"] = bool(d["is_default"])
    d["enabled"] = bool(d["enabled"])
    d["user_price_in_per_mtok"] = _marked_up(d["price_in_per_mtok"], d["markup_pct"])
    d["user_price_out_per_mtok"] = _marked_up(d["price_out_per_mtok"], d["markup_pct"])
    return d


def _marked_up(cents_per_mtok: int, markup_pct: int) -> int:
    return int(round(cents_per_mtok * (1 + markup_pct / 100)))


def list_providers(conn: sqlite3.Connection, include_disabled: bool = True) -> list[dict]:
    sql = "SELECT * FROM llm_providers"
    if not include_disabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY is_default DESC, name"
    return [_public(r) for r in conn.execute(sql)]


def get_provider(conn: sqlite3.Connection, provider_id: int) -> dict:
    row = conn.execute("SELECT * FROM llm_providers WHERE id = ?", (provider_id,)).fetchone()
    if row is None:
        raise ProviderError("no such provider", 404)
    return _public(row)


def get_provider_secret(conn: sqlite3.Connection, provider_id: int) -> dict:
    """Server-side only: the row with the key decrypted."""
    row = conn.execute("SELECT * FROM llm_providers WHERE id = ?", (provider_id,)).fetchone()
    if row is None:
        raise ProviderError("no such provider", 404)
    d = dict(row)
    try:
        d["api_key"] = crypto.decrypt(d.pop("api_key_encrypted"))
    except InvalidToken:
        raise ProviderError(f"provider {row['name']}: stored key no longer decrypts", 500)
    return d


def default_provider(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM llm_providers WHERE is_default = 1 AND enabled = 1 LIMIT 1"
    ).fetchone()
    return get_provider_secret(conn, row["id"]) if row else None


def create_provider(conn: sqlite3.Connection, *, name: str, base_url: str, model: str,
                    api_key: str, price_in_per_mtok: int, price_out_per_mtok: int,
                    markup_pct: int = 30, enabled: bool = True) -> dict:
    name, base_url, model = name.strip(), base_url.strip().rstrip("/"), model.strip()
    if not (name and base_url and model):
        raise ProviderError("name, base_url and model are required")
    if not base_url.startswith("https://") and not base_url.startswith("http://"):
        raise ProviderError("base_url must be http(s)")
    if min(price_in_per_mtok, price_out_per_mtok, markup_pct) < 0:
        raise ProviderError("prices and markup must be >= 0")
    # "unused" keeps a keyless self-hosted endpoint working with the SDK.
    encrypted = crypto.encrypt((api_key or "").strip() or "unused")
    first = conn.execute("SELECT COUNT(*) FROM llm_providers").fetchone()[0] == 0
    try:
        cur = conn.execute(
            "INSERT INTO llm_providers (name, base_url, model, api_key_encrypted, "
            "price_in_per_mtok, price_out_per_mtok, markup_pct, is_default, enabled, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, base_url, model, encrypted, price_in_per_mtok, price_out_per_mtok,
             markup_pct, 1 if first else 0, 1 if enabled else 0, _now()),
        )
    except sqlite3.IntegrityError:
        raise ProviderError(f"a provider named {name!r} already exists", 409)
    conn.commit()
    return get_provider(conn, cur.lastrowid)


def update_provider(conn: sqlite3.Connection, provider_id: int, patch: dict) -> dict:
    get_provider(conn, provider_id)
    sets, params = [], []
    for col in ("name", "base_url", "model"):
        if col in patch and patch[col] is not None:
            val = str(patch[col]).strip()
            if col == "base_url":
                val = val.rstrip("/")
            if not val:
                raise ProviderError(f"{col} can't be blank")
            sets.append(f"{col} = ?")
            params.append(val)
    for col in ("price_in_per_mtok", "price_out_per_mtok", "markup_pct"):
        if col in patch and patch[col] is not None:
            if int(patch[col]) < 0:
                raise ProviderError(f"{col} must be >= 0")
            sets.append(f"{col} = ?")
            params.append(int(patch[col]))
    if "enabled" in patch and patch["enabled"] is not None:
        sets.append("enabled = ?")
        params.append(1 if patch["enabled"] else 0)
    if patch.get("api_key"):
        sets.append("api_key_encrypted = ?")
        params.append(crypto.encrypt(str(patch["api_key"]).strip()))
    if sets:
        sets.append("updated_at = ?")
        params.extend([_now(), provider_id])
        try:
            conn.execute(f"UPDATE llm_providers SET {', '.join(sets)} WHERE id = ?", params)
        except sqlite3.IntegrityError:
            raise ProviderError("a provider with that name already exists", 409)
        conn.commit()
    return get_provider(conn, provider_id)


def set_default(conn: sqlite3.Connection, provider_id: int) -> dict:
    p = get_provider(conn, provider_id)
    if not p["enabled"]:
        raise ProviderError("enable the provider before making it the default", 409)
    conn.execute("UPDATE llm_providers SET is_default = 0")
    conn.execute("UPDATE llm_providers SET is_default = 1 WHERE id = ?", (provider_id,))
    conn.commit()
    return get_provider(conn, provider_id)


def test_provider(conn: sqlite3.Connection, provider_id: int, timeout: float = 15.0) -> dict:
    """One-token completion; reports latency. Never returns the key."""
    p = get_provider_secret(conn, provider_id)
    from openai import OpenAI

    client = OpenAI(base_url=p["base_url"], api_key=p["api_key"] or "unused",
                    timeout=timeout, max_retries=0)
    started = time.monotonic()
    try:
        client.chat.completions.create(
            model=p["model"], messages=[{"role": "user", "content": "ping"}], max_tokens=1,
        )
    except Exception as e:
        detail = (str(e).strip() or type(e).__name__).split("\n")[0][:300]
        return {"ok": False, "latency_ms": int((time.monotonic() - started) * 1000),
                "detail": detail}
    return {"ok": True, "latency_ms": int((time.monotonic() - started) * 1000),
            "detail": f"{p['model']} answered"}


# ---------------------------------------------------------------------------
# Resolution — which LLM does this user's turn run on?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedLlm:
    config: Config                 # base_url / model / key filled in
    provider: dict | None          # llm_providers row (with secret) when hosted
    billable: bool                 # True = meter + debit this user's wallet

    @property
    def provider_id(self) -> int | None:
        return self.provider["id"] if self.provider else None

    @property
    def mode(self) -> str:
        return "hosted" if self.provider else "byok"


def resolve_llm(conn: sqlite3.Connection, base: Config, user: dict) -> ResolvedLlm:
    """
    byok + stored settings         -> their endpoint, billable=False
    hosted, plan not pro/comp      -> 402 hosted LLM requires Pro
    hosted, credit <= 0 (not comp) -> 402 no credit
    hosted                         -> provider override or default, billable=True
    anything else                  -> 409 configure an LLM in Settings

    `user` is a billing.get_user dict. Raises ProviderError with the HTTP
    status the route should answer with.
    """
    if user["llm_mode"] == "hosted":
        if not billing.is_paid(user):
            raise ProviderError("Aboyeur's hosted models need a Pro plan", 402,
                                error="hosted_requires_pro", upgrade_url="/app/account")
        if user["billing_status"] != "comp" and user["credit_cents"] <= 0:
            raise ProviderError("no credit left — add credit in Settings to keep going", 402,
                                error="no_credit", topup_url="/app/settings")
        provider = None
        if user.get("llm_provider_id"):
            try:
                candidate = get_provider_secret(conn, user["llm_provider_id"])
                if candidate["enabled"]:
                    provider = candidate
            except ProviderError:
                provider = None
        if provider is None:
            provider = default_provider(conn)
        if provider is None:
            raise ProviderError("no hosted model is available right now", 503,
                                error="no_provider")
        return ResolvedLlm(
            config=replace(base, llm_base_url=provider["base_url"], llm_model=provider["model"],
                           llm_api_key=provider["api_key"]),
            provider=provider,
            billable=True,
        )
    stored = llm_settings.get_llm_settings(conn, user["id"])
    if stored is not None:
        # Cheap re-check (no DNS) so a row saved under a looser policy — the
        # beta's Tailscale LiteLLM, say — can't be used once the deploy
        # tightens it. The user gets the same 409 as "nothing configured".
        try:
            urlguard.check_llm_url(stored["base_url"], resolve=False)
        except urlguard.UnsafeUrlError as e:
            raise ProviderError(f"your saved LLM endpoint is no longer allowed ({e}); "
                                "update it in Settings", 409, error="llm_setup")
        return ResolvedLlm(
            config=replace(base, llm_base_url=stored["base_url"], llm_model=stored["model"],
                           llm_api_key=stored["api_key"]),
            provider=None,
            billable=False,
        )
    raise ProviderError("configure an LLM in Settings before chatting", 409,
                        error="llm_unconfigured", settings_url="/app/settings")


def hosted_offer(conn: sqlite3.Connection, user: dict) -> dict | None:
    """What the Settings page shows a user about the hosted option: the
    provider name they'd get and the price they'd pay. Endpoint hidden."""
    provider = None
    if user.get("llm_provider_id"):
        try:
            provider = get_provider(conn, user["llm_provider_id"])
        except ProviderError:
            provider = None
    if provider is None or not provider["enabled"]:
        row = conn.execute(
            "SELECT id FROM llm_providers WHERE is_default = 1 AND enabled = 1 LIMIT 1"
        ).fetchone()
        provider = get_provider(conn, row["id"]) if row else None
    if provider is None:
        return None
    return {
        "name": provider["name"],
        "model": provider["model"],
        # per 1K tokens, in cents, what the user pays
        "price_in_per_ktok_cents": round(provider["user_price_in_per_mtok"] / 1000, 4),
        "price_out_per_ktok_cents": round(provider["user_price_out_per_mtok"] / 1000, 4),
    }


def estimated_rows_count(conn: sqlite3.Connection, days: int = 7) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM usage_ledger WHERE kind LIKE '%_est' AND ts >= ?",
        (billing._since(days),),
    ).fetchone()[0]
