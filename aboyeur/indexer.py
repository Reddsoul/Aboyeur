"""Corpus walker/parser. Builds/updates the SQLite recipe index from markdown files.

Recipe files look like:

    ---
    title: Achar Gosht
    cuisine: Indian
    ingredients:
      - mustard oil
      - ...
    tags:
      - recipe
      - cuisine/Indian
    ---

    # Achar Gosht

    ## Ingredients
    - 2 tablespoons mustard oil
    ...

    ## Instructions
    ...

    ## Notes
    SERVES 6

Non-recipe files (index pages under `_Ingredients/`, `_Cuisines/`, the
dashboard, dotfiles, `__pycache__`) are skipped by directory shape: a real
recipe lives exactly one level under RECIPES_DIR, in a book directory that
doesn't start with `_` or `.`.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import yaml

from aboyeur.config import Config
from aboyeur.db import connect, init_db

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n(.*)$", re.DOTALL)
_SERVES_RE = re.compile(r"SERVES\s+(\d+)", re.IGNORECASE)


@dataclass
class IndexStats:
    scanned: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_no_frontmatter: int = 0
    skipped_malformed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ParsedRecipe:
    path: str
    title: str
    book: str
    cuisine: str | None
    servings: int | None
    ingredients: list[str]
    tags: list[str]
    mtime: float
    nutrition: dict | None = None  # frontmatter nutrition block, if present


def iter_recipe_entries(recipes_dir: Path) -> Iterator[tuple[Path, float]]:
    """Yield (path, mtime) for candidate recipe files: one level deep, book
    dir not starting with _ or .

    Uses os.scandir() rather than Path.glob() + a separate .stat() call, to
    avoid an explicit redundant stat. In practice this doesn't meaningfully
    speed up a cold run over SMB (measured ~8s/1000 files either way,
    within noise) — macOS's SMB client apparently doesn't reuse readdir
    attributes across a second stat() the way local filesystems do, so a
    full incremental pass over 60k files still costs ~2 minutes regardless
    of API choice. That's the real ceiling: per-file network round-trips,
    not a fixable Python-level inefficiency. A future incremental design
    should avoid the full-corpus walk entirely (e.g. only re-stat books
    whose directory mtime changed, or an fswatch-driven index).

    Dedupes by path. The SMB client has been observed to replay full
    directory listings for large book dirs (seen: every file in a ~1000-file
    dir returned 8x from a single listing) — a mount-level readdir bug, not
    a corpus issue. Silently absorbing it here is cheap and correct either
    way, since a path can never legitimately need indexing twice in one run.
    """
    seen: set[str] = set()
    for book_dir in sorted(recipes_dir.iterdir()):
        if not book_dir.is_dir() or book_dir.name.startswith(("_", ".")):
            continue
        if book_dir.name == "__pycache__":
            continue
        entries = sorted(
            (e for e in os.scandir(book_dir) if e.name.endswith(".md")),
            key=lambda e: e.name,
        )
        for entry in entries:
            if entry.path in seen:
                continue
            seen.add(entry.path)
            yield Path(entry.path), entry.stat().st_mtime


def iter_recipe_files(recipes_dir: Path) -> Iterator[Path]:
    """Yield candidate recipe file paths (no mtime). See iter_recipe_entries."""
    for path, _mtime in iter_recipe_entries(recipes_dir):
        yield path


def parse_recipe_file(path: Path, mtime: float | None = None) -> ParsedRecipe | None:
    """Parse one recipe file. Returns None if it has no usable frontmatter.

    Pass `mtime` when the caller already has it (e.g. from scandir) to skip
    a redundant stat() call — significant over SMB.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None

    fm_text, body = m.group(1), m.group(2)
    front = yaml.safe_load(fm_text) or {}
    if not isinstance(front, dict):
        return None

    title = front.get("title")
    ingredients = front.get("ingredients") or []
    if not title or not isinstance(ingredients, list):
        return None

    tags = front.get("tags") or []
    cuisine = front.get("cuisine")

    servings = None
    serves_m = _SERVES_RE.search(body)
    if serves_m:
        servings = int(serves_m.group(1))

    # Optional nutrition block, written back by estimate_nutrition — lets a
    # DB rebuild recover past estimates without re-hitting the LLM/USDA.
    nutrition = front.get("nutrition")
    if not isinstance(nutrition, dict) or nutrition.get("kcal") is None:
        nutrition = None

    return ParsedRecipe(
        path=str(path),
        title=str(title).strip(),
        book=path.parent.name,
        cuisine=str(cuisine).strip() if cuisine else None,
        servings=servings,
        ingredients=[str(i) for i in ingredients],
        tags=[str(t) for t in tags],
        mtime=mtime if mtime is not None else path.stat().st_mtime,
        nutrition=nutrition,
    )


