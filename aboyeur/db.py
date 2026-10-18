"""SQLite schema + connection helper.

One file, FTS5 for keyword search, a plain BLOB column for embeddings so
semantic search can be wired in later without a second index pass over the
corpus. No ORM — schema is DDL, queries are plain SQL.
"""

from __future__ import annotations

import fcntl
import sqlite3
from contextlib import contextmanager
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
    indexed_at      TEXT NOT NULL,
    -- Populated by aboyeur/classify_recipes.py (one-time batch LLM pass), not
    -- by the indexer. course: main|side|breakfast|dessert|drink|component|unknown.
    -- serving_est_g: one serving's cooked weight (rough). meal_complete: 1 if
    -- edible as a standalone meal (has a protein + a substantive base).
    course          TEXT NOT NULL DEFAULT 'unknown',
    serving_est_g   REAL,
    meal_complete   INTEGER NOT NULL DEFAULT 0,
    classified_at   TEXT,
    -- Populated by aboyeur/classify_equipment.py (keyword-scan MVP over the
    -- recipe's instructions text, see feature-spec-2026-09-26.md #5) — JSON
    -- list of tags from the same vocabulary as profile.tools_available, e.g.
    -- ["oven","stovetop"]. NULL = not yet classified (never treated as "no
    -- equipment needed" — the planner only hard-excludes recipes that HAVE
    -- been classified with a tool the client lacks).
    equipment_json  TEXT,
    equipment_classified_at TEXT,
    -- Populated by aboyeur/classify_meal_prep.py (batch-cook / leftover-
    -- friendly heuristic). 0 default reads as "not batch-cook-friendly" until
    -- classified; meal_prep_at distinguishes "never checked" from "checked,
    -- no" the same way classified_at does for course.
    meal_prep       INTEGER NOT NULL DEFAULT 0,
    meal_prep_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_recipes_book ON recipes(book);
-- idx_recipes_course is created by migrate._add_recipe_classification_columns,
-- not here: on an existing pre-course DB, init_db() runs this whole script
-- BEFORE the ALTER that adds the column, so a course index in the static
-- schema would fail on "no such column".
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
    estimated_at  TEXT,
    -- Servings the per-serving figures were divided by when the recipe
    -- itself doesn't say (derived from total parsed grams / the course's
    -- serving_est_g). NULL when the recipe stated its servings.
    servings_est  INTEGER,
    -- Manual correction (aboyeur/tools/nutrition.py set_recipe_macro_override).
    -- Global to the recipe, not per-client — see feature-spec-2026-09-26.md #1.
    -- Same shape as the calculated fields above (kcal/protein_g/carb_g/fat_g/
    -- fiber_g). get_effective_macros / estimate_nutrition return this instead
    -- of the calculated row whenever is_override is set; the calculated
    -- columns are left in place underneath so clearing the override restores
    -- them without a recompute.
    override_json TEXT,
    is_override   INTEGER NOT NULL DEFAULT 0
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
    -- The practitioner's own name and practice, shown on the account screen.
    -- Distinct from any client record, including their own "Me" client.
    name           TEXT,
    practice       TEXT,
    -- Operator access to the /admin console. Off for everyone by default,
    -- granted out-of-band with `aboyeur set-admin <email>` — there is no
    -- route, CLI flag aside, that can promote an account from inside the app.
    is_admin       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    -- SaaS columns (billing / hosted LLM / privacy). Mirrored in
    -- aboyeur.migrate._USER_SAAS_COLUMNS for DBs that predate them; keep
    -- the two lists in step.
    billing_status TEXT NOT NULL DEFAULT 'free',    -- free | active | past_due | canceled | comp
    plan           TEXT NOT NULL DEFAULT 'free',    -- free | pro
    country        TEXT,                            -- ISO-3166 alpha-2, user-editable
    stripe_customer_id     TEXT,                    -- unique via idx_users_stripe_customer
    stripe_subscription_id TEXT,
    paid_months    INTEGER NOT NULL DEFAULT 0,      -- consecutive paid months (loyalty tenure)
    loyalty_tier   TEXT,                            -- ladder step id; NULL = base price
    llm_mode       TEXT NOT NULL DEFAULT 'byok',    -- byok | hosted
    llm_provider_id INTEGER REFERENCES llm_providers(id),   -- NULL = platform default
    credit_cents   INTEGER NOT NULL DEFAULT 0,      -- hosted-LLM wallet
    email_verified_at TEXT,
    tos_version    TEXT,
    tos_accepted_at TEXT,
    deleted_at     TEXT,                            -- soft delete; purged after 30d
    low_balance_emailed_at TEXT,
    -- Bumped on password change/reset and "sign out everywhere"; a session
    -- cookie carrying an older value is rejected (aboyeur.auth).
    session_version INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_stripe_customer ON users(stripe_customer_id);

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
    -- The account owner's own record ("Me"). A flag, not a name match: the
    -- migration names this client after the household's primary member, and
    -- anyone can rename theirs, so the name can't be the marker.
    is_self     INTEGER NOT NULL DEFAULT 0,
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
-- single shared read-only corpus; this join table is per CLIENT, not per
-- account — a star means "this suits this person", which is exactly the
-- judgement that doesn't transfer between two clients.
CREATE TABLE IF NOT EXISTS favorites (
    client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    recipe_id   INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (client_id, recipe_id)
);

