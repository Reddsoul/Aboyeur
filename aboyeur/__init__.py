"""Aboyeur — conversational nutritionist/menu-maker over a personal recipe library."""

# .env is loaded here, at package import, not in aboyeur.config — modules that
# read os.environ at import time (aboyeur.auth's JWT_SECRET, aboyeur.crypto's
# LLM_SETTINGS_KEY) otherwise see the environment only if something imported
# aboyeur.config first. backend/main.py imports auth before config, which
# silently signed every session JWT with the insecure default secret.
from pathlib import Path as _Path

from dotenv import load_dotenv as _load_dotenv

_load_dotenv(_Path(__file__).resolve().parent.parent / ".env")
