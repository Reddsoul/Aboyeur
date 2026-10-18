"""One-time batch classifier: flag recipes that batch-cook and reheat well
(feature-spec-2026-09-26.md #2's meal-prep filter).

Heuristic MVP, same reasoning as classify_equipment.py's keyword scan: a
title/course check is a same-day win, and the spec's own recommendation for
the equipment classifier (ship the cheap keyword pass first, upgrade to an
LLM pass modeled on classify_recipes.py only if it proves too lossy)
applies just as well here — a batch-cook-or-not verdict is a coarser signal
than course, so it doesn't need per-recipe LLM judgement to start.

    uv run python -m aboyeur.classify_meal_prep            # all unclassified
    uv run python -m aboyeur.classify_meal_prep --book 660_Curries
    uv run python -m aboyeur.classify_meal_prep --rebuild  # re-scan everything
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import time

from aboyeur.config import Config
from aboyeur.db import connect

# Stews/braises/casseroles etc. — large-yield, stores and reheats well,
# nothing time-critical in the method.
_BATCH_FRIENDLY_WORDS = {
    "stew", "casserole", "chili", "chilli", "curry", "soup", "braise",
    "braised", "bolognese", "ragu", "ragout", "lasagna", "lasagne",
    "meatloaf", "pot roast", "potroast", "tagine", "gumbo", "dal", "daal",
    "chowder", "goulash", "cassoulet", "pot pie", "shepherd's pie",
    "shepherds pie", "cottage pie", "baked beans", "pulled pork", "brisket",
}

# Cooked to order / degrades fast on reheat — never meal-prep-friendly
# regardless of any batch-friendly word also present in the title.
_LAST_MINUTE_WORDS = {
    "stir-fry", "stir fry", "seared", "flash-fried", "flash fried",
    "grilled", "sashimi", "tartare", "souffle", "soufflé", "crepe", "crêpe",
    "omelet", "omelette", "pan-fried", "pan fried", "poached egg",
    "fried egg", "toast", "salad",
}

_WORD_RE = re.compile(r"[a-z']+")


def _phrase_hit(text: str, phrases: set[str]) -> bool:
    words = _WORD_RE.findall((text or "").lower())
    if not words:
        return False
    joined = " " + " ".join(words) + " "
    return any(f" {p} " in joined for p in phrases)


# A meal-prep candidate must be able to anchor a meal on its own — same
# reasoning as optimizer.is_meal_main_eligible, applied here so a side or
# dessert never gets tagged batch-cook-friendly regardless of its title.
_ELIGIBLE_COURSES = {"main", "unknown"}


def is_meal_prep_friendly(title: str, course: str | None) -> bool:
    if (course or "unknown").strip().lower() not in _ELIGIBLE_COURSES:
        return False
    if _phrase_hit(title, _LAST_MINUTE_WORDS):
        return False
    return _phrase_hit(title, _BATCH_FRIENDLY_WORDS)


def classify(
    conn: sqlite3.Connection,
    *, book: str | None = None, limit: int | None = None, rebuild: bool = False,
    progress: bool = True,
) -> dict:
    where = [] if rebuild else ["meal_prep_at IS NULL"]
    params: list = []
    if book:
        where.append("book = ?")
        params.append(book)
    sql = "SELECT id, title, course FROM recipes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()
    total = len(rows)
    done = 0
    flagged = 0
    started = time.time()

    for row in rows:
        friendly = is_meal_prep_friendly(row["title"], row["course"])
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute(
            "UPDATE recipes SET meal_prep = ?, meal_prep_at = ? WHERE id = ?",
            (1 if friendly else 0, now, row["id"]),
        )
        flagged += int(friendly)
        done += 1
        if done % 500 == 0:
            conn.commit()
            if progress:
                rate = done / max(1e-6, time.time() - started)
                eta = (total - done) / rate if rate else 0
                print(f"  {done}/{total}  {int(rate*60)}/min  eta {int(eta/60)}m  flagged {flagged}",
                      flush=True)
    conn.commit()
    return {"classified": done, "flagged": flagged}


def main() -> None:
    ap = argparse.ArgumentParser(prog="aboyeur.classify_meal_prep")
    ap.add_argument("--book", help="only classify recipes from this book")
    ap.add_argument("--limit", type=int, help="stop after N recipes")
    ap.add_argument("--rebuild", action="store_true", help="re-scan already-classified rows")
    args = ap.parse_args()

    config = Config.load()
    conn = connect(config.db_path)
    print(f"scanning meal-prep fit in {config.db_path} ...", flush=True)
    result = classify(conn, book=args.book, limit=args.limit, rebuild=args.rebuild)
    print(f"done: {result['classified']} classified — {result['flagged']} flagged meal-prep-friendly")


if __name__ == "__main__":
    main()
