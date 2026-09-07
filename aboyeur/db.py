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

-- Vintage nutrition-science/diet-theory reference text (Gutenberg bookshelf
-- 684 and similar — books with prose about nutrition, not recipes). Chunked
-- per source file; searched via search_nutrition_reference, kept separate
-- from the recipes table/FTS since it's prose, not structured recipe data.
-- See aboyeur/reference_indexer.py.
CREATE TABLE IF NOT EXISTS nutrition_reference (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL,
    book        TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    text        TEXT NOT NULL,
    mtime       REAL NOT NULL,
    indexed_at  TEXT NOT NULL,
    UNIQUE(path, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_nutrition_reference_book ON nutrition_reference(book);

CREATE VIRTUAL TABLE IF NOT EXISTS nutrition_reference_fts USING fts5(
    text, book,
    content='nutrition_reference',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS nutref_ai AFTER INSERT ON nutrition_reference BEGIN
    INSERT INTO nutrition_reference_fts(rowid, text, book)
    VALUES (new.id, new.text, new.book);
END;

CREATE TRIGGER IF NOT EXISTS nutref_ad AFTER DELETE ON nutrition_reference BEGIN
    INSERT INTO nutrition_reference_fts(nutrition_reference_fts, rowid, text, book)
    VALUES ('delete', old.id, old.text, old.book);
END;

CREATE TRIGGER IF NOT EXISTS nutref_au AFTER UPDATE ON nutrition_reference BEGIN
    INSERT INTO nutrition_reference_fts(nutrition_reference_fts, rowid, text, book)
    VALUES ('delete', old.id, old.text, old.book);
    INSERT INTO nutrition_reference_fts(rowid, text, book)
    VALUES (new.id, new.text, new.book);
END;

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

-- Multi-tenant: one row per account. password_hash is NULL for a user
-- provisioned before a password is set (not used in the current CLI-only
-- flow, but keeps room for an invite-then-set-password step later).
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT,
    created_at     TEXT NOT NULL
);

-- The people a practitioner plans for. THE subject key: profile, pantry,
-- menus and kits all hang off a client, never off the account directly, so
-- one client's data is unreachable from another's by construction (see
-- PRACTITIONER_PLAN.md). Account-level state — llm_settings, favorites —
-- stays on users.
CREATE TABLE IF NOT EXISTS clients (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    email       TEXT,
    phone       TEXT,
    notes       TEXT,
    status      TEXT NOT NULL DEFAULT 'active',   -- active | archived
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pantry (
    client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    item        TEXT NOT NULL,
    qty         TEXT,
    added_at    TEXT NOT NULL,
    expires_at  TEXT,
    PRIMARY KEY (client_id, item)
);

-- One row per client (was one per user, and before that a single
-- CHECK(id=1) row). aboyeur.migrate folds each account's old household into
-- a client of its own.
CREATE TABLE IF NOT EXISTS profile (
    client_id   INTEGER PRIMARY KEY REFERENCES clients(id) ON DELETE CASCADE,
    data_json   TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- idx_menus_user is NOT created here: on an existing pre-multi-tenant DB,
-- this whole schema runs via executescript before aboyeur.migrate has
-- rebuilt menus into the new shape, and CREATE INDEX (unlike CREATE TABLE
-- IF NOT EXISTS) doesn't no-op gracefully against a same-named table that's
-- missing the indexed column — it errors immediately. aboyeur.migrate.migrate()
-- creates the index itself once menus.user_id is guaranteed to exist,
-- whether the table was just rebuilt or was already fresh.
CREATE TABLE IF NOT EXISTS menus (
    id          INTEGER PRIMARY KEY,
    client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    week_of     TEXT NOT NULL,
    days_json   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft',
    created_at  TEXT NOT NULL
);

-- Macro Kits (build_macro_kit): a mix-and-match grocery template — food
-- columns plus a per-meal gram rule — as opposed to `menus`, which is
-- specific recipes on specific days. Deliberately its own table: a kit has
-- no recipe ids, no week, and no draft/confirmed lifecycle, so folding it
-- into menus would mean a days_json that means two different things.
-- idx_kits_user lives in aboyeur.migrate alongside idx_menus_user.
CREATE TABLE IF NOT EXISTS kits (
    id          INTEGER PRIMARY KEY,
    client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    data_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Per-user LLM endpoint + credentials, entered in the web Settings screen.
-- api_key_encrypted is a Fernet token (aboyeur/crypto.py) — never plaintext.
-- Post-multi-tenant like `kits`, so CREATE TABLE IF NOT EXISTS here is the
-- whole migration; aboyeur/migrate.py has nothing to fold.
CREATE TABLE IF NOT EXISTS llm_settings (
    user_id           INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    base_url          TEXT NOT NULL,
    model             TEXT NOT NULL,
    api_key_encrypted TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- Recipe catalog favorites (the Library UI's star toggle). Recipes stay a
-- single shared read-only corpus across every user — only this join table
-- is per-user.
CREATE TABLE IF NOT EXISTS favorites (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    recipe_id   INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, recipe_id)
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
