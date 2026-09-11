"""Batch nutrition estimator: fill the `nutrition` table for every main /
breakfast / side in the corpus, so plan_menu's pool widening has real
numbers to work with instead of estimating 10-30 recipes live per turn.

Three resumable phases, each idempotent through the caches it fills:

  parse  — every unique un-parsed ingredient line of the target recipes,
           batched through the LLM (`ingredient_parse` table). Lines are
           shared across recipes ("2 eggs" appears thousands of times), so
           this is ~43k lines for ~39k recipes, not 275k.
  usda   — every unique parsed food not yet in `usda_cache`, one lookup
           each, paced under FoodData Central's 1,000 requests/hour/IP; a
           429 pauses the run for Retry-After and resumes. This is the slow
           phase (hours) — run it in the background.
  score  — per-recipe estimate from the two caches (fast), persisted to the
           nutrition table and, unless --no-frontmatter, the recipe .md.

Run:  uv run python -m aboyeur.estimate_nutrition_batch [--phase all]
      [--courses main,breakfast,side] [--limit N] [--book B] [--workers 4]
      [--batch-size 40] [--usda-per-hour 700] [--refresh-fiber]
      [--no-frontmatter]

Safe to stop and restart at any point. Runs against the live DB alongside
the backend (WAL; separate connection).
"""

from __future__ import annotations

import argparse
import functools
import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from aboyeur.config import Config
from aboyeur.db import connect, init_db, schema_lock
from aboyeur.engines import nutrition as nutrition_engine
from aboyeur.tools.nutrition import estimate_nutrition

DEFAULT_COURSES = ("main", "breakfast", "side")

# Progress lines must show up when stdout is a pipe/log file (nohup, tee).
_print = functools.partial(print, flush=True)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def target_recipe_ids(
    conn: sqlite3.Connection, courses: tuple[str, ...], limit: int | None = None,
    book: str | None = None,
) -> list[int]:
    """Recipes of the wanted courses with no nutrition row yet."""
    placeholders = ",".join("?" * len(courses))
    sql = (
        f"SELECT r.id FROM recipes r LEFT JOIN nutrition n ON n.recipe_id = r.id "
        f"WHERE n.recipe_id IS NULL AND r.course IN ({placeholders})"
    )
    params: list = list(courses)
    if book:
        sql += " AND r.book = ?"
        params.append(book)
    sql += " ORDER BY r.id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [r["id"] for r in conn.execute(sql, params).fetchall()]


def _target_lines(conn: sqlite3.Connection, ids: list[int]) -> dict[str, str]:
    """{normalized line key: one original spelling} across the target recipes."""
    lines: dict[str, str] = {}
    for chunk in _chunks(ids, 500):
        placeholders = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT ingredients_json FROM recipes WHERE id IN ({placeholders})", chunk
        ):
            for line in json.loads(r["ingredients_json"]):
                key = (line or "").strip().lower()
                if key and key not in lines:
                    lines[key] = line
    return lines


def _chunks(seq: list, n: int) -> list[list]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


# ---------------------------------------------------------------------------
# Phase 1: LLM ingredient parse
# ---------------------------------------------------------------------------

def _parse_batch(config: Config, lines: list[str]) -> list[dict | None]:
    """Parse a batch; on failure bisect so one bad line doesn't sink 39 good
    ones (a lone failure costs ~log2(batch) extra calls). Unparseable lines
    come back None (the score phase falls back to the heuristic for them,
    uncached, as usual)."""
    parsed = nutrition_engine._llm_parse_lines(config, lines)
    if parsed is not None:
        return list(parsed)
    if len(lines) <= 1:
        return [None] * len(lines)
    mid = len(lines) // 2
    return _parse_batch(config, lines[:mid]) + _parse_batch(config, lines[mid:])


