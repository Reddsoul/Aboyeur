"""SQLite schema + connection helper.

One file, FTS5 for keyword search, a plain BLOB column for embeddings so
semantic search can be wired in later without a second index pass over the
corpus. No ORM — schema is DDL, queries are plain SQL.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id              INTEGER PRIMARY KEY,
    path            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    book            TEXT NOT NULL,
    cuisine         TEXT,
    servings        INTEGER,
    ingredients_json TEXT NOT NULL,
    tags_json       TEXT NOT NULL,
    mtime           REAL NOT NULL,
    indexed_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recipes_book ON recipes(book);
CREATE INDEX IF NOT EXISTS idx_recipes_cuisine ON recipes(cuisine);

CREATE VIRTUAL TABLE IF NOT EXISTS recipes_fts USING fts5(
    title, ingredients, tags, cuisine, book,
    content='recipes',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS recipes_ai AFTER INSERT ON recipes BEGIN
    INSERT INTO recipes_fts(rowid, title, ingredients, tags, cuisine, book)
    VALUES (new.id, new.title, new.ingredients_json, new.tags_json, new.cuisine, new.book);
END;

CREATE TRIGGER IF NOT EXISTS recipes_ad AFTER DELETE ON recipes BEGIN
    INSERT INTO recipes_fts(recipes_fts, rowid, title, ingredients, tags, cuisine, book)
    VALUES ('delete', old.id, old.title, old.ingredients_json, old.tags_json, old.cuisine, old.book);
END;

CREATE TRIGGER IF NOT EXISTS recipes_au AFTER UPDATE ON recipes BEGIN
    INSERT INTO recipes_fts(recipes_fts, rowid, title, ingredients, tags, cuisine, book)
    VALUES ('delete', old.id, old.title, old.ingredients_json, old.tags_json, old.cuisine, old.book);
    INSERT INTO recipes_fts(rowid, title, ingredients, tags, cuisine, book)
    VALUES (new.id, new.title, new.ingredients_json, new.tags_json, new.cuisine, new.book);
END;

-- Vector column, populated lazily by a future embedding pass (not Phase 1).
-- Raw float32 BLOB; cosine similarity computed in Python until/unless a
-- vector index extension is warranted at this corpus size.
CREATE TABLE IF NOT EXISTS embeddings (
    recipe_id   INTEGER PRIMARY KEY REFERENCES recipes(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nutrition (
    recipe_id     INTEGER PRIMARY KEY REFERENCES recipes(id) ON DELETE CASCADE,
    kcal          REAL,
    protein_g     REAL,
    carbs_g       REAL,
    fat_g         REAL,
    fiber_g       REAL,
    micros_json   TEXT,
    per_serving   INTEGER NOT NULL DEFAULT 1,
    confidence    TEXT NOT NULL DEFAULT 'unknown',
    estimated_at  TEXT
);

-- Parsed ingredient lines ("6 eggs" -> food "eggs", ~300 g), LLM-produced.
-- Keyed by the raw line so a corpus-wide repeat ("salt and pepper to taste")
-- is parsed once ever. Heuristic fallbacks are NOT cached (same reasoning as
-- usda_cache: a fallback isn't information about the line, and caching it
-- would stop the LLM from ever being retried).
CREATE TABLE IF NOT EXISTS ingredient_parse (
    raw        TEXT PRIMARY KEY,
    food       TEXT NOT NULL,
    grams      REAL NOT NULL,
    parsed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usda_cache (
    ingredient   TEXT PRIMARY KEY,
    fdc_id       TEXT,
    macros_json  TEXT,
    micros_json  TEXT,
    fetched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pantry (
    item        TEXT PRIMARY KEY,
    qty         TEXT,
    added_at    TEXT NOT NULL,
    expires_at  TEXT
);

CREATE TABLE IF NOT EXISTS profile (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    data_json   TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS menus (
    id          INTEGER PRIMARY KEY,
    week_of     TEXT NOT NULL,
    days_json   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interactions (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    session  TEXT,
    role     TEXT NOT NULL,
    content  TEXT NOT NULL,
    intent   TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the FastAPI backend shares one connection across
    # its threadpool. Safe here — callers serialize access with a lock.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
