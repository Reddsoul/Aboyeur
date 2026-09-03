"""Reference-corpus tests — chunking, incremental indexing, FTS search.
Mirrors test_indexer.py's shape but for the nutrition_reference table."""

from __future__ import annotations

from pathlib import Path

import pytest

from aboyeur.config import Config
from aboyeur.db import connect
from aboyeur.reference_indexer import _chunk_text, index_reference_corpus
from aboyeur.tools.nutrition_reference import search_nutrition_reference

BOOK_A = """---
title: The Chemistry of Food and Nutrition
source: gutenberg
---

Vitamin C is destroyed by prolonged boiling. Scurvy was long known among \
sailors on long voyages before its dietary cause was understood.

Protein is built from amino acids. The body cannot synthesize every amino \
acid it needs, so some must come from food.
"""

BOOK_B = """---
title: Diet and Health
source: gutenberg
---

A calorie is a unit of heat. Counting calories was a popular reducing \
method in the early twentieth century.
"""


@pytest.fixture()
def reference_dir(tmp_path: Path) -> Path:
    d = tmp_path / "reference"
    d.mkdir()
    (d / "The_Chemistry_of_Food_and_Nutrition.md").write_text(BOOK_A, encoding="utf-8")
    (d / "Diet_and_Health.md").write_text(BOOK_B, encoding="utf-8")
    return d


@pytest.fixture()
def config(tmp_path: Path, reference_dir: Path) -> Config:
    return Config(
        recipes_dir=tmp_path / "recipes",  # unused by these tests, no dir needed
        reference_dir=reference_dir,
        db_path=tmp_path / "test.db",
        llm_base_url="http://unused",
        llm_model="unused",
        usda_api_key=None,
        embedding_model="unused",
    )


def test_chunk_text_splits_at_paragraph_boundaries():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 100 for i in range(10))
    chunks = _chunk_text(text)
    assert len(chunks) > 1
    # every paragraph survives whole in at least one chunk
    for i in range(10):
        assert any(f"Paragraph {i} " in c for c in chunks)


def test_chunk_text_short_input_is_one_chunk():
    assert _chunk_text("just one short paragraph") == ["just one short paragraph"]


def test_index_reference_corpus_inserts_chunks_per_book(config: Config):
    stats = index_reference_corpus(config, rebuild=True)
    assert stats.scanned == 2
    assert stats.inserted_files == 2
    assert stats.chunks_written >= 2

    conn = connect(config.db_path)
    books = {r["book"] for r in conn.execute("SELECT DISTINCT book FROM nutrition_reference")}
    assert books == {"The Chemistry of Food and Nutrition", "Diet and Health"}
    conn.close()


def test_index_reference_corpus_incremental_skips_unchanged(config: Config):
    index_reference_corpus(config, rebuild=True)
    stats2 = index_reference_corpus(config, rebuild=False)
    assert stats2.unchanged_files == 2
    assert stats2.inserted_files == 0
    assert stats2.updated_files == 0


def test_index_reference_corpus_reparses_touched_file(config: Config, reference_dir: Path):
    import os
    import time

    index_reference_corpus(config, rebuild=True)
    target = reference_dir / "Diet_and_Health.md"
    target.write_text(BOOK_B + "\nA new sentence about macronutrients.\n", encoding="utf-8")
    os.utime(target, (time.time() + 5, time.time() + 5))

    stats = index_reference_corpus(config, rebuild=False)
    assert stats.updated_files == 1
    assert stats.unchanged_files == 1


def test_search_nutrition_reference_finds_relevant_book(config: Config):
    index_reference_corpus(config, rebuild=True)
    conn = connect(config.db_path)
    results = search_nutrition_reference(conn, "scurvy vitamin")
    conn.close()
    assert results
    assert results[0]["book"] == "The Chemistry of Food and Nutrition"
    assert "scurvy" in results[0]["excerpt"].lower()


def test_search_nutrition_reference_or_matches_terms_in_different_paragraphs(config: Config):
    """BOOK_A has 'scurvy' in one paragraph and 'amino acids' in another —
    an implicit-AND query would miss it once chunks are paragraph-sized."""
    index_reference_corpus(config, rebuild=True)
    conn = connect(config.db_path)
    results = search_nutrition_reference(conn, "scurvy amino acids")
    conn.close()
    assert any(r["book"] == "The Chemistry of Food and Nutrition" for r in results)


def test_search_nutrition_reference_stopword_only_query_returns_empty(config: Config):
    index_reference_corpus(config, rebuild=True)
    conn = connect(config.db_path)
    assert search_nutrition_reference(conn, "why do we") == []
    conn.close()


def test_search_nutrition_reference_empty_query_returns_empty(config: Config):
    index_reference_corpus(config, rebuild=True)
    conn = connect(config.db_path)
    results = search_nutrition_reference(conn, "")
    conn.close()
    assert results == []


def test_search_nutrition_reference_missing_table_returns_empty(tmp_path: Path):
    """No index_reference_corpus ever run -> table doesn't exist. Must not raise."""
    from aboyeur.db import init_db

    db_path = tmp_path / "empty.db"
    conn = connect(db_path)
    init_db(conn)  # creates nutrition_reference (empty) via schema, but exercise
    # the "table genuinely absent" path too by dropping it back out.
    conn.execute("DROP TABLE nutrition_reference_fts")
    conn.execute("DROP TABLE nutrition_reference")
    results = search_nutrition_reference(conn, "vitamin")
    conn.close()
    assert results == []
