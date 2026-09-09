"""One-time batch classifier: label every recipe with a course, a rough
serving weight, and whether it's a complete meal.

Why this exists: the corpus is 74k Victorian/Edwardian cookbook recipes with
no course tags and `servings` almost always NULL. The menu path needs to know
"is this a dinner or a garnish" and "is this a dessert" — `optimizer.is_dessert`
et al. are a keyword fallback; this pass is the real signal, stored on the
`recipes` row (course / serving_est_g / meal_complete / classified_at).

The local model is weak at multi-field structured output, so it is asked for
ONE thing per recipe — the course — and `serving_est_g` / `meal_complete` are
derived from that. Small batches, tolerant parsing (course synonyms
normalized), per-item keyword fallback so a run always completes.

    uv run python -m aboyeur.classify_recipes            # all unclassified
    uv run python -m aboyeur.classify_recipes --book 660_Curries
    uv run python -m aboyeur.classify_recipes --rebuild  # re-do everything
"""

from __future__ import annotations

import argparse
import concurrent.futures as _cf
import json
import re
import sqlite3
import time

from aboyeur.config import Config
from aboyeur.db import connect
from aboyeur.engines import optimizer

_COURSES = ("main", "side", "breakfast", "dessert", "drink", "component")

# Typical finished-serving weight + whether one serving stands alone as a meal.
_COURSE_DEFAULTS = {
    "main":      (430.0, 1),
    "breakfast": (350.0, 1),
    "side":      (180.0, 0),
    "dessert":   (110.0, 0),
    "drink":     (250.0, 0),
    "component": (55.0, 0),
    "unknown":   (None, 0),
}

# The model says "main course", "entree", "beverage", "condiment", ... — map
# every reasonable phrasing onto our six buckets.
_COURSE_SYNONYMS = {
    "main": "main", "main course": "main", "main dish": "main", "entree": "main",
    "entrée": "main", "dinner": "main", "supper": "main", "lunch": "main",
    "meat": "main", "fish": "main", "poultry": "main", "casserole": "main",
    "stew": "main", "soup": "main", "curry": "main",
    "side": "side", "side dish": "side", "vegetable": "side", "salad": "side",
    "accompaniment": "side", "starch": "side",
    "breakfast": "breakfast", "brunch": "breakfast",
    "dessert": "dessert", "sweet": "dessert", "pudding": "dessert",
    "cake": "dessert", "pastry": "dessert", "confection": "dessert",
    "candy": "dessert", "ice": "dessert", "baked good": "dessert",
    "drink": "drink", "beverage": "drink", "cocktail": "drink", "tea": "drink",
    "coffee": "drink",
    "component": "component", "condiment": "component", "sauce": "component",
    "dressing": "component", "spread": "component", "garnish": "component",
    "seasoning": "component", "filling": "component", "preserve": "component",
    "pickle": "component", "dough": "component", "base": "component",
}

_SYSTEM_PROMPT = (
    "You label historical cookbook recipes by course. For each recipe in the "
    "input array, decide ONE label:\n"
    '  "main"      - a dinner/lunch centre: meat, fish, poultry, egg, hearty '
    "bean or grain dish, substantial soup or stew, savoury pie/pudding\n"
    '  "side"      - a vegetable, salad, plain starch, or small accompaniment\n'
    '  "breakfast" - porridge, pancakes, breakfast eggs, breakfast breads\n'
    '  "dessert"   - cake, pie, pudding, cookie, candy, ice, sweet compote\n'
    '  "drink"     - tea, coffee, cocoa, punch, cordial, cocktail, ade\n'
    '  "component" - sauce, gravy, dressing, filling, icing, spice blend, '
    "pastry dough, pickle, preserve, garnish - served WITH food, not as it\n"
    "A pie or pudding with meat/fish/vegetable filling is \"main\", not "
    '"dessert". Reply with ONLY a JSON array of lowercase label strings, one '
    'per recipe, same order, e.g. ["main","dessert","side"]. No prose.'
)


def _heuristic_course(title: str, ingredients: list[str]) -> str:
    if optimizer.is_drink(title):
        return "drink"
    if optimizer.is_dessert(title):
        return "dessert"
    if optimizer.is_component(title, ingredients):
        return "component"
    if optimizer.has_protein(ingredients) and len(ingredients) >= 4:
        return "main"
    return "unknown"