def phase_parse(
    conn: sqlite3.Connection, config: Config, ids: list[int],
    *, batch_size: int = 40, workers: int = 4, log=_print,
) -> dict:
    all_lines = _target_lines(conn, ids)
    cached = {r["raw"] for r in conn.execute("SELECT raw FROM ingredient_parse")}
    todo = [(k, v) for k, v in all_lines.items() if k not in cached]
    log(f"[parse] {len(all_lines)} unique lines across {len(ids)} recipes; "
        f"{len(todo)} to parse ({len(all_lines) - len(todo)} cached)")
    if not todo:
        return {"lines": len(all_lines), "parsed": 0, "failed": 0}

    batches = _chunks(todo, batch_size)
    parsed_n = failed_n = 0
    started = time.monotonic()
    lock = threading.Lock()

    def run(batch: list[tuple[str, str]]):
        return batch, _parse_batch(config, [orig for _, orig in batch])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (batch, results) in enumerate(pool.map(run, batches), 1):
            rows = []
            for (key, _), parsed in zip(batch, results):
                if parsed is None:
                    failed_n += 1
                    continue
                rows.append((key, parsed["food"], parsed["grams"], _now()))
            with lock:
                conn.executemany(
                    "INSERT INTO ingredient_parse (raw, food, grams, parsed_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(raw) DO UPDATE SET food=excluded.food, grams=excluded.grams, "
                    "parsed_at=excluded.parsed_at",
                    rows,
                )
                conn.commit()
            parsed_n += len(rows)
            if i % 10 == 0 or i == len(batches):
                el = time.monotonic() - started
                rate = (parsed_n + failed_n) / el if el else 0
                left = (len(todo) - parsed_n - failed_n) / rate if rate else 0
                log(f"[parse] {i}/{len(batches)} batches · {parsed_n} parsed · "
                    f"{failed_n} failed · {el / 60:.1f} min · ~{left / 60:.0f} min left")
    return {"lines": len(all_lines), "parsed": parsed_n, "failed": failed_n}


# ---------------------------------------------------------------------------
# Phase 2: USDA lookups
# ---------------------------------------------------------------------------

def _target_foods(conn: sqlite3.Connection, ids: list[int]) -> list[str]:
    """Distinct food nouns the score phase will look up for these recipes:
    the parsed food for cached lines, the heuristic-cleaned name otherwise."""
    lines = _target_lines(conn, ids)
    parsed = {r["raw"]: r["food"] for r in conn.execute("SELECT raw, food FROM ingredient_parse")}
    foods: set[str] = set()
    for key in lines:
        food = parsed.get(key)
        if food is None:
            food = nutrition_engine.clean_ingredient_name(key) or key
        food = (food or "").strip().lower()
        if food:
            foods.add(food)
    return sorted(foods)


def phase_usda(
    conn: sqlite3.Connection, config: Config, ids: list[int],
    *, per_hour: int = 700, refresh_fiber: bool = False, log=_print,
) -> dict:
    if not config.usda_api_key:
        log("[usda] no USDA_API_KEY configured — skipping (estimates would all be low-confidence)")
        return {"foods": 0, "looked_up": 0, "hits": 0, "misses": 0}
    foods = _target_foods(conn, ids)
    if refresh_fiber:
        # Entries cached before fiber (nutrient 1079) was tracked carry no
        # fiber_g; drop them so they're fetched again with it.
        stale = [
            r["ingredient"] for r in conn.execute(
                "SELECT ingredient FROM usda_cache WHERE macros_json NOT LIKE '%fiber_g%'"
            )
        ]
        stale = [f for f in stale if f in set(foods)]
        for chunk in _chunks(stale, 500):
            conn.execute(
                f"DELETE FROM usda_cache WHERE ingredient IN ({','.join('?' * len(chunk))})", chunk
            )
        conn.commit()
        log(f"[usda] dropped {len(stale)} fiber-less cache entries for re-fetch")
    cached = {r["ingredient"] for r in conn.execute("SELECT ingredient FROM usda_cache")}
    todo = [f for f in foods if f not in cached]
    log(f"[usda] {len(foods)} distinct foods; {len(todo)} to look up "
        f"({len(foods) - len(todo)} cached) · ~{len(todo) / max(1, per_hour):.1f} h at {per_hour}/h")

    min_gap = 3600.0 / max(1, per_hour)
    hits = misses = 0
    started = time.monotonic()
    for i, food in enumerate(todo, 1):
        while True:
            t0 = time.monotonic()
            res = nutrition_engine.lookup_ingredient_macros(conn, config, food)
            if res.get("status") == "rate_limited":
                wait = max(60.0, float(res.get("retry_after_s") or 60.0))
                log(f"[usda] rate-limited at {i}/{len(todo)} — sleeping {wait:.0f}s")
                time.sleep(wait)
                continue
            break
        if res.get("status") == "success":
            hits += 1
        else:
            misses += 1
        # Pace by lookup: a lookup is 1-4 requests, so `per_hour` is a
        # lookups-per-hour budget — keep it ≤ ~700 to stay under 1,000 req/h.
        spent = time.monotonic() - t0
        if spent < min_gap:
            time.sleep(min_gap - spent)
        if i % 50 == 0 or i == len(todo):
            el = time.monotonic() - started
            left = (len(todo) - i) * (el / i) if i else 0
            log(f"[usda] {i}/{len(todo)} · {hits} hits · {misses} misses · "
                f"{el / 60:.1f} min · ~{left / 3600:.1f} h left")
    return {"foods": len(foods), "looked_up": len(todo), "hits": hits, "misses": misses}


