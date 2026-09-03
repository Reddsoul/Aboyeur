"""search_nutrition_reference tool: FTS5 keyword search over the vintage
nutrition-science/diet-theory reference corpus (nutrition_reference table —
see aboyeur/reference_indexer.py), separate from the recipe corpus.

Reuses tools/search.py's tokenize-and-quote approach so free-text queries
(hyphens, apostrophes) can't trip FTS5's query-syntax parser.
"""

from __future__ import annotations

import re
import sqlite3

_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")
# words that carry no topical signal — drop so they don't dilate the OR
_STOPWORDS = frozenset(
    "a an the of to in on for and or is are was were be been being do does did "
    "with without from by as at it its this that these those we you they i how "
    "why what when where which who whom will would can could should may might "
    "our your their my his her also not no".split()
)


def _safe_fts_query(query: str) -> str | None:
    """Quote each token as a literal (so hyphens/apostrophes can't be parsed
    as FTS5 operators) and OR them together. OR, not implicit AND: reference
    chunks are paragraph-sized, so requiring every query word in one
    paragraph misses almost everything — BM25 ranks the chunks that match
    the most (and rarest) terms to the top instead."""
    tokens = [t for t in _FTS_TOKEN_RE.findall((query or "").lower()) if t not in _STOPWORDS]
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)


def search_nutrition_reference(conn: sqlite3.Connection, query: str, limit: int = 5) -> list[dict]:
    """Returns [{book, excerpt}], best BM25 match first. Empty list if the
    table doesn't exist yet (reference corpus never indexed) or the query
    has no searchable tokens — never raises, so a stale/missing table
    doesn't take down a chat turn."""
    limit = min(max(1, limit), 10)
    safe_query = _safe_fts_query(query)
    if safe_query is None:
        return []
    try:
        rows = conn.execute(
            """SELECT nr.book, nr.text, bm25(nutrition_reference_fts) AS rank
               FROM nutrition_reference_fts
               JOIN nutrition_reference nr ON nr.id = nutrition_reference_fts.rowid
               WHERE nutrition_reference_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (safe_query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"book": r["book"], "excerpt": r["text"]} for r in rows]
