"""search_recipes tool: FTS5 keyword search with hard allergen exclusion.

Allergens are excluded here, in code, before results ever reach the LLM.
This is the one place in the codebase where that must never be skipped —
see task.md §3 "Allergy filtering happens in code."
"""

from __future__ import annotations

import json
import re
import sqlite3

from aboyeur.engines import allergens

# FTS5 MATCH treats -, ", (, ), :, * etc. as query-syntax operators. A
# free-text query built from natural language (e.g. "Indian / Mexican" or
# any hyphenated word) can trip fts5's parser and raise sqlite3.OperationalError
# mid-request — which, inside a streaming chat response, aborts the connection
# outright rather than surfacing a clean error. Reduce to plain alnum/apostrophe
# tokens and quote each as a literal phrase so nothing can be parsed as an
# operator; FTS5's implicit AND between phrases keeps the same match behavior
# a plain keyword search wants.
_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")


def _safe_fts_query(query: str) -> str | None:
    tokens = _FTS_TOKEN_RE.findall(query or "")
    if not tokens:
        return None
    return " ".join(f'"{t}"' for t in tokens)


def search_recipes(
    conn: sqlite3.Connection,
    query: str,
    allergies: list[str] | None = None,
    cuisine: str | None = None,
    book: str | None = None,
    include_ingredients: list[str] | None = None,
    exclude_ingredients: list[str] | None = None,
    pantry: list[str] | None = None,
    limit: int = 25,
) -> list[dict]:
    """FTS5 search over title/ingredients/tags/cuisine/book.

    Returns compact dicts: id, title, book, cuisine, first 6 ingredients,
    pantry-match flag. Allergens are hard-excluded before results are built.
    """
    limit = min(max(1, limit), 25)
    # Expand category allergies ("shellfish" -> shrimp, octopus, ...) — see
    # engines/allergens.py. A literal match on the category word alone would
    # miss almost every actual matching recipe.
    allergies = allergens.expand_all([a for a in (allergies or []) if a.strip()])
    exclude_ingredients = [e.lower().strip() for e in (exclude_ingredients or []) if e.strip()]
    include_ingredients = [i.lower().strip() for i in (include_ingredients or []) if i.strip()]
    pantry = [p.lower().strip() for p in (pantry or []) if p.strip()]

    safe_query = _safe_fts_query(query)
    if safe_query is None:
        return []

    sql = """SELECT r.id, r.title, r.book, r.cuisine, r.ingredients_json
              FROM recipes_fts JOIN recipes r ON r.id = recipes_fts.rowid
              WHERE recipes_fts MATCH ?"""
    params: list = [safe_query]
    if cuisine:
        sql += " AND r.cuisine = ?"
        params.append(cuisine)
    if book:
        sql += " AND r.book = ?"
        params.append(book)
    sql += " ORDER BY bm25(recipes_fts) LIMIT ?"
    # Overfetch — allergy/ingredient filters happen in Python after the FTS hit,
    # since FTS5 MATCH can't express "ingredients_json does not contain X".
    params.append(limit * 4)

    rows = conn.execute(sql, params).fetchall()

    results = []
    for row in rows:
        ingredients = json.loads(row["ingredients_json"])
        ing_blob = " ".join(ingredients).lower()
        # Corpus frontmatter ingredient extraction is occasionally incomplete
        # (measured: ~0.4% of recipes mention a common allergen in the title
        # that's missing from their structured ingredients list — e.g. a
        # "...Peanut Cakes" recipe whose ingredients_json is just
        # ["yellow split peas"]). Also checking the title is cheap and closes
        # exactly that gap; it's defense-in-depth, not a substitute for
        # fixing extraction at the source.
        allergy_blob = ing_blob + " " + row["title"].lower()

        if allergies and any(a in allergy_blob for a in allergies):
            continue
        if exclude_ingredients and any(e in ing_blob for e in exclude_ingredients):
            continue
        if include_ingredients and not all(i in ing_blob for i in include_ingredients):
            continue

        results.append({
            "id": row["id"],
            "title": row["title"],
            "book": row["book"],
            "cuisine": row["cuisine"],
            "ingredients": ingredients[:6],
            "pantry_match": bool(pantry) and any(p in ing_blob for p in pantry),
        })
        if len(results) >= limit:
            break

    return results