-- Conversations, and the messages in them. A client has many conversations —
-- this week's plan, last month's cut, a one-off question — so starting a new
-- one never means losing the old. Sessions hang off a client and messages
-- hang off a session, the same one-hop shape clients have off users, which
-- keeps the isolation story unchanged: a session is reachable only through
-- its client, and its client only through its account.
CREATE TABLE IF NOT EXISTS chat_sessions (
    id          INTEGER PRIMARY KEY,
    client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    -- NULL until the first user message names it (see chat_history.py).
    title       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,            -- user | assistant
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    -- aboyeur/sensitivity.py: NULL (unclassified) | none | sensitive | unknown,
    -- and the classifier's category list. Never a summary of the content.
    sensitivity      TEXT,
    sensitivity_json TEXT,
    -- Set on an assistant turn that built/updated/delivered a menu, so the
    -- transcript can offer "Export PDF" on that bubble later. Added by
    -- aboyeur.migrate on older DBs. No REFERENCES: a purged menu must not
    -- take the conversation with it.
    menu_id          INTEGER
);

-- Menu → PDF snapshots (aboyeur/export_pdf.py). Stored as a blob rather
-- than re-rendered on demand so what the practitioner sent the client is
-- what they can re-download later, however the menu, pantry or corpus
-- changed since. Cascades with its menu/client, so purge stays one DELETE.
CREATE TABLE IF NOT EXISTS menu_exports (
    id          INTEGER PRIMARY KEY,
    menu_id     INTEGER NOT NULL REFERENCES menus(id) ON DELETE CASCADE,
    client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    pdf         BLOB NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_menu_exports_menu ON menu_exports(menu_id);

CREATE TABLE IF NOT EXISTS interactions (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    session  TEXT,
    role     TEXT NOT NULL,
    content  TEXT NOT NULL,
    intent   TEXT
);

-- Operator monitoring. Born after the practitioner migration, so — like
-- `kits` and `llm_settings` — CREATE TABLE IF NOT EXISTS here is the whole
-- story: there is no older shape of these to rebuild.
--
-- One row per agent turn, plus a row for anything else worth answering
-- "what happened at 3am" with. Deliberately denormalized and nullable: this
-- is a log, and a log that refuses a write because a foreign key moved is
-- worse than one with a dangling id. No REFERENCES for that reason — a
-- purged client must not cascade its history out of the record.
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,              -- ISO8601 UTC
    kind         TEXT NOT NULL,              -- chat_turn | error | menu_plan | login
    user_id      INTEGER,
    client_id    INTEGER,
    session_id   INTEGER,
    model        TEXT,
    latency_ms   INTEGER,
    tool_calls   INTEGER,
    tokens_in    INTEGER,                    -- NULL when the endpoint omits usage
    tokens_out   INTEGER,
    ok           INTEGER NOT NULL DEFAULT 1,
    error_type   TEXT,
    error_detail TEXT,                       -- truncated traceback / message
    meta_json    TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id, ts);

