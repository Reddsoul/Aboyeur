"""One-time batch classifier: tag every recipe with the equipment its
instructions actually require (feature-spec-2026-09-26.md #5).

MVP by design, per the spec: a keyword scan over the recipe's full text
(title + ingredients + instructions), not an LLM pass. Cheap, same-day,
good enough to hard-exclude the obvious cases (a recipe that says "blend
until smooth" needs a blender); a keyword miss just leaves a recipe
unclassified (equipment_json NULL), which the planner never treats as
"needs nothing" — see engines/optimizer.equipment_blocked. Move to an LLM
classifier pass (modeled on classify_recipes.py) if the keyword list proves
too lossy in practice.

Tag vocabulary matches aboyeur/tools/profile.py's DEFAULT_TOOLS_AVAILABLE
and tools_available patch keys, so a classified recipe's equipment_json
lines up 1:1 with what a client can toggle.

    uv run python -m aboyeur.classify_equipment            # all unclassified
    uv run python -m aboyeur.classify_equipment --book 660_Curries
    uv run python -m aboyeur.classify_equipment --rebuild  # re-scan everything
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time

from aboyeur.config import Config
from aboyeur.db import connect
from aboyeur.tools.recipe import resolve_recipe_path

# Word/phrase -> tool tag. Order doesn't matter — every match is collected,
# not just the first. Phrases are matched on word boundaries via the same
# regex-escape + join approach classify_recipes/optimizer use elsewhere, so
# "wok" doesn't fire inside some unrelated longer word.
_KEYWORDS: dict[str, list[str]] = {
    "oven": [r"\boven\b", r"\bbak(?:e|ing|ed)\b", r"\broast(?:ing|ed)?\b", r"\bbroil(?:er|ing|ed)?\b"],
    "stovetop": [r"\bstovetop\b", r"\bskillet\b", r"\bsaucepan\b", r"\bfrying pan\b",
                 r"\bsimmer(?:ing|ed)?\b", r"\bsaut[ée](?:ing|ed)?\b"],
    "blender": [r"\bblender\b", r"\bblend(?:ing|ed)? until\b"],
    "food_processor": [r"\bfood processor\b"],
    "slow_cooker": [r"\bslow cooker\b", r"\bcrock[- ]?pot\b"],
    "instant_pot": [r"\binstant pot\b", r"\bpressure cooker\b"],
    "air_fryer": [r"\bair[- ]?fryer\b"],
    "wok": [r"\bwok\b"],
    "grill": [r"\bgrill(?:ing|ed)?\b", r"\bbarbecue\b", r"\bbbq\b"],
    "stand_mixer": [r"\bstand mixer\b", r"\belectric mixer\b", r"\bhand mixer\b"],
    "dutch_oven": [r"\bdutch oven\b"],
    "sous_vide": [r"\bsous vide\b"],
    "grinder": [r"\bmeat grinder\b", r"\bspice grinder\b"],
    "steamer": [r"\bsteamer\b", r"\bsteaming basket\b"],
}
_COMPILED = {tag: [re.compile(p, re.IGNORECASE) for p in pats] for tag, pats in _KEYWORDS.items()}


def keyword_equipment(text: str) -> list[str]:
    """Every tag whose keyword pattern fires anywhere in `text`. Empty list
    (not None) means "scanned, needs nothing beyond the basics" — distinct
    from an unscanned recipe, whose equipment_json stays NULL."""
    text = text or ""
    return sorted(tag for tag, patterns in _COMPILED.items() if any(p.search(text) for p in patterns))


def classify(
    conn: sqlite3.Connection,
    *, book: str | None = None, limit: int | None = None, rebuild: bool = False,
    progress: bool = True,
) -> dict:
    where = [] if rebuild else ["equipment_classified_at IS NULL"]
    params: list = []
    if book:
        where.append("book = ?")
        params.append(book)
    sql = "SELECT id, path FROM recipes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()
    total = len(rows)
    done = 0
    tag_counts: dict[str, int] = {}
    started = time.time()

    for row in rows:
        path = resolve_recipe_path(row["path"])
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        tags = keyword_equipment(text)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute(
            "UPDATE recipes SET equipment_json = ?, equipment_classified_at = ? WHERE id = ?",
            (json.dumps(tags), now, row["id"]),
        )
        for t in tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1
        done += 1
        if done % 500 == 0:
            conn.commit()
            if progress:
                rate = done / max(1e-6, time.time() - started)
                eta = (total - done) / rate if rate else 0
                print(f"  {done}/{total}  {int(rate*60)}/min  eta {int(eta/60)}m", flush=True)
    conn.commit()
    return {"classified": done, "tag_counts": tag_counts}


def main() -> None:
    ap = argparse.ArgumentParser(prog="aboyeur.classify_equipment")
    ap.add_argument("--book", help="only classify recipes from this book")
    ap.add_argument("--limit", type=int, help="stop after N recipes")
    ap.add_argument("--rebuild", action="store_true", help="re-scan already-classified rows")
    args = ap.parse_args()

    config = Config.load()
    conn = connect(config.db_path)
    print(f"scanning recipe equipment in {config.db_path} ...", flush=True)
    result = classify(conn, book=args.book, limit=args.limit, rebuild=args.rebuild)
    print(f"done: {result['classified']} classified — {result['tag_counts']}")


if __name__ == "__main__":
    main()