def browse_recipes(
    conn: sqlite3.Connection,
    query: str = "",
    cuisine: str | None = None,
    book: str | None = None,
    allergies: list[str] | None = None,
    offset: int = 0,
    limit: int = 24,
    only_ids: set[int] | None = None,
) -> tuple[list[dict], int]:
    """Paginated catalog browsing (the Library UI's default grid) — unlike
    search_recipes, an empty query means "show everything" rather than no
    results, and results page with offset/limit instead of a hard 25 cap.
    Returns (page of results, total match count).

    total is a pre-allergy-filter SQL count (an upper bound, not exact) —
    the allergen check runs in Python same as search_recipes, since FTS
    can't express "ingredients_json does not contain X".

    only_ids restricts the whole query to a set of recipe ids — how the
    Library's Favorites section is built. Filtering in SQL rather than
    fetching a page and keeping the starred ones: over a 75k-recipe corpus
    the starred ones are almost never in the first page, which looked exactly
    like favorites not saving.
    """
    limit = min(max(1, limit), 100)
    offset = max(0, offset)
    allergies = allergens.expand_all([a for a in (allergies or []) if a.strip()])
    safe_query = _safe_fts_query(query) if (query or "").strip() else None

    conditions: list[str] = []
    params: list = []
    if only_ids is not None:
        if not only_ids:
            return [], 0
        conditions.append(f"r.id IN ({', '.join('?' * len(only_ids))})")
        params.extend(sorted(only_ids))
    if safe_query is not None:
        conditions.append("recipes_fts MATCH ?")
        params.append(safe_query)
    if cuisine:
        conditions.append("r.cuisine = ?")
        params.append(cuisine)
    if book:
        conditions.append("r.book = ?")
        params.append(book)

    from_clause = (
        "FROM recipes_fts JOIN recipes r ON r.id = recipes_fts.rowid"
        if safe_query is not None else "FROM recipes r"
    )
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    order_clause = "ORDER BY bm25(recipes_fts)" if safe_query is not None else "ORDER BY r.title"

    total_row = conn.execute(f"SELECT COUNT(*) AS n {from_clause} {where_clause}", params).fetchone()
    total = total_row["n"]

    # Overfetch when filtering by allergy in Python, same reasoning as
    # search_recipes — a page-sized SQL LIMIT could come back short.
    fetch_limit = limit * 4 if allergies else limit
    rows = conn.execute(
        f"SELECT r.id, r.title, r.book, r.cuisine, r.servings, r.ingredients_json, r.tags_json "
        f"{from_clause} {where_clause} {order_clause} LIMIT ? OFFSET ?",
        [*params, fetch_limit, offset],
    ).fetchall()

    results = []
    for row in rows:
        ingredients = json.loads(row["ingredients_json"])
        if allergies:
            blob = " ".join(ingredients).lower() + " " + row["title"].lower()
            if any(a in blob for a in allergies):
                continue
        results.append({
            "id": row["id"], "title": row["title"], "book": row["book"],
            "cuisine": row["cuisine"], "servings": row["servings"],
            "ingredients": ingredients, "tags": json.loads(row["tags_json"]),
        })
        if len(results) >= limit:
            break

    return results, total


def recipe_facets(conn: sqlite3.Connection) -> dict:
    """Distinct cuisines and cookbooks with counts, for the catalog sidebar."""
    cuisines = [
        {"value": row["cuisine"], "count": row["n"]}
        for row in conn.execute(
            "SELECT cuisine, COUNT(*) AS n FROM recipes "
            "WHERE cuisine IS NOT NULL GROUP BY cuisine ORDER BY n DESC"
        )
    ]
    books = [
        {"value": row["book"], "count": row["n"]}
        for row in conn.execute("SELECT book, COUNT(*) AS n FROM recipes GROUP BY book ORDER BY n DESC")
    ]
    return {"cuisines": cuisines, "books": books}