# ---------------------------------------------------------------------------
# Phase 3: per-recipe estimates
# ---------------------------------------------------------------------------

def phase_score(
    conn: sqlite3.Connection, config: Config, ids: list[int],
    *, write_frontmatter: bool = True, log=_print,
) -> dict:
    medium = low = 0
    started = time.monotonic()
    chunks = _chunks(ids, 50)
    for i, chunk in enumerate(chunks, 1):
        while True:
            try:
                out = estimate_nutrition(
                    conn, config, chunk,
                    write_frontmatter=write_frontmatter, raise_on_rate_limit=True,
                )
                break
            except nutrition_engine.RateLimited as e:
                wait = max(60.0, e.retry_after_s)
                log(f"[score] USDA rate-limited mid-chunk — sleeping {wait:.0f}s")
                time.sleep(wait)
        for rid in chunk:
            if out.get(rid, {}).get("confidence") == "medium":
                medium += 1
            else:
                low += 1
        if i % 20 == 0 or i == len(chunks):
            el = time.monotonic() - started
            done = min(len(ids), i * 50)
            left = (len(ids) - done) * (el / done) if done else 0
            log(f"[score] {done}/{len(ids)} · {medium} estimated · {low} low-confidence "
                f"(not stored) · {el / 60:.1f} min · ~{left / 60:.0f} min left")
    return {"recipes": len(ids), "medium": medium, "low": low}


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aboyeur.estimate_nutrition_batch")
    ap.add_argument("--phase", choices=("all", "parse", "usda", "score"), default="all")
    ap.add_argument("--courses", default=",".join(DEFAULT_COURSES),
                    help="comma-separated recipe courses to cover")
    ap.add_argument("--limit", type=int, help="stop after N recipes (by id)")
    ap.add_argument("--book", help="only recipes from this book")
    ap.add_argument("--workers", type=int, default=4, help="concurrent LLM parse batches")
    ap.add_argument("--batch-size", type=int, default=40, help="ingredient lines per LLM call")
    ap.add_argument("--usda-per-hour", type=int, default=700,
                    help="USDA lookups per hour (each is 1-4 requests; FDC allows 1,000 req/h)")
    ap.add_argument("--refresh-fiber", action="store_true",
                    help="re-fetch cached USDA foods that predate fiber tracking")
    ap.add_argument("--no-frontmatter", action="store_true",
                    help="don't write nutrition blocks into the recipe .md files (DB only)")
    args = ap.parse_args(argv)

    config = Config.load()
    conn = connect(config.db_path)
    # Same idempotent schema bring-up the backend does at start, so this can
    # run before the server has restarted onto a newer schema.
    from aboyeur.migrate import migrate
    with schema_lock(config.db_path):
        init_db(conn)
        migrate(conn)
    courses = tuple(c.strip() for c in args.courses.split(",") if c.strip())
    ids = target_recipe_ids(conn, courses, limit=args.limit, book=args.book)
    _print(f"{len(ids)} recipes ({', '.join(courses)}) without a nutrition row")
    if not ids:
        return 0

    t0 = time.monotonic()
    if args.phase in ("all", "parse"):
        _print(phase_parse(conn, config, ids, batch_size=args.batch_size, workers=args.workers))
    if args.phase in ("all", "usda"):
        _print(phase_usda(conn, config, ids, per_hour=args.usda_per_hour,
                         refresh_fiber=args.refresh_fiber))
    if args.phase in ("all", "score"):
        _print(phase_score(conn, config, ids, write_frontmatter=not args.no_frontmatter))
    _print(f"done in {(time.monotonic() - t0) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
