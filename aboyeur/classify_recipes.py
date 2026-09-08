"""One-time batch classifier: label every recipe with a course, a rough
serving weight, and whether it's a complete meal.

Why this exists: the corpus is 74k Victorian/Edwardian cookbook recipes with
no course tags and `servings` almost always NULL. The menu path needs to know
"is this a dinner or a garnish" and "is this a dessert" — `optimizer.is_dessert`
et al. are a keyword fallback; this pass is the real signal, stored on the
`recipes` row (course / serving_est_g / meal_complete / classified_at).

Batched LLM calls (same client pattern as engines/nutrition._llm_parse_lines),
resumable (skips rows with classified_at set unless --rebuild), idempotent.
Falls back to a keyword heuristic for any row the model can't classify, so a
run always completes.

    uv run python -m aboyeur.classify_recipes            # all unclassified
    uv run python -m aboyeur.classify_recipes --book 660_Curries
    uv run python -m aboyeur.classify_recipes --rebuild  # re-do everything
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time

from aboyeur.config import Config
from aboyeur.db import connect
from aboyeur.engines import optimizer

_COURSES = ("main", "side", "breakfast", "dessert", "drink", "component", "unknown")

_SYSTEM_PROMPT = (
    "You label historical cookbook recipes. For each recipe return a JSON object:\n"
    '  "course": one of "main","side","breakfast","dessert","drink","component"\n'
    '  "serving_est_g": integer grams for ONE serving of the finished dish (a '
    "main ~350-550, a side ~120-250, a sauce/component ~30-80, a drink ~250)\n"
    '  "meal_complete": true only if one serving could stand alone as a meal '
    "(has a protein or hearty legume/grain base AND enough substance) — a plain "
    "vegetable side, a sauce, a biscuit, a drink are all false.\n"
    "Rules: 'component' = sauces, dressings, fillings, icings, spice blends, "
    "pastry doughs, garnishes, pickles/preserves — anything served WITH food, "
    "not as it. 'drink' = teas, coffees, cocktails, punches, cordials, ades. "
    "'dessert' = cakes, pies, puddings, cookies, candies, ices. A pie or pudding "
    "with meat/fish/vegetables as the filling is 'main', not 'dessert'.\n"
    "Reply with ONLY a JSON array, one object per recipe, same order. No prose."
)

_MAX_STEPS_CHARS = 240


def _heuristic(title: str, ingredients: list[str]) -> dict:
    """Keyword fallback — mirrors optimizer's menu-eligibility rules."""
    if optimizer.is_drink(title):
        return {"course": "drink", "serving_est_g": 250, "meal_complete": 0}
    if optimizer.is_dessert(title):
        return {"course": "dessert", "serving_est_g": 120, "meal_complete": 0}
    if optimizer.is_component(title, ingredients):
        return {"course": "component", "serving_est_g": 60, "meal_complete": 0}
    if optimizer.has_protein(ingredients) and len(ingredients) >= 4:
        return {"course": "main", "serving_est_g": 400, "meal_complete": 1}
    return {"course": "unknown", "serving_est_g": None, "meal_complete": 0}


def _coerce(item: dict, title: str, ingredients: list[str]) -> dict:
    try:
        course = str(item.get("course", "")).strip().lower()
        if course not in _COURSES or course == "unknown":
            raise ValueError
        g = item.get("serving_est_g")
        g = float(g) if g not in (None, "") else None
        if g is not None and not (5 <= g <= 2000):
            g = None
        mc = 1 if item.get("meal_complete") in (True, 1, "true", "True") else 0
        # A dessert/drink/component is never a standalone meal.
        if course in ("dessert", "drink", "component", "side"):
            mc = 0
        return {"course": course, "serving_est_g": g, "meal_complete": mc}
    except (ValueError, TypeError, AttributeError):
        return _heuristic(title, ingredients)


def _classify_batch(config: Config, rows: list[sqlite3.Row]) -> list[dict]:
    from openai import OpenAI

    payload = []
    for r in rows:
        ings = json.loads(r["ingredients_json"])
        payload.append({"title": r["title"], "ingredients": ings[:20]})
    try:
        client = OpenAI(base_url=config.llm_base_url,
                        api_key=config.llm_api_key or "unused",
                        timeout=120, max_retries=1)
        resp = client.chat.completions.create(
            model=config.llm_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            temperature=0,
        )
        content = (resp.choices[0].message.content or "").strip()
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
        parsed = json.loads(content)
        if not isinstance(parsed, list) or len(parsed) != len(rows):
            raise ValueError("shape mismatch")
    except Exception:
        parsed = [None] * len(rows)

    out = []
    for r, item in zip(rows, parsed):
        ings = json.loads(r["ingredients_json"])
        if isinstance(item, dict):
            out.append(_coerce(item, r["title"], ings))
        else:
            out.append(_heuristic(r["title"], ings))
    return out


def classify(
    conn: sqlite3.Connection, config: Config,
    *, book: str | None = None, limit: int | None = None,
    batch_size: int = 40, rebuild: bool = False, progress: bool = True,
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
    done = 0
    counts: dict[str, int] = {c: 0 for c in _COURSES}
    now = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for start in range(0, total, batch_size):
        chunk = rows[start:start + batch_size]
        results = _classify_batch(config, chunk)
        for r, res in zip(chunk, results):
            conn.execute(
                "UPDATE recipes SET course = ?, serving_est_g = ?, "
                "meal_complete = ?, classified_at = ? WHERE id = ?",
                (res["course"], res["serving_est_g"], res["meal_complete"], now(), r["id"]),
            )
            counts[res["course"]] = counts.get(res["course"], 0) + 1
        conn.commit()
        done += len(chunk)
        if progress:
            print(f"  {done}/{total}  {counts}", flush=True)

    return {"classified": done, "counts": counts}


def main() -> None:
    ap = argparse.ArgumentParser(prog="aboyeur.classify_recipes")
    ap.add_argument("--book", help="only classify recipes from this book")
    ap.add_argument("--limit", type=int, help="stop after N recipes")
    ap.add_argument("--batch-size", type=int, default=40)
    ap.add_argument("--rebuild", action="store_true", help="re-classify already-labeled rows")
    args = ap.parse_args()

    config = Config.load()
    conn = connect(config.db_path)
    print(f"classifying recipes in {config.db_path} ...", flush=True)
    result = classify(
        conn, config, book=args.book, limit=args.limit,
        batch_size=args.batch_size, rebuild=args.rebuild,
    )
    print(f"done: {result['classified']} classified — {result['counts']}")


if __name__ == "__main__":
    main()