def _existing_mtimes(conn: sqlite3.Connection) -> dict[str, float]:
    return {row["path"]: row["mtime"] for row in conn.execute("SELECT path, mtime FROM recipes")}


def _upsert(conn: sqlite3.Connection, r: ParsedRecipe) -> str:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cur = conn.execute("SELECT id FROM recipes WHERE path = ?", (r.path,))
    row = cur.fetchone()
    if row is None:
        cur = conn.execute(
            """INSERT INTO recipes
               (path, title, book, cuisine, servings, ingredients_json, tags_json, mtime, indexed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r.path, r.title, r.book, r.cuisine, r.servings,
                json.dumps(r.ingredients), json.dumps(r.tags), r.mtime, now,
            ),
        )
        _upsert_nutrition(conn, cur.lastrowid, r.nutrition)
        return "inserted"
    conn.execute(
        """UPDATE recipes SET title=?, book=?, cuisine=?, servings=?,
           ingredients_json=?, tags_json=?, mtime=?, indexed_at=?
           WHERE path=?""",
        (
            r.title, r.book, r.cuisine, r.servings,
            json.dumps(r.ingredients), json.dumps(r.tags), r.mtime, now, r.path,
        ),
    )
    _upsert_nutrition(conn, row["id"], r.nutrition)
    return "updated"


def _upsert_nutrition(conn: sqlite3.Connection, recipe_id: int, nutrition: dict | None) -> None:
    """Restore a frontmatter nutrition block into the nutrition table, so a
    rebuilt DB doesn't re-estimate recipes that were already resolved."""
    if not nutrition:
        return
    try:
        conn.execute(
            """INSERT INTO nutrition (recipe_id, kcal, protein_g, carbs_g, fat_g, fiber_g, per_serving, confidence, estimated_at, servings_est)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT(recipe_id) DO UPDATE SET kcal=excluded.kcal, protein_g=excluded.protein_g,
               carbs_g=excluded.carbs_g, fat_g=excluded.fat_g, fiber_g=excluded.fiber_g,
               confidence=excluded.confidence, estimated_at=excluded.estimated_at,
               servings_est=excluded.servings_est""",
            (
                recipe_id,
                float(nutrition.get("kcal") or 0),
                float(nutrition.get("protein_g") or 0),
                float(nutrition.get("carbs_g") or 0),
                float(nutrition.get("fat_g") or 0),
                float(nutrition.get("fiber_g") or 0),
                str(nutrition.get("confidence") or "medium"),
                str(nutrition.get("estimated_at") or ""),
                int(nutrition["servings_est"]) if nutrition.get("servings_est") else None,
            ),
        )
    except (TypeError, ValueError):
        pass  # a hand-mangled block never blocks indexing the recipe itself


def index_corpus(config: Config, rebuild: bool = False, progress_cb=None) -> IndexStats:
    """Walk RECIPES_DIR and upsert into the DB. Incremental by (path, mtime) unless rebuild=True.

    progress_cb(scanned, total) is called periodically if given (for CLI progress bars).
    Raises FileNotFoundError via config.check_recipes_dir() if the mount is missing/stale.
    """
    config.check_recipes_dir()
    conn = connect(config.db_path)
    init_db(conn)

    stats = IndexStats()
    existing = {} if rebuild else _existing_mtimes(conn)

    entries = list(iter_recipe_entries(config.recipes_dir))
    total = len(entries)

    for i, (path, mtime) in enumerate(entries, start=1):
        stats.scanned += 1
        path_str = str(path)
        try:
            if not rebuild and path_str in existing and existing[path_str] == mtime:
                stats.unchanged += 1
                if progress_cb and i % 200 == 0:
                    progress_cb(i, total)
                continue

            parsed = parse_recipe_file(path, mtime=mtime)
            if parsed is None:
                stats.skipped_no_frontmatter += 1
                continue

            action = _upsert(conn, parsed)
            if action == "inserted":
                stats.inserted += 1
            else:
                stats.updated += 1

            if i % 200 == 0:
                conn.commit()
                if progress_cb:
                    progress_cb(i, total)
        except (yaml.YAMLError, ValueError) as e:
            stats.skipped_malformed += 1
            stats.errors.append(f"{path_str}: {e}")
        except OSError as e:
            stats.errors.append(f"{path_str}: {e}")

    conn.commit()
    if progress_cb:
        progress_cb(total, total)
    conn.close()
    return stats
