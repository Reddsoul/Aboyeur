"""Shared fixtures for tool tests: a real SQLite DB built from tests/fixtures."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aboyeur.config import Config
from aboyeur.db import connect
from aboyeur.indexer import index_corpus

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    return Config(
        recipes_dir=FIXTURES,
        db_path=tmp_path / "test.db",
        llm_base_url="http://unused",
        llm_model="unused",
        usda_api_key=None,  # no real network calls in tests
        embedding_model="unused",
    )


@pytest.fixture()
def db_conn(config: Config) -> sqlite3.Connection:
    index_corpus(config, rebuild=True)
    conn = connect(config.db_path)
    yield conn
    conn.close()
