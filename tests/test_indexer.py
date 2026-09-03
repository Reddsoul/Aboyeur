"""Phase 1 tests — indexer + DB, against real recipe fixtures. Never touches the SMB mount."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aboyeur.config import Config
from aboyeur.db import connect
from aboyeur.indexer import index_corpus, iter_recipe_files, parse_recipe_file

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    return Config(
        recipes_dir=FIXTURES,
        reference_dir=tmp_path / "reference",  # unused by these tests, no dir needed
        db_path=tmp_path / "test.db",
        llm_base_url="http://unused",
        llm_model="unused",
        usda_api_key=None,
        embedding_model="unused",
    )


def test_iter_recipe_files_skips_top_level_and_underscore_dirs():
    files = list(iter_recipe_files(FIXTURES))
    names = {f.name for f in files}
    assert "00_Dashboard.md" not in names
    assert len(files) == 19


def test_parse_recipe_file_extracts_frontmatter():
    r = parse_recipe_file(FIXTURES / "660_Curries" / "Achar_Gosht.md")
    assert r is not None
    assert r.title == "Achar Gosht"
    assert r.cuisine == "Indian"
    assert r.book == "660_Curries"
    assert "mustard oil" in r.ingredients
    assert "recipe" in r.tags


def test_parse_recipe_file_reads_serves_from_notes():
    r = parse_recipe_file(FIXTURES / "660_Curries" / "Achari_Murghi.md")
    assert r is not None
    assert r.servings == 6


def test_parse_recipe_file_handles_missing_servings():
    r = parse_recipe_file(FIXTURES / "A_Poetical_Cook-Book" / "APPLE_DUMPLINGS.md")
    assert r is not None
    assert r.servings is None


def test_parse_non_recipe_file_returns_none():
    # 00_Dashboard.md has no YAML frontmatter block.
    r = parse_recipe_file(FIXTURES / "00_Dashboard.md")
    assert r is None


def test_index_corpus_full_rebuild(config: Config):
    stats = index_corpus(config, rebuild=True)
    assert stats.scanned == 19
    assert stats.inserted == 19
    assert stats.updated == 0
    assert stats.skipped_no_frontmatter == 0

    conn = connect(config.db_path)
    n = conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
    assert n == 19
    conn.close()


def test_index_corpus_incremental_skips_unchanged(config: Config):
    index_corpus(config, rebuild=True)
    stats2 = index_corpus(config, rebuild=False)
    assert stats2.unchanged == 19
    assert stats2.inserted == 0
    assert stats2.updated == 0


def test_index_corpus_incremental_reparses_touched_file(config: Config):
    index_corpus(config, rebuild=True)

    target = FIXTURES / "660_Curries" / "Achar_Gosht.md"
    original = target.read_text()
    try:
        target.write_text(original)  # rewrite bumps mtime
        import os
        import time

        os.utime(target, (time.time() + 5, time.time() + 5))

        stats = index_corpus(config, rebuild=False)
        assert stats.updated == 1
        assert stats.unchanged == 18
    finally:
        target.write_text(original)


def test_ingredients_stored_as_json(config: Config):
    index_corpus(config, rebuild=True)
    conn = connect(config.db_path)
    row = conn.execute(
        "SELECT ingredients_json FROM recipes WHERE title = 'Achar Gosht'"
    ).fetchone()
    ingredients = json.loads(row["ingredients_json"])
    assert isinstance(ingredients, list)
    assert "beef" in ingredients
    conn.close()


def test_fts_search_returns_relevant_results(config: Config):
    index_corpus(config, rebuild=True)
    conn = connect(config.db_path)
    rows = conn.execute(
        """SELECT r.title FROM recipes_fts
           JOIN recipes r ON r.id = recipes_fts.rowid
           WHERE recipes_fts MATCH 'chocolate'
           ORDER BY bm25(recipes_fts)"""
    ).fetchall()
    titles = {row["title"] for row in rows}
    assert "Chocolate Cake" in titles
    conn.close()


def test_check_recipes_dir_missing_raises():
    bad_config = Config(
        recipes_dir=Path("/nonexistent/does/not/exist"),
        reference_dir=Path("/nonexistent/does/not/exist/reference"),
        db_path=Path("/tmp/unused.db"),
        llm_base_url="http://unused",
        llm_model="unused",
        usda_api_key=None,
        embedding_model="unused",
    )
    with pytest.raises(FileNotFoundError):
        index_corpus(bad_config)
