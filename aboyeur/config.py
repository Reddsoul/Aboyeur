"""Environment configuration. Reads .env (if present) then process env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_FILE)


@dataclass(frozen=True)
class Config:
    recipes_dir: Path
    reference_dir: Path
    db_path: Path
    llm_base_url: str
    llm_model: str
    usda_api_key: str | None
    embedding_model: str

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
            llm_base_url=os.environ.get("LLM_BASE_URL", "http://100.70.6.1:4000/v1"),
            llm_model=os.environ.get("LLM_MODEL", "fast"),
            usda_api_key=os.environ.get("USDA_API_KEY") or None,
            embedding_model=os.environ.get("EMBEDDING_MODEL", "nomic-embed-text"),
        )

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
