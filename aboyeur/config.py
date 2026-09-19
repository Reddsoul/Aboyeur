"""Environment configuration. Reads process env (aboyeur/__init__.py loads .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# .env is loaded by aboyeur/__init__.py — importing any module in this package
# is enough, and doing it there (not here) is what keeps import order from
# deciding whether aboyeur.auth sees the real JWT_SECRET.


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _signup_mode(raw: str | None) -> str:
    mode = (raw or "invite").strip().lower()
    return mode if mode in ("invite", "open") else "invite"


class DeployConfigError(RuntimeError):
    """A secret a real deploy can't run without is missing."""


# Secrets the app refuses to boot without once it believes it's behind HTTPS
# (COOKIE_SECURE unset/true). Each guards a different thing: JWT_SECRET signs
# sessions, LLM_SETTINGS_KEY encrypts users' BYOK keys, DATA_KEY encrypts
# client PII. They're separate so rotating one doesn't orphan the others'
# ciphertext.
DEPLOY_SECRETS = ("JWT_SECRET", "DATA_KEY", "LLM_SETTINGS_KEY")


def check_deploy_secrets() -> None:
    """Raise DeployConfigError when COOKIE_SECURE is on and any of
    DEPLOY_SECRETS is empty. Local dev sets COOKIE_SECURE=false and is
    exempt — the derived-from-JWT_SECRET fallbacks in crypto.py cover it."""
    if not env_flag("COOKIE_SECURE", True):
        return
    missing = [name for name in DEPLOY_SECRETS if not (os.environ.get(name) or "").strip()]
    if missing:
        raise DeployConfigError(
            "refusing to start with COOKIE_SECURE=true and empty "
            + ", ".join(missing)
            + " — generate each with "
            "'python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"' "
            "(JWT_SECRET: 'python -c \"import secrets; print(secrets.token_urlsafe(48))\"') "
            "and put them in .env. Set COOKIE_SECURE=false only for plain-http local dev."
        )


@dataclass(frozen=True)
class Config:
    recipes_dir: Path
    reference_dir: Path
    db_path: Path
    llm_base_url: str
    llm_model: str
    llm_api_key: str | None
    usda_api_key: str | None
    embedding_model: str
    # --- SaaS (all optional in dev; see check_deploy_secrets for what a real
    # deploy must set) ---
    app_base_url: str = "http://localhost:5173"
    stripe_secret_key: str | None = None
    stripe_publishable_key: str | None = None
    stripe_webhook_secret: str | None = None
    resend_api_key: str | None = None
    email_from: str = "Aboyeur <no-reply@localhost>"
    signup_mode: str = "invite"          # invite | open
    sentry_dsn: str | None = None
    trust_cf_headers: bool = False       # read CF-Connecting-IP / CF-IPCountry

    @classmethod
    def load(cls) -> "Config":
        recipes_dir = os.environ.get("RECIPES_DIR", "/Volumes/General/Cookbooks/Recipes")
        # Sibling of RECIPES_DIR, not inside it — the recipe indexer only
        # scans one level under RECIPES_DIR, so a full-book reference file
        # living outside it is never mistaken for a recipe (see
        # aboyeur/indexer.py's docstring on directory shape).
        reference_dir = os.environ.get("REFERENCE_DIR", "/Volumes/General/Cookbooks/Reference")
        db_path = os.environ.get("DB_PATH", "~/.local/share/aboyeur/aboyeur.db")
        return cls(
            recipes_dir=Path(recipes_dir),
            reference_dir=Path(reference_dir),
            # Must be local disk, not the SMB share the code/corpus live on —
            # SQLite's WAL page reads over SMB run ~150x slower (measured:
            # <1ms local vs ~150ms over the mount for the same FTS5 query).
            db_path=Path(db_path).expanduser(),
            # No default. Empty = no platform fallback at all (every web user
            # brings their own key or uses a hosted provider) — a private
            # Tailscale IP baked in here would be one config slip away from a
            # public deploy pointing at the homelab.
            llm_base_url=(os.environ.get("LLM_BASE_URL") or "").strip().rstrip("/"),
            llm_model=os.environ.get("LLM_MODEL", "fast"),
            # Fallback only: the CLI and the OpenWebUI shim. Web users
            # store their own key (aboyeur/llm_settings.py), which
            # backend/main.py overrides onto a copy of this Config.
            llm_api_key=os.environ.get("LLM_API_KEY") or None,
            usda_api_key=os.environ.get("USDA_API_KEY") or None,
            embedding_model=os.environ.get("EMBEDDING_MODEL", "nomic-embed-text"),
            app_base_url=(os.environ.get("APP_BASE_URL") or "http://localhost:5173").rstrip("/"),
            stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY") or None,
            stripe_publishable_key=os.environ.get("STRIPE_PUBLISHABLE_KEY") or None,
            stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET") or None,
            resend_api_key=os.environ.get("RESEND_API_KEY") or None,
            email_from=os.environ.get("EMAIL_FROM") or "Aboyeur <no-reply@localhost>",
            signup_mode=_signup_mode(os.environ.get("SIGNUP_MODE")),
            sentry_dsn=os.environ.get("SENTRY_DSN") or None,
            trust_cf_headers=env_flag("TRUST_CF_HEADERS", False),
        )

    @property
    def has_platform_llm(self) -> bool:
        """True when env LLM_BASE_URL names an endpoint the CLI / seed shim may
        fall back to. Web users never get this fallback."""
        return bool(self.llm_base_url)

    def check_recipes_dir(self) -> None:
        """Raise a clear, friendly error if the corpus mount is missing or empty.

        Only call this from code paths that actually touch the corpus
        (indexing, verbatim recipe reads) — never from chat/tool dispatch.
        """
        if not self.recipes_dir.exists():
            raise FileNotFoundError(
                f"RECIPES_DIR not found: {self.recipes_dir}\n"
                "SMB mount likely unmounted or stale. Check /Volumes and remount, "
                "or set RECIPES_DIR in .env to the correct path."
            )
        if not any(self.recipes_dir.iterdir()):
            raise FileNotFoundError(
                f"RECIPES_DIR is empty: {self.recipes_dir}\n"
                "Mount looks present but empty — likely stale SMB mount. Remount and retry."
            )

    def check_reference_dir(self) -> None:
        """Same as check_recipes_dir but for REFERENCE_DIR. Only call from
        code paths that touch the reference corpus (reference indexing)."""
        if not self.reference_dir.exists():
            raise FileNotFoundError(
                f"REFERENCE_DIR not found: {self.reference_dir}\n"
                "SMB mount likely unmounted or stale. Check /Volumes and remount, "
                "or set REFERENCE_DIR in .env to the correct path."
            )
        if not any(self.reference_dir.iterdir()):
            raise FileNotFoundError(
                f"REFERENCE_DIR is empty: {self.reference_dir}\n"
                "Run nutrition_reference_extract.py in the Cookbooks repo first."
            )
