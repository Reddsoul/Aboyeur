"""Batch nutrition estimator (aboyeur/estimate_nutrition_batch.py) and the
engine changes it rides on: derived servings, 429 handling, servings_est."""

from __future__ import annotations

import dataclasses
import json
import sqlite3

import httpx
import pytest

from aboyeur import estimate_nutrition_batch as batch
from aboyeur.config import Config
from aboyeur.engines import nutrition as nutrition_engine
from aboyeur.tools.nutrition import estimate_nutrition


def _canned_usda(monkeypatch, *, rate_limit_first: int = 0, retry_after: str = "7"):
    """Every lookup resolves to the same macros. The first `rate_limit_first`
    calls answer 429 instead."""
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] <= rate_limit_first:
            return httpx.Response(429, headers={"Retry-After": retry_after},
                                  request=httpx.Request("GET", url))
        return httpx.Response(
            200,
            json={"foods": [{"description": f"{params['query']}, raw", "foodNutrients": [
                {"nutrientId": 1008, "value": 200.0}, {"nutrientId": 1003, "value": 10.0},
                {"nutrientId": 1005, "value": 20.0}, {"nutrientId": 1004, "value": 5.0},
                {"nutrientId": 1079, "value": 3.0},
            ]}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(nutrition_engine.httpx, "get", fake_get)
    return calls


def _llm_parses_everything(monkeypatch, grams: float = 100.0, fail_lines: set[str] = frozenset()):
    """Stand-in LLM: food = first word, fixed grams; a batch containing a
    `fail_lines` line fails whole (as the real strict parser does)."""
    calls = {"n": 0, "sizes": []}

    def fake(config, lines):
        calls["n"] += 1
        calls["sizes"].append(len(lines))
        if any(l in fail_lines for l in lines):
            return None
        return [{"food": (l.split() or ["x"])[0].lower(), "grams": grams} for l in lines]

    monkeypatch.setattr(nutrition_engine, "_llm_parse_lines", fake)
    return calls


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def test_derive_servings_from_total_grams_and_plate_size():
    assert nutrition_engine.derive_servings(1290, 430) == 3
    assert nutrition_engine.derive_servings(200, 430) == 1  # never below 1
    assert nutrition_engine.derive_servings(50_000, 430) == nutrition_engine.SERVINGS_EST_MAX
    assert nutrition_engine.derive_servings(1290, None) is None
    assert nutrition_engine.derive_servings(0, 430) is None


def test_estimate_uses_stated_servings_else_derived_else_default(
    db_conn: sqlite3.Connection, config: Config, monkeypatch,
):
    live = dataclasses.replace(config, usda_api_key="k")
    _canned_usda(monkeypatch)
    _llm_parses_everything(monkeypatch, grams=430.0)  # 3 lines -> 1290 g
    lines = ["one onion", "two carrots", "three eggs"]

    stated = nutrition_engine.estimate_recipe_macros(db_conn, live, lines, servings=6, serving_est_g=430)
    assert (stated["servings_used"], stated["servings_source"]) == (6, "recipe")

    derived = nutrition_engine.estimate_recipe_macros(db_conn, live, lines, servings=None, serving_est_g=430)
    assert (derived["servings_used"], derived["servings_source"]) == (3, "estimated")
    assert derived["total_grams"] == 1290.0
    # per-serving = total / 3: 1290 g at 200 kcal/100 g = 2580 kcal -> 860
    assert derived["calories"] == 860.0

    default = nutrition_engine.estimate_recipe_macros(db_conn, live, lines, servings=None, serving_est_g=None)
    assert (default["servings_used"], default["servings_source"]) == (4, "default")

    # No USDA key -> heuristic grams -> never "estimated" servings.
    heur = nutrition_engine.estimate_recipe_macros(db_conn, config, lines, servings=None, serving_est_g=430)
    assert heur["servings_source"] == "default"


def test_usda_429_is_reported_not_cached_and_can_raise(
    db_conn: sqlite3.Connection, config: Config, monkeypatch,
):
    live = dataclasses.replace(config, usda_api_key="k")
    calls = _canned_usda(monkeypatch, rate_limit_first=1, retry_after="9")
    res = nutrition_engine.lookup_ingredient_macros(db_conn, live, "onion")
    assert res["status"] == "rate_limited" and res["retry_after_s"] == 9.0
    assert calls["n"] == 1  # stopped at the first 429, no second query
    assert db_conn.execute("SELECT COUNT(*) AS n FROM usda_cache").fetchone()["n"] == 0

    # Next call succeeds (the fake only 429s once) and IS cached.
    assert nutrition_engine.lookup_ingredient_macros(db_conn, live, "onion")["status"] == "success"
    assert db_conn.execute("SELECT COUNT(*) AS n FROM usda_cache").fetchone()["n"] == 1

    # The agent path swallows a 429 as an unresolved ingredient; a batch
    # caller can ask for the exception instead.
    _llm_parses_everything(monkeypatch)
    _canned_usda(monkeypatch, rate_limit_first=1)
    quiet = nutrition_engine.estimate_recipe_macros(db_conn, live, ["garlic"], servings=1)
    assert quiet["usda_status"] in ("failed", "partial")
    _canned_usda(monkeypatch, rate_limit_first=1)
    with pytest.raises(nutrition_engine.RateLimited) as exc:
        nutrition_engine.estimate_recipe_macros(
            db_conn, live, ["pepper"], servings=1, raise_on_rate_limit=True,
        )
    assert exc.value.retry_after_s == 7.0


def test_servings_est_is_persisted_restored_and_used_for_portions(
    db_conn: sqlite3.Connection, config: Config, monkeypatch,
):
    live = dataclasses.replace(config, usda_api_key="k")
    _canned_usda(monkeypatch)
    _llm_parses_everything(monkeypatch, grams=430.0)
    rid = db_conn.execute("SELECT id FROM recipes WHERE servings IS NULL LIMIT 1").fetchone()["id"]
    db_conn.execute("UPDATE recipes SET course = 'main', serving_est_g = 430 WHERE id = ?", (rid,))
    db_conn.commit()
    n_lines = len(json.loads(db_conn.execute(
        "SELECT ingredients_json FROM recipes WHERE id = ?", (rid,)).fetchone()["ingredients_json"]))
    expected = nutrition_engine.derive_servings(n_lines * 430.0, 430)

    out = estimate_nutrition(db_conn, live, [rid], write_frontmatter=False)
    assert out[rid]["confidence"] == "medium"
    assert out[rid]["servings_est"] == expected
    row = db_conn.execute("SELECT servings_est FROM nutrition WHERE recipe_id = ?", (rid,)).fetchone()
    assert row["servings_est"] == expected

    # The menu path uses it where the recipe has no stated servings.
    from aboyeur.tools.menu import _load_candidates
    cand = _load_candidates(db_conn, live, [rid])[0]
    assert cand["servings"] == expected

    # ...and the frontmatter block carries it so a rebuild restores it.
    path = db_conn.execute("SELECT path FROM recipes WHERE id = ?", (rid,)).fetchone()["path"]
    macros = {"calories": 1, "protein_g": 1, "carb_g": 1, "fat_g": 1, "fiber_g": 0,
              "servings_used": expected, "servings_source": "estimated"}
    assert nutrition_engine.write_nutrition_frontmatter(path, macros, "medium", "2026-09-11T00:00:00Z")
    from aboyeur.indexer import parse_recipe_file
    from pathlib import Path
    parsed = parse_recipe_file(Path(path))
    assert parsed.nutrition["servings_est"] == expected


# ---------------------------------------------------------------------------
# Batch phases
# ---------------------------------------------------------------------------

def _mark_mains(conn: sqlite3.Connection, n: int) -> list[int]:
    ids = [r["id"] for r in conn.execute("SELECT id FROM recipes ORDER BY id LIMIT ?", (n,))]
    conn.executemany("UPDATE recipes SET course = 'main', serving_est_g = 430 WHERE id = ?",
                     [(i,) for i in ids])
    conn.commit()
    return ids


def test_target_recipe_ids_skips_scored_rows_and_other_courses(db_conn: sqlite3.Connection):
    ids = _mark_mains(db_conn, 4)
    db_conn.execute("INSERT INTO nutrition (recipe_id, kcal, confidence) VALUES (?, 100, 'medium')", (ids[0],))
    db_conn.commit()
    assert batch.target_recipe_ids(db_conn, ("main",)) == ids[1:]
    assert batch.target_recipe_ids(db_conn, ("main",), limit=2) == ids[1:3]
    assert batch.target_recipe_ids(db_conn, ("dessert",)) == []


def test_phase_parse_dedups_lines_bisects_failures_and_is_resumable(
    db_conn: sqlite3.Connection, config: Config, monkeypatch,
):
    ids = _mark_mains(db_conn, 6)
    lines = batch._target_lines(db_conn, ids)
    bad = next(iter(lines))
    calls = _llm_parses_everything(monkeypatch, fail_lines={bad})
    stats = batch.phase_parse(db_conn, config, ids, batch_size=8, workers=2, log=lambda *_: None)
    assert stats["lines"] == len(lines)
    assert stats["failed"] == 1 and stats["parsed"] == len(lines) - 1
    # Bisection: a failed 8-batch is retried as 4+4, 2+2, 1+1 — down to the culprit.
    assert min(calls["sizes"]) == 1
    cached = db_conn.execute("SELECT COUNT(*) AS n FROM ingredient_parse").fetchone()["n"]
    assert cached == len(lines) - 1

    # Second run: nothing left but the failed line (heuristic result is never cached).
    calls2 = _llm_parses_everything(monkeypatch, fail_lines={bad})
    again = batch.phase_parse(db_conn, config, ids, batch_size=8, workers=2, log=lambda *_: None)
    assert again["parsed"] == 0 and again["failed"] == 1
    assert calls2["n"] >= 1


def test_phase_usda_paces_dedups_and_waits_out_a_429(
    db_conn: sqlite3.Connection, config: Config, monkeypatch,
):
    ids = _mark_mains(db_conn, 5)
    _llm_parses_everything(monkeypatch)
    batch.phase_parse(db_conn, config, ids, batch_size=50, workers=1, log=lambda *_: None)
    foods = batch._target_foods(db_conn, ids)
    assert foods and len(foods) == len(set(foods))

    slept: list[float] = []
    monkeypatch.setattr(batch.time, "sleep", lambda s: slept.append(s))
    live = dataclasses.replace(config, usda_api_key="k")
    calls = _canned_usda(monkeypatch, rate_limit_first=1, retry_after="120")
    stats = batch.phase_usda(db_conn, live, ids, per_hour=3600, log=lambda *_: None)
    assert stats["looked_up"] == len(foods) and stats["hits"] == len(foods)
    assert 120.0 in slept  # paused for Retry-After, then resumed the same food
    zero = nutrition_engine._ZERO_MACRO_FOODS
    networked = [f for f in foods
                 if f not in zero and nutrition_engine.clean_ingredient_name(f) not in zero]
    assert calls["n"] == len(networked) + 1  # water/salt never hit the network
    assert db_conn.execute("SELECT COUNT(*) AS n FROM usda_cache").fetchone()["n"] == len(networked)

    # Resumable: everything cached now (zero-macro foods are answered
    # without a lookup, so they're re-"looked up" for free), no network.
    calls = _canned_usda(monkeypatch)
    again = batch.phase_usda(db_conn, live, ids, per_hour=3600, log=lambda *_: None)
    assert again["looked_up"] == len(foods) - len(networked) and calls["n"] == 0


def test_phase_usda_refresh_fiber_refetches_fiberless_entries(
    db_conn: sqlite3.Connection, config: Config, monkeypatch,
):
    ids = _mark_mains(db_conn, 3)
    _llm_parses_everything(monkeypatch)
    batch.phase_parse(db_conn, config, ids, batch_size=50, workers=1, log=lambda *_: None)
    foods = batch._target_foods(db_conn, ids)
    old = json.dumps({"kcal": 1, "protein_g": 0, "carb_g": 0, "fat_g": 0, "status": "success"})
    db_conn.executemany("INSERT INTO usda_cache (ingredient, macros_json, fetched_at) VALUES (?, ?, '')",
                        [(f, old) for f in foods])
    db_conn.commit()
    monkeypatch.setattr(batch.time, "sleep", lambda s: None)
    live = dataclasses.replace(config, usda_api_key="k")
    _canned_usda(monkeypatch)
    plain = batch.phase_usda(db_conn, live, ids, per_hour=3600, log=lambda *_: None)
    assert plain["looked_up"] == 0
    refreshed = batch.phase_usda(db_conn, live, ids, per_hour=3600, refresh_fiber=True, log=lambda *_: None)
    assert refreshed["looked_up"] == len(foods)
    row = db_conn.execute("SELECT macros_json FROM usda_cache WHERE ingredient = ?", (foods[0],)).fetchone()
    assert json.loads(row["macros_json"])["fiber_g"] == 3.0


def test_phase_score_persists_estimates_from_the_caches(
    db_conn: sqlite3.Connection, config: Config, monkeypatch,
):
    ids = _mark_mains(db_conn, 5)
    live = dataclasses.replace(config, usda_api_key="k")
    _llm_parses_everything(monkeypatch, grams=430.0)
    _canned_usda(monkeypatch)
    monkeypatch.setattr(batch.time, "sleep", lambda s: None)
    batch.phase_parse(db_conn, config, ids, batch_size=50, workers=1, log=lambda *_: None)
    batch.phase_usda(db_conn, live, ids, per_hour=3600, log=lambda *_: None)

    written: list[str] = []
    monkeypatch.setattr(nutrition_engine, "write_nutrition_frontmatter",
                        lambda path, *a: written.append(path) or True)
    stats = batch.phase_score(db_conn, live, ids, write_frontmatter=False, log=lambda *_: None)
    assert stats == {"recipes": 5, "medium": 5, "low": 0}
    assert written == []  # --no-frontmatter honored
    rows = db_conn.execute(
        "SELECT recipe_id, kcal, servings_est FROM nutrition WHERE recipe_id IN (?,?,?,?,?)", ids
    ).fetchall()
    assert len(rows) == 5 and all(r["kcal"] > 0 for r in rows)
    unstated = {r["id"] for r in db_conn.execute(
        "SELECT id FROM recipes WHERE servings IS NULL AND id IN (?,?,?,?,?)", ids)}
    assert unstated
    for r in rows:  # derived only where the recipe doesn't say
        assert (r["servings_est"] is not None) == (r["recipe_id"] in unstated)
    assert batch.target_recipe_ids(db_conn, ("main",)) == []  # nothing left to do


def test_main_cli_runs_all_phases(db_conn: sqlite3.Connection, config: Config, monkeypatch, capsys):
    ids = _mark_mains(db_conn, 3)
    monkeypatch.setattr(batch.Config, "load", staticmethod(lambda: dataclasses.replace(config, usda_api_key="k")))
    monkeypatch.setattr(batch, "connect", lambda path: db_conn)
    _llm_parses_everything(monkeypatch, grams=430.0)
    _canned_usda(monkeypatch)
    monkeypatch.setattr(batch.time, "sleep", lambda s: None)
    assert batch.main(["--no-frontmatter", "--courses", "main", "--usda-per-hour", "3600"]) == 0
    out = capsys.readouterr().out
    assert "3 recipes (main) without a nutrition row" in out
    assert "'medium': 3" in out
    assert batch.target_recipe_ids(db_conn, ("main",)) == []
