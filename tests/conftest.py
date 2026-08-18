"""Shared fixtures for tool tests: a real SQLite DB built from tests/fixtures."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from aboyeur.config import Config
from aboyeur.db import connect
from aboyeur.indexer import index_corpus

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def _fixtures_local(tmp_path_factory) -> Path:
    """One NAS read per session — the repo lives on SMB, where per-file
    round-trips dominate; per-test copies then go local-to-local."""
    src = tmp_path_factory.mktemp("fixtures") / "recipes"
    shutil.copytree(FIXTURES, src)
    return src


@pytest.fixture()
def config(tmp_path: Path, _fixtures_local: Path) -> Config:
    # Copy fixtures per-test: estimate_nutrition now writes nutrition blocks
    # back into recipe .md frontmatter, and tests must never dirty the repo's
    # fixture files (a committed block would flip later cache-behavior tests).
    recipes_dir = tmp_path / "recipes"
    shutil.copytree(_fixtures_local, recipes_dir)
    return Config(
        recipes_dir=recipes_dir,
        db_path=tmp_path / "test.db",
        llm_base_url="http://unused",
        llm_model="unused",
        usda_api_key=None,  # no real network calls in tests
        embedding_model="unused",
    )


@pytest.fixture(autouse=True)
def _no_llm_ingredient_parse(monkeypatch):
    """No test should hit a real LLM for ingredient parsing — default to the
    heuristic fallback path. Tests exercising the LLM path re-patch this."""
    from aboyeur.engines import nutrition as nutrition_engine
    monkeypatch.setattr(nutrition_engine, "_llm_parse_lines", lambda cfg, lines: None)


@pytest.fixture()
def db_conn(config: Config) -> sqlite3.Connection:
    index_corpus(config, rebuild=True)
    conn = connect(config.db_path)
    yield conn
    conn.close()