-- A thumb on one assistant message. Anchored to (session, message) rather
-- than floating free, so triage always lands on the exact turn that earned
-- it. UNIQUE(message_id, user_id) makes re-thumbing an update, not a pile.
CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id   INTEGER,
    session_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    rating      INTEGER NOT NULL,            -- -1 | +1
    body        TEXT,                        -- optional thumb-down comment
    status      TEXT NOT NULL DEFAULT 'new', -- new | triaged | in_progress | resolved | wontfix
    admin_note  TEXT,
    resolved_at TEXT,
    UNIQUE (message_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(status, created_at);

-- ---------------------------------------------------------------------------
-- SaaS: billing, hosted LLM, privacy. All post-practitioner, so CREATE TABLE
-- IF NOT EXISTS is the whole story for the new tables; the new columns on
-- users / chat_messages are added by aboyeur.migrate (ALTER TABLE).
-- ---------------------------------------------------------------------------

-- Free-form operator KV: cached Stripe Product ids, feature switches. Not
-- config — config is env. This is state the app creates for itself.
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- Source of truth for what a plan costs where. Each row maps to exactly one
-- Stripe Price (immutable there, so an edit here archives the row and makes
-- a new one). country is ISO-3166 alpha-2 or '*' for the default.
-- Uniqueness of the *active* row per combo is a partial index below, not a
-- table constraint: UNIQUE(plan, interval, country, active) would forbid a
-- second archived row for the same combo, and every edit makes one.
CREATE TABLE IF NOT EXISTS plan_prices (
    id               INTEGER PRIMARY KEY,
    plan             TEXT NOT NULL,
    interval         TEXT NOT NULL,             -- month | year
    country          TEXT NOT NULL,
    amount_cents     INTEGER NOT NULL,          -- USD
    stripe_price_id  TEXT,                      -- created lazily on first checkout
    active           INTEGER NOT NULL DEFAULT 1,
    updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_prices_active
    ON plan_prices(plan, interval, country) WHERE active = 1;

-- Tenure ladder: after `min_paid_months` consecutive paid months the user's
-- subscription carries this tier's coupon. Lapse resets tenure (billing.py).
CREATE TABLE IF NOT EXISTS loyalty_ladder (
    id               INTEGER PRIMARY KEY,
    tier             TEXT NOT NULL UNIQUE,      -- m3 | m6 | m12
    min_paid_months  INTEGER NOT NULL,
    percent_off      INTEGER NOT NULL,          -- 0-100
    stripe_coupon_id TEXT,                      -- created lazily (duration=forever)
    active           INTEGER NOT NULL DEFAULT 1
);

-- Hosted LLM endpoints we pay for and re-bill. The key is Fernet-encrypted
-- (aboyeur/crypto.py); prices are OUR cost in cents per million tokens, and
-- markup_pct is what the user pays on top.
CREATE TABLE IF NOT EXISTS llm_providers (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    base_url            TEXT NOT NULL,
    model               TEXT NOT NULL,
    api_key_encrypted   TEXT NOT NULL,
    price_in_per_mtok   INTEGER NOT NULL,
    price_out_per_mtok  INTEGER NOT NULL,
    markup_pct          INTEGER NOT NULL DEFAULT 30,
    is_default          INTEGER NOT NULL DEFAULT 0,
    enabled             INTEGER NOT NULL DEFAULT 1,
    updated_at          TEXT NOT NULL
);

-- Every movement of a user's prepaid credit: turns debit, top-ups credit,
-- refunds and admin adjustments do either. cost_cents is our cost at the
-- time; charge_cents is what actually moved on users.credit_cents.
CREATE TABLE IF NOT EXISTS usage_ledger (
    id             INTEGER PRIMARY KEY,
    ts             TEXT NOT NULL,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id       INTEGER,
    provider_id    INTEGER REFERENCES llm_providers(id),
    model          TEXT NOT NULL,
    tokens_in      INTEGER NOT NULL,
    tokens_out     INTEGER NOT NULL,
    cost_cents     INTEGER NOT NULL,
    charge_cents   INTEGER NOT NULL,
    kind           TEXT NOT NULL                -- chat_turn | chat_turn_est | classify | topup | adjust | refund
);
CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage_ledger(user_id, ts);

-- Webhook idempotency: Stripe retries until it sees a 2xx, and can deliver
-- the same event twice regardless. PK on the event id makes the second
-- delivery a no-op before any handler runs.
CREATE TABLE IF NOT EXISTS stripe_events (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed   INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

-- Who did what in the console. Transcript reveals, comp toggles, price
-- edits — anything a user would want to know an operator did.
CREATE TABLE IF NOT EXISTS admin_audit (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    admin_id  INTEGER NOT NULL,
    action    TEXT NOT NULL,
    target    TEXT,                             -- user:12 / session:44 / price:3
    meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_target ON admin_audit(target, ts);

-- Email verification + password reset. Single-use, short-lived.
CREATE TABLE IF NOT EXISTS email_tokens (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose    TEXT NOT NULL,                   -- verify | reset
    expires_at TEXT NOT NULL,
    used_at    TEXT
);

-- SIGNUP_MODE=invite: a code the signup form must carry. max_uses NULL =
-- unlimited until expiry.
CREATE TABLE IF NOT EXISTS invite_codes (
    code        TEXT PRIMARY KEY,
    created_by  INTEGER,
    created_at  TEXT NOT NULL,
    expires_at  TEXT,
    max_uses    INTEGER,
    uses        INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);

-- Operator's record that a support reveal happened, surfaced to the user on
-- their Account page. Denormalized from admin_audit on purpose: the audit
-- table is 2-year retention and admin-only; this is the user-facing view.
CREATE TABLE IF NOT EXISTS support_access (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id  INTEGER NOT NULL,
    admin_id    INTEGER NOT NULL
);
"""


@contextmanager
def schema_lock(db_path: Path):
    """Serialize init_db + migrate across PROCESSES, not just threads.

    uvicorn --reload starts a new app process on every edit, and each one runs
    init_db() then migrate() on the same file. Two of those overlapping is not
    hypothetical — it cost a transcript table during development: one process
    had a table renamed away mid-migration when the other's CREATE TABLE IF
    NOT EXISTS decided it was missing and made a fresh empty one.

    A BEGIN IMMEDIATE inside migrate() can't cover this, because init_db()
    uses executescript (which commits, so it can't run inside a transaction).
    An flock on a sidecar file can: it's held across both calls, released on
    process exit even if that exit is a crash, and costs nothing when
    uncontended.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = db_path.with_suffix(db_path.suffix + ".schema-lock")
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


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