def _row_fields(course: str) -> tuple:
    g, mc = _COURSE_DEFAULTS.get(course, (None, 0))
    return course, g, mc


def _normalize_course(raw, title: str, ingredients: list[str]) -> str:
    if isinstance(raw, dict):
        raw = raw.get("course") or raw.get("label") or ""
    s = str(raw or "").strip().lower()
    s = re.sub(r"[^a-z ]", "", s).strip()
    if s in _COURSES:
        return s
    if s in _COURSE_SYNONYMS:
        return _COURSE_SYNONYMS[s]
    for word in s.split():
        if word in _COURSE_SYNONYMS:
            return _COURSE_SYNONYMS[word]
    return _heuristic_course(title, ingredients)


def _classify_batch(config: Config, rows: list[sqlite3.Row]) -> list[str]:
    from openai import OpenAI

    payload = [
        {"title": r["title"], "ingredients": json.loads(r["ingredients_json"])[:8]}
        for r in rows
    ]
    labels: list = [None] * len(rows)
    try:
        client = OpenAI(base_url=config.llm_base_url,
                        api_key=config.llm_api_key or "unused",
                        timeout=90, max_retries=1)
        resp = client.chat.completions.create(
            model=config.llm_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            temperature=0,
        )
        content = (resp.choices[0].message.content or "").strip()
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
        m = re.search(r"\[.*\]", content, re.DOTALL)
        parsed = json.loads(m.group(0) if m else content)
        if isinstance(parsed, list) and len(parsed) == len(rows):
            labels = parsed
    except Exception:
        pass

    out = []
    for r, raw in zip(rows, labels):
        ings = json.loads(r["ingredients_json"])
        out.append(_normalize_course(raw, r["title"], ings))
    return out


def classify(
    conn: sqlite3.Connection, config: Config,
    *, book: str | None = None, limit: int | None = None,
    batch_size: int = 12, workers: int = 4, rebuild: bool = False,
    progress: bool = True,
) -> dict:
    where = [] if rebuild else ["classified_at IS NULL"]
    params: list = []
    if book:
        where.append("book = ?")
        params.append(book)
    sql = "SELECT id, title, book, ingredients_json FROM recipes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()
    total = len(rows)
    chunks = [rows[i:i + batch_size] for i in range(0, total, batch_size)]
    done = 0
    counts: dict[str, int] = {}
    started = time.time()

    # `workers` batches classified concurrently (the LLM proxy serializes some,
    # but overlap still ~2-3x's throughput). DB writes stay on this thread —
    # one sqlite connection, committed per group of results.
    with _cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for wave_start in range(0, len(chunks), max(1, workers)):
            wave = chunks[wave_start:wave_start + max(1, workers)]
            results = list(pool.map(lambda ch: (ch, _classify_batch(config, ch)), wave))
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for chunk, courses in results:
                for r, course in zip(chunk, courses):
                    course, g, mc = _row_fields(course)
                    conn.execute(
                        "UPDATE recipes SET course = ?, serving_est_g = ?, "
                        "meal_complete = ?, classified_at = ? WHERE id = ?",
                        (course, g, mc, now, r["id"]),
                    )
                    counts[course] = counts.get(course, 0) + 1
                    done += 1
            conn.commit()
            if progress:
                rate = done / max(1e-6, time.time() - started)
                eta = (total - done) / rate if rate else 0
                print(f"  {done}/{total}  {int(rate*60)}/min  eta {int(eta/60)}m  {counts}",
                      flush=True)

    return {"classified": done, "counts": counts}


def main() -> None:
    ap = argparse.ArgumentParser(prog="aboyeur.classify_recipes")
    ap.add_argument("--book", help="only classify recipes from this book")
    ap.add_argument("--limit", type=int, help="stop after N recipes")
    ap.add_argument("--batch-size", type=int, default=12)
    ap.add_argument("--workers", type=int, default=4, help="concurrent LLM batches")
    ap.add_argument("--rebuild", action="store_true", help="re-classify already-labeled rows")
    args = ap.parse_args()

    config = Config.load()
    conn = connect(config.db_path)
    print(f"classifying recipes in {config.db_path} ...", flush=True)
    result = classify(
        conn, config, book=args.book, limit=args.limit,
        batch_size=args.batch_size, workers=args.workers, rebuild=args.rebuild,
    )
    print(f"done: {result['classified']} classified — {result['counts']}")


if __name__ == "__main__":
    main()
