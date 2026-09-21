"""Phase 2 tests — the 9 LLM-facing tools, against the real fixture DB.

No network: config.usda_api_key is None, so nutrition estimates come back
confidence="low" rather than hitting USDA. That's the correct behavior to
test — Phase 2's acceptance bar is tool wiring + allergy safety, not live
USDA data.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from aboyeur.config import Config
from aboyeur.db import connect
from aboyeur.engines import nutrition as nutrition_engine
from aboyeur.engines import optimizer
from aboyeur.engines import shopping as shopping_engine
from aboyeur.tools import (
    build_macro_kit,
    estimate_nutrition,
    get_menu_recipes,
    get_profile,
    get_recipe,
    get_shopping_list,
    manage_pantry,
    plan_menu,
    search_recipes,
    swap_menu_item,
    update_profile,
)


def _all_ids(conn: sqlite3.Connection) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM recipes").fetchall()]


# ---------------------------------------------------------------------------
# search_recipes — including the allergy-safety acceptance check
# ---------------------------------------------------------------------------

def test_search_recipes_finds_real_matches(db_conn: sqlite3.Connection):
    results = search_recipes(db_conn, "chocolate")
    titles = {r["title"] for r in results}
    assert "Chocolate Cake" in titles


def test_search_recipes_for_menu_drops_classified_desserts(db_conn: sqlite3.Connection):
    row = db_conn.execute("SELECT id FROM recipes WHERE title = 'Chocolate Cake'").fetchone()
    db_conn.execute(
        "UPDATE recipes SET course = 'dessert', classified_at = 'now' WHERE id = ?", (row["id"],)
    )
    db_conn.commit()
    assert "Chocolate Cake" in {r["title"] for r in search_recipes(db_conn, "chocolate")}
    assert "Chocolate Cake" not in {
        r["title"] for r in search_recipes(db_conn, "chocolate", for_menu=True)
    }


def test_search_recipes_hard_excludes_allergen(db_conn: sqlite3.Connection):
    # Achar Gosht (660_Curries) contains beef, no peanut — sanity baseline.
    unfiltered = search_recipes(db_conn, "beef")
    assert any(r["title"] == "Achar Gosht" for r in unfiltered)

    # Cream Cake (The_Myrtle_Reed_Cook_Book) — search broadly, then prove that
    # allergy-filtering a real ingredient it contains removes it, provably.
    baseline = search_recipes(db_conn, "cream cake")
    assert any(r["title"] == "Cream Cake" for r in baseline)

    filtered = search_recipes(db_conn, "cream cake", allergies=["eggs"])
    assert not any(r["title"] == "Cream Cake" for r in filtered)


def test_search_recipes_respects_limit_cap(db_conn: sqlite3.Connection):
    results = search_recipes(db_conn, "chicken OR beef OR cake OR ginger", limit=999)
    assert len(results) <= 25


def test_search_recipes_pantry_match_flag(db_conn: sqlite3.Connection):
    results = search_recipes(db_conn, "chocolate", pantry=["chocolate"])
    assert any(r["pantry_match"] for r in results)


def test_search_recipes_survives_fts5_special_characters(db_conn: sqlite3.Connection):
    # Raw punctuation used to reach FTS5's MATCH parser unescaped and raise
    # sqlite3.OperationalError ("fts5: syntax error near ...") — which, inside
    # a streaming chat response, aborted the connection outright instead of
    # returning a clean error. Regression coverage for that incident.
    for query in [
        "Indian / Mexican", "gluten-free", 'chicken "curry"',
        "beef (spicy)", "-tofu", "a:b*c", "chicken NOT beef",
    ]:
        results = search_recipes(db_conn, query)
        assert isinstance(results, list)


def test_search_recipes_all_punctuation_query_returns_empty(db_conn: sqlite3.Connection):
    # No alnum tokens survive sanitizing — must short-circuit to [] rather
    # than hand FTS5 an empty/invalid MATCH string.
    assert search_recipes(db_conn, "---///:::") == []


# ---------------------------------------------------------------------------
# get_recipe
# ---------------------------------------------------------------------------

def test_get_recipe_returns_verbatim_markdown(db_conn: sqlite3.Connection):
    row = db_conn.execute("SELECT id, path FROM recipes WHERE title = 'Achar Gosht'").fetchone()
    result = get_recipe(db_conn, row["id"])
    from pathlib import Path
    assert result["markdown"] == Path(row["path"]).read_text(encoding="utf-8")


def test_get_recipe_unknown_id_returns_error(db_conn: sqlite3.Connection):
    result = get_recipe(db_conn, 999999)
    assert "error" in result


def test_get_recipe_scale_multiplier_only(db_conn: sqlite3.Connection):
    row = db_conn.execute("SELECT id FROM recipes WHERE title = 'Achari Murghi'").fetchone()
    result = get_recipe(db_conn, row["id"], scale_to_servings=12)
    assert result["scale_multiplier"] == 2.0
    assert "not auto-rewritten" in result["scale_note"]


# ---------------------------------------------------------------------------
# get_profile / update_profile
# ---------------------------------------------------------------------------

def test_get_profile_default_is_empty_but_shaped(db_conn: sqlite3.Connection, client_id: int):
    profile = get_profile(db_conn, client_id)
    assert profile["allergies"] == []
    assert profile["targets"] == {}


def test_update_profile_allergy_roundtrip(db_conn: sqlite3.Connection, client_id: int):
    update_profile(db_conn, client_id, {"add_allergies": ["Peanut", "shellfish"]})
    profile = get_profile(db_conn, client_id)
    assert profile["allergies"] == ["peanut", "shellfish"]

    update_profile(db_conn, client_id, {"remove_allergies": ["peanut"]})
    profile = get_profile(db_conn, client_id)
    assert profile["allergies"] == ["shellfish"]


def test_update_profile_add_member_computes_targets(db_conn: sqlite3.Connection, client_id: int):
    update_profile(db_conn, client_id, {"add_member": {
        "id": "adult_1", "label": "Jose", "sex": "m", "age": 30,
        "height_cm": 178, "weight_kg": 80, "activity": "moderate",
    }})
    profile = get_profile(db_conn, client_id)
    assert "adult_1" in profile["targets"]
    assert profile["targets"]["adult_1"]["source"] == "bmr"


def test_get_profile_passes_bmr_kcal_through(db_conn: sqlite3.Connection, client_id: int):
    update_profile(db_conn, client_id, {"add_member": [
        {"id": "adult_1", "sex": "m", "age": 30, "height_cm": 178, "weight_kg": 80},
        {"id": "kid_1", "daily_kcal": 1600},
    ]})
    profile = get_profile(db_conn, client_id)
    engine_targets = nutrition_engine.household_targets(profile)
    for mid in ("adult_1", "kid_1"):
        assert profile["targets"][mid]["bmr_kcal"] == engine_targets[mid]["bmr_kcal"]
    assert profile["targets"]["adult_1"]["bmr_kcal"] == round(nutrition_engine.bmr_mifflin("m", 30, 178, 80))
    assert profile["targets"]["kid_1"]["bmr_kcal"] is None


def test_update_profile_unrelated_field_never_touches_allergies(db_conn: sqlite3.Connection, client_id: int):
    update_profile(db_conn, client_id, {"add_allergies": ["peanut"]})
    update_profile(db_conn, client_id, {"set_meals_per_day": 4})
    profile = get_profile(db_conn, client_id)
    assert profile["allergies"] == ["peanut"]
    assert profile["meals_per_day"] == 4


def test_update_profile_add_member_merges_not_replaces(db_conn: sqlite3.Connection, client_id: int):
    update_profile(db_conn, client_id, {"add_member": {
        "id": "adult_1", "label": "Jose", "sex": "m", "age": 30,
        "height_cm": 178, "weight_kg": 80, "activity": "moderate",
    }})
    # Later patch only sets meals_per_day for this one person — anthropometrics
    # must survive, otherwise the BMR target silently degrades to the fallback.
    update_profile(db_conn, client_id, {"add_member": {"id": "adult_1", "meals_per_day": 1}})
    profile = get_profile(db_conn, client_id)
    member = next(m for m in profile["household"] if m["id"] == "adult_1")
    assert member["meals_per_day"] == 1
    assert member["height_cm"] == 178
    assert member["weight_kg"] == 80
    assert profile["targets"]["adult_1"]["source"] == "bmr"


def test_update_profile_add_member_accepts_list(db_conn: sqlite3.Connection, client_id: int):
    # The local model sometimes sends add_member as a list despite the schema
    # saying object — this used to raise TypeError and kill the stream.
    update_profile(db_conn, client_id, {"add_member": [
        {"id": "adult_1", "label": "Jose"},
        {"id": "kid_1", "label": "Sam", "meals_per_day": 1},
    ]})
    profile = get_profile(db_conn, client_id)
    assert {m["id"] for m in profile["household"]} == {"adult_1", "kid_1"}


def test_update_profile_add_member_rejects_non_dict(db_conn: sqlite3.Connection, client_id: int):
    with pytest.raises(ValueError, match="add_member"):
        update_profile(db_conn, client_id, {"add_member": "adult_1"})
    with pytest.raises(ValueError, match="add_member"):
        update_profile(db_conn, client_id, {"add_member": {"label": "no id"}})


# ---------------------------------------------------------------------------
# manage_pantry
# ---------------------------------------------------------------------------

def test_manage_pantry_add_list_remove_clear(db_conn: sqlite3.Connection, client_id: int):
    manage_pantry(db_conn, client_id, "add", ["ginger", "soy sauce"])
    state = manage_pantry(db_conn, client_id, "list")
    assert {i["item"] for i in state} == {"ginger", "soy sauce"}

    manage_pantry(db_conn, client_id, "remove", ["ginger"])
    state = manage_pantry(db_conn, client_id, "list")
    assert {i["item"] for i in state} == {"soy sauce"}

    manage_pantry(db_conn, client_id, "clear")
    assert manage_pantry(db_conn, client_id, "list") == []


def test_manage_pantry_unknown_action_raises(db_conn: sqlite3.Connection, client_id: int):
    import pytest
    with pytest.raises(ValueError):
        manage_pantry(db_conn, client_id, "bogus")


# ---------------------------------------------------------------------------
# estimate_nutrition
# ---------------------------------------------------------------------------

def test_estimate_nutrition_flags_low_confidence_without_usda_key(
    db_conn: sqlite3.Connection, config: Config
):
    ids = _all_ids(db_conn)[:2]
    result = estimate_nutrition(db_conn, config, ids)
    for rid in ids:
        assert result[rid]["confidence"] == "low"


def test_estimate_nutrition_does_not_cache_low_confidence(db_conn: sqlite3.Connection, config: Config):
    """A "low" confidence result is a statement about missing USDA data, not
    the recipe — persisting it would freeze the recipe at 0 kcal forever,
    even after ingredient lookups start succeeding (e.g. once a key is added).
    """
    rid = _all_ids(db_conn)[0]
    estimate_nutrition(db_conn, config, [rid])
    row = db_conn.execute("SELECT confidence FROM nutrition WHERE recipe_id = ?", (rid,)).fetchone()
    assert row is None


def _fake_usda_response(monkeypatch):
    """Every ingredient 'looked up' resolves to the same canned macros, and
    counts each real httpx.get call so tests can assert the cache, not the
    network, served repeat requests.
    """
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"foods": [{"foodNutrients": [
                {"nutrientId": 1008, "value": 200.0},
                {"nutrientId": 1003, "value": 10.0},
                {"nutrientId": 1005, "value": 20.0},
                {"nutrientId": 1004, "value": 5.0},
            ]}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(nutrition_engine.httpx, "get", fake_get)
    return calls


def test_estimate_nutrition_reuses_cached_recipe_without_recomputing(
    db_conn: sqlite3.Connection, config: Config, monkeypatch
):
    """Once a recipe's nutrition is resolved (persisted to the `nutrition`
    table), a second estimate_nutrition call for the same id must come
    straight from the DB — no ingredient lookups, no USDA calls at all.
    """
    calls = _fake_usda_response(monkeypatch)
    live_config = dataclasses.replace(config, usda_api_key="fake-key-for-test")
    rid = _all_ids(db_conn)[0]

    first = estimate_nutrition(db_conn, live_config, [rid])
    assert first[rid]["confidence"] == "medium"
    calls_after_first = calls["n"]
    assert calls_after_first > 0

    second = estimate_nutrition(db_conn, live_config, [rid])
    assert second == first
    assert calls["n"] == calls_after_first  # zero additional USDA calls


def test_lookup_ingredient_macros_reuses_cached_ingredient_without_recomputing(
    db_conn: sqlite3.Connection, config: Config, monkeypatch
):
    """Two recipes sharing an ingredient (e.g. "salt") should hit USDA for
    that ingredient once, not once per recipe — the usda_cache table is
    keyed by ingredient name, not recipe id.
    """
    calls = _fake_usda_response(monkeypatch)
    live_config = dataclasses.replace(config, usda_api_key="fake-key-for-test")

    first = nutrition_engine.lookup_ingredient_macros(db_conn, live_config, "salt")
    assert first["status"] == "success"
    calls_after_first = calls["n"]

    second = nutrition_engine.lookup_ingredient_macros(db_conn, live_config, "salt")
    assert second == first
    assert calls["n"] == calls_after_first  # cache hit, no second HTTP call


def _fake_lookup(macros_by_name):
    def fake(conn, cfg, name):
        hit = macros_by_name.get(name)
        if hit:
            return {**hit, "status": "success"}
        return {"kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "status": "failed"}
    return fake


def test_estimate_recipe_macros_extrapolates_unmatched_ingredients(
    db_conn: sqlite3.Connection, config: Config, monkeypatch
):
    """Half the ingredients resolving used to mean half the real calories —
    a systematic undercount that broke daily-target math downstream."""
    monkeypatch.setattr(nutrition_engine, "lookup_ingredient_macros", _fake_lookup(
        {"chicken": {"kcal": 200.0, "protein_g": 20.0, "carb_g": 0.0, "fat_g": 10.0}}
    ))
    m = nutrition_engine.estimate_recipe_macros(db_conn, config, ["chicken", "mystery goo"], servings=1)
    assert m["usda_status"] == "success"
    assert m["matched_ingredients"] == 1
    assert m["total_ingredients"] == 2
    # 200 kcal * 0.8 grams-factor, doubled to cover the unmatched ingredient
    assert m["calories"] == 320.0


def test_estimate_recipe_macros_partial_when_under_half_matched(
    db_conn: sqlite3.Connection, config: Config, monkeypatch
):
    monkeypatch.setattr(nutrition_engine, "lookup_ingredient_macros", _fake_lookup(
        {"chicken": {"kcal": 200.0, "protein_g": 20.0, "carb_g": 0.0, "fat_g": 10.0}}
    ))
    m = nutrition_engine.estimate_recipe_macros(
        db_conn, config, ["chicken", "mystery goo", "stranger goo"], servings=1)
    assert m["usda_status"] == "partial"  # 1/3 matched — too thin to trust


def test_parse_ingredients_prefers_cache_over_llm(db_conn: sqlite3.Connection, config: Config, monkeypatch):
    db_conn.execute(
        "INSERT INTO ingredient_parse (raw, food, grams, parsed_at) VALUES (?, ?, ?, ?)",
        ("6 eggs", "eggs", 300.0, "2026-01-01T00:00:00Z"),
    )
    monkeypatch.setattr(nutrition_engine, "_llm_parse_lines",
                        lambda *a: (_ for _ in ()).throw(AssertionError("LLM called on cache hit")))
    out = nutrition_engine.parse_ingredients(db_conn, config, ["6 eggs"])
    assert out == [{"food": "eggs", "grams": 300.0, "source": "cache"}]


def test_parse_ingredients_caches_llm_results(db_conn: sqlite3.Connection, config: Config, monkeypatch):
    monkeypatch.setattr(nutrition_engine, "_llm_parse_lines",
                        lambda cfg, lines: [{"food": "eggs", "grams": 600.0}])
    first = nutrition_engine.parse_ingredients(db_conn, config, ["12 eggs"])
    assert first[0]["source"] == "llm"
    # Second call must come from the table, not the LLM.
    monkeypatch.setattr(nutrition_engine, "_llm_parse_lines",
                        lambda *a: (_ for _ in ()).throw(AssertionError("LLM re-called")))
    second = nutrition_engine.parse_ingredients(db_conn, config, ["12 eggs"])
    assert second == [{"food": "eggs", "grams": 600.0, "source": "cache"}]


def test_parse_ingredients_heuristic_fallback_is_not_cached(
    db_conn: sqlite3.Connection, config: Config, monkeypatch
):
    monkeypatch.setattr(nutrition_engine, "_llm_parse_lines", lambda cfg, lines: None)
    out = nutrition_engine.parse_ingredients(db_conn, config, ["one-half cupful of syrup"])
    assert out[0] == {"food": "syrup", "grams": 80.0, "source": "heuristic"}
    assert db_conn.execute("SELECT COUNT(*) c FROM ingredient_parse").fetchone()["c"] == 0


def test_estimate_nutrition_persists_block_to_recipe_md(
    db_conn: sqlite3.Connection, config: Config, monkeypatch
):
    _fake_usda_response(monkeypatch)
    monkeypatch.setattr(nutrition_engine, "_llm_parse_lines", lambda cfg, lines: None)
    live_config = dataclasses.replace(config, usda_api_key="fake-key-for-test")
    rid = _all_ids(db_conn)[0]
    estimate_nutrition(db_conn, live_config, [rid])

    path = db_conn.execute("SELECT path FROM recipes WHERE id = ?", (rid,)).fetchone()["path"]
    text = Path(path).read_text()
    assert "nutrition:" in text
    assert "confidence: medium" in text


def test_indexer_restores_nutrition_from_frontmatter(
    db_conn: sqlite3.Connection, config: Config, monkeypatch
):
    """A DB rebuild must recover persisted estimates without any network."""
    calls = _fake_usda_response(monkeypatch)
    monkeypatch.setattr(nutrition_engine, "_llm_parse_lines", lambda cfg, lines: None)
    live_config = dataclasses.replace(config, usda_api_key="fake-key-for-test")
    rid = _all_ids(db_conn)[0]
    first = estimate_nutrition(db_conn, live_config, [rid])
    path = db_conn.execute("SELECT path FROM recipes WHERE id = ?", (rid,)).fetchone()["path"]

    from aboyeur.indexer import index_corpus
    index_corpus(config, rebuild=True)  # wipes nothing but re-upserts all rows
    fresh = connect(config.db_path)
    try:
        new_rid = fresh.execute("SELECT id FROM recipes WHERE path = ?", (path,)).fetchone()["id"]
        calls_before = calls["n"]
        restored = estimate_nutrition(fresh, live_config, [new_rid])
        assert restored[new_rid]["kcal"] == first[rid]["kcal"]
        assert calls["n"] == calls_before  # served from the restored table
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# plan_menu / swap_menu_item
# ---------------------------------------------------------------------------

def test_plan_menu_returns_requested_days(db_conn: sqlite3.Connection, config: Config, client_id: int):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=3)
    assert len(plan["days"]) == 3
    assert "overlap_score" in plan
    assert "macro_fit_avg" in plan
    assert "menu_id" in plan


def _dessert_ids(conn: sqlite3.Connection) -> list[int]:
    ids = [
        r["id"] for r in conn.execute(
            "SELECT id, title FROM recipes WHERE lower(title) LIKE '%cake%' "
            "OR lower(title) LIKE '%glaze%' OR lower(title) LIKE '%tea-cake%'"
        ).fetchall()
    ]
    assert ids, "fixture should contain some dessert recipes"
    return ids


def test_plan_menu_repairs_a_useless_pool_from_the_corpus(
    db_conn: sqlite3.Connection, config: Config, client_id: int,
):
    """A candidate pool that is only desserts used to be a refusal. Now
    plan_menu widens the pool itself from the library and still delivers
    every requested day — with real dishes, never the desserts."""
    from aboyeur.tools import menu as menu_mod
    result = plan_menu(db_conn, config, client_id, candidate_ids=_dessert_ids(db_conn), days=3)
    assert "error" not in result, result
    assert sorted({d["day"] for d in result["days"]}) == [1, 2, 3]
    assert not any(optimizer.is_dessert(d["title"]) for d in result["days"])
    assert result["repair_notes"] and result["repair_notes"][0].startswith("round 1")
    assert "repair" not in result["summary_markdown"].lower() or "Not fully on target" in result["summary_markdown"]
    assert menu_mod.REPAIR_ROUNDS[0] >= 3 * 3  # widening asks for at least a few x the slots


def test_plan_menu_refuses_only_when_the_library_has_nothing_left(
    db_conn: sqlite3.Connection, config: Config, client_id: int, monkeypatch,
):
    # With nothing to widen from, an all-dessert pool is still a refusal —
    # cannot_build, nothing persisted, Macro Kit suggested.
    from aboyeur.tools import menu as menu_mod
    monkeypatch.setattr(menu_mod, "_widen_pool", lambda *a, **k: [])
    before = db_conn.execute("SELECT COUNT(*) AS n FROM menus").fetchone()["n"]
    result = plan_menu(db_conn, config, client_id, candidate_ids=_dessert_ids(db_conn), days=3)
    assert result["error"] == "cannot_build"
    assert result["reasons"]
    assert result["suggest"] == "macro_kit"
    after = db_conn.execute("SELECT COUNT(*) AS n FROM menus").fetchone()["n"]
    assert after == before  # nothing persisted


def test_plan_menu_reports_macro_targets_and_ingredient_summary(
    db_conn: sqlite3.Connection, config: Config, client_id: int,
):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=3)
    # Macro targets, not just calories — now including fiber.
    assert set(plan["daily_target_macros"]) == {"protein_g", "carb_g", "fat_g", "fiber_g"}
    for day in plan["daily_totals"]:
        assert {"protein_g", "carb_g", "fat_g", "fiber_g", "macro_status"} <= set(day)
    # Plan-wide ingredient stats: total distinct + which overlap across recipes.
    summary = plan["ingredient_summary"]
    assert summary["total_unique"] > 0
    assert summary["shared_count"] == len(summary["shared"])
    for entry in summary["shared"]:
        assert entry["recipes"] >= 2


def _seed_macros(monkeypatch, per_id: dict[int, dict]):
    """Stub the nutrition estimate plan_menu/swap use, so day-solver behavior
    can be tested against known per-serving macros without USDA/LLM."""
    import aboyeur.tools.menu as menu_mod

    def fake(conn, config, recipe_ids):
        return {
            rid: {**per_id.get(rid, {"kcal": 0, "protein_g": 0, "carb_g": 0,
                                     "fat_g": 0, "fiber_g": 0}),
                  "confidence": "medium" if rid in per_id else "low"}
            for rid in recipe_ids
        }
    monkeypatch.setattr(menu_mod, "estimate_nutrition", fake)


def test_plan_menu_day_solver_hits_all_macros_and_varies_portions(
    db_conn: sqlite3.Connection, config: Config, client_id: int, user_id: int, monkeypatch,
):
    from aboyeur import clients
    # A client with real anthropometrics -> a concrete kcal + macro target.
    cid = clients.create_client(db_conn, user_id, "Solver Client")["id"]
    update_profile(db_conn, cid, {"add_member": {
        "id": "a", "label": "A", "sex": "m", "age": 35,
        "height_cm": 180, "weight_kg": 82, "activity": "moderate", "meals_per_day": 2,
    }})
    eligible = [
        r["id"] for r in db_conn.execute(
            "SELECT id, title FROM recipes WHERE lower(title) NOT LIKE '%cake%' "
            "AND lower(title) NOT LIKE '%glaze%' AND lower(title) NOT LIKE '%tea%'"
        ).fetchall()
    ]
    # Two dish shapes — protein-leaning and carb-leaning — both substantial
    # enough that a per-day pair can be scaled onto every macro without
    # hitting the portion clamp, so the solve has real freedom to move them.
    per_id = {}
    for n, rid in enumerate(eligible):
        if n % 2 == 0:
            per_id[rid] = {"kcal": 820, "protein_g": 68, "carb_g": 56, "fat_g": 27, "fiber_g": 9}
        else:
            per_id[rid] = {"kcal": 780, "protein_g": 47, "carb_g": 90, "fat_g": 22, "fiber_g": 13}
    _seed_macros(monkeypatch, per_id)

    plan = plan_menu(db_conn, config, cid, candidate_ids=eligible, days=2, meals_per_day=2)
    assert "menu_id" in plan, plan
    for dt in plan["daily_totals"]:
        assert dt["macro_status"] == "ok", dt
    mults = {d["portion_multiplier"] for d in plan["days"]}
    assert len(mults) > 1  # per-dish multipliers, not one uniform day factor
    assert all(
        optimizer.PORTION_MULT_MIN <= d["portion_multiplier"] <= optimizer.PORTION_MULT_MAX
        for d in plan["days"]
    )


def test_plan_menu_delivers_an_impossible_omad_week_flagged(
    db_conn: sqlite3.Connection, config: Config, client_id: int, user_id: int, monkeypatch,
):
    from aboyeur import clients
    cid = clients.create_client(db_conn, user_id, "OMAD Client")["id"]
    update_profile(db_conn, cid, {"add_member": {
        "id": "a", "label": "A", "sex": "m", "age": 35,
        "height_cm": 183, "weight_kg": 95, "activity": "active", "meals_per_day": 1,
    }})
    eligible = [
        r["id"] for r in db_conn.execute(
            "SELECT id, title FROM recipes WHERE lower(title) NOT LIKE '%cake%' "
            "AND lower(title) NOT LIKE '%glaze%' AND lower(title) NOT LIKE '%tea%'"
        ).fetchall()
    ]
    # Every dish a ~120 kcal side — no way to reach a 2500+ kcal OMAD day.
    _seed_macros(monkeypatch, {
        rid: {"kcal": 120, "protein_g": 4, "carb_g": 20, "fat_g": 2, "fiber_g": 3}
        for rid in eligible
    })
    result = plan_menu(db_conn, config, cid, candidate_ids=eligible, days=3, meals_per_day=1)
    # Every dish in the library is a 120 kcal side, so no repair round can
    # fix it — but the week is still delivered, every day flagged honestly
    # rather than hidden behind a refusal.
    assert "error" not in result, result
    assert sorted({d["day"] for d in result["days"]}) == [1, 2, 3]
    assert all(t["macro_status"] == "off" for t in result["daily_totals"])
    assert all(t["gap_notes"] for t in result["daily_totals"])
    assert result["repair_notes"][-1].startswith("day(s) 1, 2, 3 are the closest")
    assert "**Not fully on target:** days 1, 2, 3" in result["summary_markdown"]


def test_plan_menu_persists_to_menus_table(db_conn: sqlite3.Connection, config: Config, client_id: int):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=3)
    row = db_conn.execute("SELECT status FROM menus WHERE id = ?", (plan["menu_id"],)).fetchone()
    assert row is not None
    assert row["status"] == "draft"


def _full_variety(conn, client_id: int) -> None:
    """These tests assert the no-repeat rule, which is variety 100 now — the
    default 50 lets the optimizer repeat a dish to reach its overlap floor."""
    from aboyeur.tools.profile import update_profile
    update_profile(conn, client_id, {"set_variety": 100})


def test_plan_menu_never_repeats_a_recipe(db_conn: sqlite3.Connection, config: Config, client_id: int):
    _full_variety(db_conn, client_id)
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=5)
    recipe_ids = [d["recipe_id"] for d in plan["days"]]
    assert len(recipe_ids) == len(set(recipe_ids))


def test_swap_menu_item_replaces_the_right_day(db_conn: sqlite3.Connection, config: Config, client_id: int):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=3)
    used = {d["recipe_id"] for d in plan["days"]}
    replacement = next(i for i in ids if i not in used)

    swapped = swap_menu_item(db_conn, config, client_id, day=1, replacement_id=replacement, menu_id=plan["menu_id"])
    assert swapped["days"][0]["recipe_id"] == replacement
    assert swapped["days"][0]["pick_reason"] == "manual swap"


def test_swap_menu_item_defaults_to_most_recent_menu(db_conn: sqlite3.Connection, config: Config, client_id: int):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=3)
    used = {d["recipe_id"] for d in plan["days"]}
    replacement = next(i for i in ids if i not in used)

    swapped = swap_menu_item(db_conn, config, client_id, day=1, replacement_id=replacement)  # no menu_id
    assert swapped["menu_id"] == plan["menu_id"]
    assert swapped["days"][0]["recipe_id"] == replacement


def test_swap_menu_item_persists_the_update(db_conn: sqlite3.Connection, config: Config, client_id: int):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=3)
    used = {d["recipe_id"] for d in plan["days"]}
    replacement = next(i for i in ids if i not in used)
    swap_menu_item(db_conn, config, client_id, day=1, replacement_id=replacement, menu_id=plan["menu_id"])

    row = db_conn.execute("SELECT days_json FROM menus WHERE id = ?", (plan["menu_id"],)).fetchone()
    stored = json.loads(row["days_json"])
    assert stored["days"][0]["recipe_id"] == replacement


def test_plan_menu_meals_per_day_grouped_into_day_and_meal_in_one_call(
    db_conn: sqlite3.Connection, config: Config, client_id: int,
):
    # The actual fix for a real incident: plan_menu used to be called once
    # PER MEAL by the LLM (N sequential LLM round trips) — a 6-day x 3-meal
    # request burned all 60 tool-call rounds over ~10 minutes with zero
    # output. It must now produce every meal for every day from ONE call.
    _full_variety(db_conn, client_id)
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=2, meals_per_day=3)
    assert len(plan["days"]) == 6
    assert plan["meals_per_day"] == 3
    days_seen = sorted({d["day"] for d in plan["days"]})
    assert days_seen == [1, 2]
    for day in days_seen:
        meals = sorted(d["meal"] for d in plan["days"] if d["day"] == day)
        assert meals == [1, 2, 3]
    recipe_ids = [d["recipe_id"] for d in plan["days"]]
    assert len(recipe_ids) == len(set(recipe_ids))  # no repeats across the whole plan


def test_swap_menu_item_targets_specific_meal_when_meals_per_day_over_1(
    db_conn: sqlite3.Connection, config: Config, client_id: int,
):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=2, meals_per_day=2)
    used = {d["recipe_id"] for d in plan["days"]}
    replacement = next(i for i in ids if i not in used)

    swapped = swap_menu_item(
        db_conn, config, client_id, day=1, replacement_id=replacement, meal=2, menu_id=plan["menu_id"]
    )
    target = next(d for d in swapped["days"] if d["day"] == 1 and d["meal"] == 2)
    assert target["recipe_id"] == replacement
    assert target["pick_reason"] == "manual swap"
    # day 1 meal 1 must be untouched
    other = next(d for d in swapped["days"] if d["day"] == 1 and d["meal"] == 1)
    assert other["recipe_id"] != replacement


def test_swap_menu_item_unknown_day_errors(db_conn: sqlite3.Connection, config: Config, client_id: int):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=2)
    result = swap_menu_item(db_conn, config, client_id, day=99, replacement_id=ids[0], menu_id=plan["menu_id"])
    assert "error" in result


def test_swap_menu_item_no_menu_exists_errors(db_conn: sqlite3.Connection, config: Config, client_id: int):
    result = swap_menu_item(db_conn, config, client_id, day=1, replacement_id=1)
    assert "error" in result


# ---------------------------------------------------------------------------
# get_shopping_list
# ---------------------------------------------------------------------------

def test_get_shopping_list_from_menu_id(db_conn: sqlite3.Connection, config: Config, client_id: int):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=3)
    shopping_list = get_shopping_list(db_conn, config, client_id, plan["menu_id"])
    assert shopping_list["total_items"] > 0
    assert shopping_list["to_buy"] <= shopping_list["total_items"]


def test_get_shopping_list_defaults_to_most_recent_menu(db_conn: sqlite3.Connection, config: Config, client_id: int):
    ids = _all_ids(db_conn)
    plan_menu(db_conn, config, client_id, candidate_ids=ids, days=3)
    shopping_list = get_shopping_list(db_conn, config, client_id)  # no menu_id
    assert shopping_list["total_items"] > 0


def test_get_shopping_list_pantry_have_flag(db_conn: sqlite3.Connection, config: Config, client_id: int):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=2)
    all_ings = {
        ing for d in plan["days"] for ing in json.loads(
            db_conn.execute("SELECT ingredients_json FROM recipes WHERE id = ?",
                             (d["recipe_id"],)).fetchone()["ingredients_json"]
        )
    }
    manage_pantry(db_conn, client_id, "add", list(all_ings)[:1])
    shopping_list = get_shopping_list(db_conn, config, client_id, plan["menu_id"])
    haves = [it["have"] for s in shopping_list["sections"].values() for it in s]
    assert any(haves)


def test_get_shopping_list_merges_canonical_variants(db_conn: sqlite3.Connection, config: Config, client_id: int):
    """Cached parses collapse raw variants ('boiled potato', 'potatoes') into
    one canonical entry with summed grams — the potato-six-ways bug."""
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=2)
    per_recipe_lines = [
        [ing.lower().strip() for ing in json.loads(
            db_conn.execute("SELECT ingredients_json FROM recipes WHERE id = ?",
                            (d["recipe_id"],)).fetchone()["ingredients_json"]
        ) if ing.strip()]
        for d in plan["days"]
    ]
    raw_lines = sorted({line for lines in per_recipe_lines for line in lines})
    # Seed the parse cache mapping every raw line to one canonical food, as
    # the LLM parser would after nutrition estimation warmed the cache.
    for raw in raw_lines:
        db_conn.execute(
            "INSERT OR REPLACE INTO ingredient_parse (raw, food, grams, parsed_at) "
            "VALUES (?, 'potato', 100.0, '2026-01-01T00:00:00Z')", (raw,))
    db_conn.commit()

    shopping_list = get_shopping_list(db_conn, config, client_id, plan["menu_id"])
    items = [it for s in shopping_list["sections"].values() for it in s]
    assert len(items) == 1
    item = items[0]
    assert item["name"] == "potato"
    # Grams sum once per line occurrence (a shared line in two recipes means
    # buying it for both), recipes count once each however many lines they had.
    total_occurrences = sum(len(lines) for lines in per_recipe_lines)
    assert item["approx_grams"] == 100 * total_occurrences
    assert item["used_in_recipes"] == sum(1 for lines in per_recipe_lines if lines)


# ---------------------------------------------------------------------------
# get_menu_recipes — deterministic STEP-2 delivery
# ---------------------------------------------------------------------------

def test_get_menu_recipes_orders_and_labels_days(db_conn: sqlite3.Connection, config: Config, client_id: int):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=3)
    result = get_menu_recipes(db_conn, client_id, plan["menu_id"])
    assert result["menu_id"] == plan["menu_id"]
    assert result["slot_count"] == len(plan["days"])
    md = result["markdown"]
    positions = [md.index(f"# Day {d}") for d in (1, 2, 3)]
    assert positions == sorted(positions)  # day headings appear in order
    # Each slot's recipe markdown is present verbatim under its heading.
    for d in plan["days"]:
        assert d["title"] in md


def test_get_menu_recipes_defaults_to_most_recent_menu(db_conn: sqlite3.Connection, config: Config, client_id: int):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=2)
    result = get_menu_recipes(db_conn, client_id)  # no menu_id
    assert result["menu_id"] == plan["menu_id"]


def test_get_menu_recipes_no_menu_errors(db_conn: sqlite3.Connection, client_id: int):
    assert "error" in get_menu_recipes(db_conn, client_id, 9999)


# ---------------------------------------------------------------------------
# Multi-tenancy — every per-household table (profile, pantry, menus) must be
# scoped by client_id. These exist because _load_menu's default-to-most-recent
# path (used by swap_menu_item/get_menu_recipes/get_shopping_list whenever
# menu_id is omitted) had no owner filter until this pass — harmless with one
# household, a real cross-tenant leak the moment a second account exists.
# ---------------------------------------------------------------------------

def _other_client(db_conn: sqlite3.Connection) -> int:
    """A second client of the SAME practitioner. Deliberately not a second
    account: two clients on one practitioner's book is the case where a leak
    would actually happen, and the one PRACTITIONER_PLAN.md promises against."""
    from aboyeur import auth, clients
    user_id = auth.get_or_create_user(db_conn, "tester@example.com")
    return clients.create_client(db_conn, user_id, "Other Client")["id"]


def test_profile_is_isolated_per_client(db_conn: sqlite3.Connection, client_id: int):
    other = _other_client(db_conn)
    update_profile(db_conn, client_id, {"add_allergies": ["peanut"]})
    update_profile(db_conn, other, {"add_allergies": ["shellfish"]})

    assert get_profile(db_conn, client_id)["allergies"] == ["peanut"]
    assert get_profile(db_conn, other)["allergies"] == ["shellfish"]


def test_pantry_is_isolated_per_client(db_conn: sqlite3.Connection, client_id: int):
    other = _other_client(db_conn)
    manage_pantry(db_conn, client_id, "add", ["ginger"])
    manage_pantry(db_conn, other, "add", ["basil"])

    mine = {p["item"] for p in manage_pantry(db_conn, client_id, "list")}
    theirs = {p["item"] for p in manage_pantry(db_conn, other, "list")}
    assert mine == {"ginger"}
    assert theirs == {"basil"}


def test_menu_default_lookup_never_crosses_clients(
    db_conn: sqlite3.Connection, config: Config, client_id: int
):
    """The specific bug: omitting menu_id must resolve to *this user's* most
    recent menu, never another account's, even when theirs is newer."""
    other = _other_client(db_conn)
    ids = _all_ids(db_conn)

    mine = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=2)
    plan_menu(db_conn, config, other, candidate_ids=ids, days=2)  # other's newer menu

    # No menu_id passed — must resolve to client_id's own plan, not other's.
    result = get_menu_recipes(db_conn, client_id)
    assert result["menu_id"] == mine["menu_id"]

    shopping = get_shopping_list(db_conn, config, client_id)
    assert shopping.get("error") is None or "sections" in shopping


def test_menu_lookup_by_id_rejects_other_clients_menu(
    db_conn: sqlite3.Connection, config: Config, client_id: int
):
    other = _other_client(db_conn)
    ids = _all_ids(db_conn)
    theirs = plan_menu(db_conn, config, other, candidate_ids=ids, days=2)

    # client_id explicitly asking for other's menu_id must not see it.
    assert "error" in get_menu_recipes(db_conn, client_id, theirs["menu_id"])
    assert "error" in get_shopping_list(db_conn, config, client_id, theirs["menu_id"])


# ---------------------------------------------------------------------------
# build_macro_kit
#
# Every kit test takes the `component_macros` fixture (tests/conftest.py):
# config.usda_api_key is None here, so without it every component would
# resolve to zero macros and a portion solve against zeros proves nothing.
# ---------------------------------------------------------------------------

def _runner(db_conn: sqlite3.Connection, client_id: int, **overrides) -> None:
    member = {"id": "jo", "label": "Jo", "weight_kg": 70, "daily_kcal": 2900}
    member.update(overrides)
    update_profile(db_conn, client_id, {"add_member": member, "set_meals_per_day": 3})


def test_build_macro_kit_returns_columns_rule_and_bowls(
    db_conn: sqlite3.Connection, config: Config, client_id: int, component_macros
):
    _runner(db_conn, client_id)
    kit = build_macro_kit(db_conn, config, client_id, emphasis="endurance")

    assert kit["kit_id"] > 0
    assert kit["emphasis"] == "endurance"
    assert kit["targets"]["strategy"] == "endurance"
    assert "1.8 g/kg" in kit["targets"]["basis"]

    for role in ("protein", "carb_base", "veg_volume", "fat", "sauce"):
        assert kit["columns"][role], f"{role} column is empty"
    for item in kit["columns"]["protein"]:
        assert item["kcal"] > 0 and item["protein_g"] > 0
        assert item["confidence"] in ("high", "medium")

    grams = kit["assembly_rule"]["per_meal_grams"]
    assert grams["protein"] > 0 and grams["veg_volume"] > 0
    assert "raw" in kit["assembly_rule"]["text"]

    assert 2 <= len(kit["example_bowls"]) <= 3
    for bowl in kit["example_bowls"]:
        assert bowl["items"] and bowl["kcal"] > 0
        assert 0 <= bowl["fit"] <= 1


def test_build_macro_kit_daily_totals_hit_the_strategy_target(
    db_conn: sqlite3.Connection, config: Config, client_id: int, component_macros
):
    _runner(db_conn, client_id)
    kit = build_macro_kit(db_conn, config, client_id, emphasis="endurance")

    totals = kit["daily_totals"]
    assert totals["status"] == "ok"
    assert totals["macros_on_target"] is True
    assert abs(totals["kcal"] - totals["target_kcal"]) <= 0.1 * totals["target_kcal"]
    # 1.8 g/kg for a 70 kg runner, within the solver's macro tolerance.
    assert abs(totals["protein_g"] - 126) <= 0.2 * 126


def test_build_macro_kit_defaults_emphasis_from_the_profile_strategy(
    db_conn: sqlite3.Connection, config: Config, client_id: int, component_macros
):
    _runner(db_conn, client_id)
    update_profile(db_conn, client_id, {"set_macro_strategy": "endurance"})
    kit = build_macro_kit(db_conn, config, client_id)  # no emphasis argument
    assert kit["emphasis"] == "endurance"
    assert kit["targets"]["strategy"] == "endurance"


def test_build_macro_kit_excludes_allergens_and_respects_diet(
    db_conn: sqlite3.Connection, config: Config, client_id: int, component_macros
):
    _runner(db_conn, client_id)
    update_profile(db_conn, client_id, {
        "add_allergies": ["shellfish"], "set_diet_modes": ["vegetarian"],
    })
    kit = build_macro_kit(db_conn, config, client_id)

    names = {i["name"] for col in kit["columns"].values() for i in col}
    assert "shrimp" not in names
    assert "chicken breast" not in names  # vegetarian
    assert kit["columns"]["protein"], "vegetarian protein column must not be empty"
    assert kit["diet"] == "vegetarian"


def test_build_macro_kit_drops_disliked_foods(
    db_conn: sqlite3.Connection, config: Config, client_id: int, component_macros
):
    _runner(db_conn, client_id)
    update_profile(db_conn, client_id, {"add_dislikes": ["tofu"]})
    kit = build_macro_kit(db_conn, config, client_id, exclude_foods=["white rice"])
    names = {i["name"] for col in kit["columns"].values() for i in col}
    assert "firm tofu" not in names
    assert "white rice" not in names


def test_build_macro_kit_persists_and_reloads_the_kit(
    db_conn: sqlite3.Connection, config: Config, client_id: int, component_macros
):
    from aboyeur.tools.kit import load_kit

    _runner(db_conn, client_id)
    kit = build_macro_kit(db_conn, config, client_id)
    loaded_id, loaded = load_kit(db_conn, client_id, None)  # most recent
    assert loaded_id == kit["kit_id"]
    assert loaded["assembly_rule"] == kit["assembly_rule"]


def test_build_macro_kit_flags_components_whose_macros_never_resolved(
    db_conn: sqlite3.Connection, config: Config, client_id: int
):
    """No USDA key (the test default) means nothing resolves. The kit must
    still come back, with its confidence flags and a note saying so — the
    same degrade-don't-crash contract estimate_nutrition has."""
    _runner(db_conn, client_id)
    kit = build_macro_kit(db_conn, config, client_id)
    assert kit["low_confidence_components"]
    assert any("didn't resolve" in n for n in kit["notes"])
    assert kit["columns"]["protein"], "a column must never be emptied entirely"


# ---------------------------------------------------------------------------
# Golden: an emphasis="cut" kit lands in the Built With Science envelope
# ---------------------------------------------------------------------------

# The five bowls from the "30-Day Filling Diet" PDF, as (kcal, protein, carb,
# fat) per serving. Reference points only — the PDF is a copyrighted 2024
# document and is deliberately NOT indexed into nutrition_reference (that
# corpus is public-domain 1800s-1920s text, and the prompt flags its hits as
# dated; a modern commercial PDF breaks both).
_BWS_BOWLS = [
    (428, 66, 22, 9),   # Greek chicken bowl
    (484, 42, 58, 11),  # Rainbow power breakfast bowl
    (407, 58, 34, 6),   # Tuna kimchi salad bowl
    (521, 72, 35, 13),  # Chicken konjac yakisoba
    (410, 52, 52, 2),   # Jello yogurt bowl
]


def test_cut_kit_bowls_land_in_the_bws_envelope(
    db_conn: sqlite3.Connection, config: Config, client_id: int, component_macros
):
    """A 70 kg member cutting from 2000 kcal maintenance lands on ~1250
    kcal/day; over 3 meals that is the same envelope the reference bowls
    occupy — ~400-520 kcal, 45-72 g protein, no more than ~13 g fat."""
    kcal_lo, kcal_hi = min(b[0] for b in _BWS_BOWLS), max(b[0] for b in _BWS_BOWLS)
    fat_max = max(b[3] for b in _BWS_BOWLS)

    update_profile(db_conn, client_id, {
        "add_member": {"id": "jo", "sex": "f", "weight_kg": 70,
                       "daily_kcal": 2000, "goal": "lose"},
        "set_meals_per_day": 3,
    })
    kit = build_macro_kit(db_conn, config, client_id, emphasis="cut")

    per_meal = kit["targets"]["per_meal"]
    assert kcal_lo <= per_meal["kcal"] <= kcal_hi

    in_envelope = 0
    for bowl in kit["example_bowls"]:
        assert kcal_lo * 0.95 <= bowl["kcal"] <= kcal_hi * 1.05, bowl
        assert 45 <= bowl["protein_g"] <= 72, bowl
        if bowl["fat_g"] <= fat_max + 1:
            in_envelope += 1
        else:
            # The reference list has no red meat; a leaner-than-average beef
            # still carries more fat than 13 g at a cut portion. That's a
            # real result, and the bowl has to admit it rather than sit in
            # the list looking like the others.
            assert bowl["macros_on_target"] is False, bowl
    assert in_envelope >= 2, kit["example_bowls"]

    # The kit's own recommendation — the assembly rule, and the daily math
    # that follows from it — is the one that has to land.
    assert kit["daily_totals"]["status"] == "ok"
    assert kit["daily_totals"]["macros_on_target"] is True
    assert kit["assembly_rule"]["tips"], "a cut kit carries the satiety strategies"


def test_non_cut_kits_carry_no_satiety_tips(
    db_conn: sqlite3.Connection, config: Config, client_id: int, component_macros
):
    _runner(db_conn, client_id)
    kit = build_macro_kit(db_conn, config, client_id, emphasis="endurance")
    assert kit["assembly_rule"]["tips"] == []


# ---------------------------------------------------------------------------
# Kit shopping list
# ---------------------------------------------------------------------------

def _kit_items(result: dict) -> dict:
    return {it["name"]: it for s in result["sections"].values() for it in s}


def test_kit_shopping_list_assigns_sections_and_grams(
    db_conn: sqlite3.Connection, config: Config, client_id: int, component_macros
):
    _runner(db_conn, client_id)
    kit = build_macro_kit(db_conn, config, client_id, days=7)
    result = get_shopping_list(db_conn, config, client_id, kit_id=kit["kit_id"])

    assert result["source"] == "kit"
    assert result["kit_id"] == kit["kit_id"]
    assert set(result["sections"]) <= set(shopping_engine.SECTIONS)
    items = _kit_items(result)
    assert items, "kit shopping list must not be empty"
    for item in items.values():
        assert item["approx_grams"] > 0
        assert item["meals_covered"] > 0
    # Every column the kit offers should be buyable, sauces included.
    kit_names = {i["name"] for col in kit["columns"].values() for i in col}
    assert set(items) == kit_names


def test_kit_shopping_list_quantities_scale_with_days_and_meals(
    db_conn: sqlite3.Connection, config: Config, client_id: int, component_macros
):
    _runner(db_conn, client_id)
    week = build_macro_kit(db_conn, config, client_id, days=7, meals_per_day=3)
    fortnight = build_macro_kit(db_conn, config, client_id, days=14, meals_per_day=3)

    a = _kit_items(get_shopping_list(db_conn, config, client_id, kit_id=week["kit_id"]))
    b = _kit_items(get_shopping_list(db_conn, config, client_id, kit_id=fortnight["kit_id"]))
    name = next(iter(a))
    assert b[name]["approx_grams"] == pytest.approx(a[name]["approx_grams"] * 2, rel=0.02)

    one_meal = build_macro_kit(db_conn, config, client_id, days=7, meals_per_day=1)
    c = _kit_items(get_shopping_list(db_conn, config, client_id, kit_id=one_meal["kit_id"]))
    # Fewer meals a day means fewer total meals to shop for, even though each
    # one is bigger.
    assert sum(i["meals_covered"] for i in c.values()) < sum(
        i["meals_covered"] for i in a.values())


def test_kit_shopping_list_marks_pantry_items(
    db_conn: sqlite3.Connection, config: Config, client_id: int, component_macros
):
    _runner(db_conn, client_id)
    kit = build_macro_kit(db_conn, config, client_id)
    first = kit["columns"]["protein"][0]["name"]
    manage_pantry(db_conn, client_id, "add", [first])
    result = get_shopping_list(db_conn, config, client_id, kit_id=kit["kit_id"])
    assert _kit_items(result)[first]["have"] is True


def test_shopping_list_falls_back_to_a_kit_when_there_is_no_menu(
    db_conn: sqlite3.Connection, config: Config, client_id: int, component_macros
):
    _runner(db_conn, client_id)
    kit = build_macro_kit(db_conn, config, client_id)
    result = get_shopping_list(db_conn, config, client_id)  # no id at all
    assert result["source"] == "kit"
    assert result["kit_id"] == kit["kit_id"]


def test_shopping_list_resolves_a_kit_id_passed_as_menu_id(
    db_conn: sqlite3.Connection, config: Config, client_id: int, component_macros
):
    """The model conflates the two ids; owning the row is what matters."""
    _runner(db_conn, client_id)
    kit = build_macro_kit(db_conn, config, client_id)
    result = get_shopping_list(db_conn, config, client_id, menu_id=kit["kit_id"])
    assert result["source"] == "kit"


def test_menu_shopping_list_is_unchanged_by_the_kit_path(
    db_conn: sqlite3.Connection, config: Config, client_id: int
):
    """A menu list must keep its own shape — used_in_recipes, no kit keys."""
    plan = plan_menu(db_conn, config, client_id, candidate_ids=_all_ids(db_conn), days=2)
    result = get_shopping_list(db_conn, config, client_id, menu_id=plan["menu_id"])
    assert "source" not in result
    items = [it for s in result["sections"].values() for it in s]
    assert items and all("used_in_recipes" in it for it in items)


# ---------------------------------------------------------------------------
# Kits are per-user, like every other household table
# ---------------------------------------------------------------------------

def test_kit_default_lookup_never_crosses_clients(
    db_conn: sqlite3.Connection, config: Config, client_id: int, component_macros
):
    from aboyeur.tools.kit import load_kit

    other = _other_client(db_conn)
    _runner(db_conn, client_id)
    _runner(db_conn, other)

    mine = build_macro_kit(db_conn, config, client_id)
    build_macro_kit(db_conn, config, other)  # other's kit is newer

    assert load_kit(db_conn, client_id, None)[0] == mine["kit_id"]
    assert get_shopping_list(db_conn, config, client_id)["kit_id"] == mine["kit_id"]


def test_kit_lookup_by_id_rejects_another_clients_kit(
    db_conn: sqlite3.Connection, config: Config, client_id: int, component_macros
):
    from aboyeur.tools.kit import load_kit

    other = _other_client(db_conn)
    _runner(db_conn, other)
    theirs = build_macro_kit(db_conn, config, other)

    assert load_kit(db_conn, client_id, theirs["kit_id"]) is None
    assert "error" in get_shopping_list(db_conn, config, client_id, kit_id=theirs["kit_id"])
    assert "error" in get_shopping_list(db_conn, config, client_id, menu_id=theirs["kit_id"])


# ---------------------------------------------------------------------------
# set_macro_strategy
# ---------------------------------------------------------------------------

def test_set_macro_strategy_adds_strategy_macros_without_touching_menu_targets(
    db_conn: sqlite3.Connection, client_id: int
):
    update_profile(db_conn, client_id, {
        "add_member": {"id": "jo", "weight_kg": 70, "daily_kcal": 2900},
    })
    before = get_profile(db_conn, client_id)["targets"]["jo"]
    assert "strategy_daily_macros" not in before

    after = update_profile(db_conn, client_id, {"set_macro_strategy": "endurance"})
    entry = after["targets"]["jo"]
    assert after["macro_strategy"] == "endurance"
    assert entry["strategy_daily_macros"]["protein_g"] == 126.0
    # The menu path's own numbers must be byte-identical.
    assert entry["per_meal"] == before["per_meal"]
    assert entry["daily_macros"] == before["daily_macros"]


def test_set_macro_strategy_ignores_an_unknown_strategy(
    db_conn: sqlite3.Connection, client_id: int
):
    profile = update_profile(db_conn, client_id, {"set_macro_strategy": "carnivore"})
    assert "macro_strategy" not in profile


# ---------------------------------------------------------------------------
# plan_menu continuation (extend_menu_id) + the code-rendered STEP-1 summary
# ---------------------------------------------------------------------------

def test_plan_menu_extends_an_existing_menu_in_place(
    db_conn: sqlite3.Connection, config: Config, client_id: int,
):
    """'Give me the remaining 2 days' — the new days join the same menu_id,
    numbered after the existing ones, with no dish repeated across the
    merged plan and plan-wide numbers covering all 7 days."""
    from aboyeur.tools.menu import plan_menu
    _full_variety(db_conn, client_id)
    ids = _all_ids(db_conn)
    first = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=5)
    assert "error" not in first
    mid = first["menu_id"]
    assert sorted({d["day"] for d in first["days"]}) == [1, 2, 3, 4, 5]
    menus_before = db_conn.execute("SELECT COUNT(*) AS n FROM menus").fetchone()["n"]

    merged = plan_menu(
        db_conn, config, client_id, candidate_ids=ids, days=2, extend_menu_id=mid,
    )
    assert "error" not in merged, merged
    assert merged["menu_id"] == mid  # same row, not a new one
    assert merged["appended_days"] == [6, 7]
    assert sorted({d["day"] for d in merged["days"]}) == [1, 2, 3, 4, 5, 6, 7]
    assert db_conn.execute("SELECT COUNT(*) AS n FROM menus").fetchone()["n"] == menus_before

    # The original five days are exactly what was shown before.
    old_slots = {(d["day"], d["meal"], d["role"]): d["recipe_id"] for d in first["days"]}
    kept = {(d["day"], d["meal"], d["role"]): d["recipe_id"] for d in merged["days"] if d["day"] <= 5}
    assert kept == old_slots

    # Dedup holds across the whole merged plan, by id and by normalized title.
    recipe_ids = [d["recipe_id"] for d in merged["days"]]
    assert len(recipe_ids) == len(set(recipe_ids))
    titles = [optimizer.norm_title(d["title"]) for d in merged["days"]]
    assert len(titles) == len(set(titles))

    # Plan-wide fields reflect all seven days.
    assert [t["day"] for t in merged["daily_totals"]] == [1, 2, 3, 4, 5, 6, 7]
    assert merged["macro_fit_avg"] == pytest.approx(
        sum(d["macro_fit"] for d in merged["days"]) / len(merged["days"]), abs=0.01
    )
    assert merged["ingredient_summary"]["total_unique"] >= first["ingredient_summary"]["total_unique"]
    for e in merged["ingredient_summary"]["shared"]:
        assert e["recipes"] >= 2

    # ...and that's what is persisted under the original id.
    row = db_conn.execute("SELECT days_json FROM menus WHERE id = ?", (mid,)).fetchone()
    stored = json.loads(row["days_json"])
    assert sorted({d["day"] for d in stored["days"]}) == [1, 2, 3, 4, 5, 6, 7]
    assert "days 1-7" in stored["summary_markdown"]


def test_plan_menu_extend_unknown_menu_errors(
    db_conn: sqlite3.Connection, config: Config, client_id: int,
):
    from aboyeur.tools.menu import plan_menu
    ids = _all_ids(db_conn)
    out = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=2, extend_menu_id=999_999)
    assert "error" in out and "999999" in out["error"]


def test_plan_menu_extend_infeasible_leaves_existing_menu_untouched(
    db_conn: sqlite3.Connection, config: Config, client_id: int, monkeypatch,
):
    """A continuation that can't be built (nothing left to widen from)
    surfaces as cannot_build, exactly like a fresh plan — the days already
    planned are neither shortened nor partially appended to."""
    from aboyeur.tools import menu as menu_mod
    from aboyeur.tools.menu import plan_menu
    monkeypatch.setattr(menu_mod, "_widen_pool", lambda *a, **k: [])
    ids = _all_ids(db_conn)
    first = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=3)
    mid = first["menu_id"]
    before = db_conn.execute("SELECT days_json FROM menus WHERE id = ?", (mid,)).fetchone()["days_json"]

    dessert_ids = [
        r["id"] for r in db_conn.execute(
            "SELECT id FROM recipes WHERE lower(title) LIKE '%cake%' OR lower(title) LIKE '%glaze%'"
        ).fetchall()
    ]
    out = plan_menu(db_conn, config, client_id, candidate_ids=dessert_ids, days=2, extend_menu_id=mid)
    assert out["error"] == "cannot_build"
    assert out["menu_id"] == mid
    assert out["existing_days_kept"] == 3
    assert out["reasons"]
    after = db_conn.execute("SELECT days_json FROM menus WHERE id = ?", (mid,)).fetchone()["days_json"]
    assert after == before


def test_plan_menu_fresh_call_is_unaffected_by_extension_support(
    db_conn: sqlite3.Connection, config: Config, client_id: int,
):
    from aboyeur.tools.menu import plan_menu
    ids = _all_ids(db_conn)
    first = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=3)
    second = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=2)
    assert second["menu_id"] != first["menu_id"]
    assert sorted({d["day"] for d in second["days"]}) == [1, 2]  # restarts at day 1
    assert "appended_days" not in second
    assert [t["day"] for t in second["daily_totals"]] == [1, 2]


def test_render_menu_summary_has_correct_day_numbers_from_code(
    db_conn: sqlite3.Connection, config: Config, client_id: int,
):
    """The STEP-1 table is generated, not transcribed: every slot's row
    carries the day the plan actually assigned it, and plan_menu ships the
    markdown alongside the JSON."""
    from aboyeur.tools.menu import plan_menu, render_menu_summary
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=3)
    md = plan["summary_markdown"]
    assert md == render_menu_summary(plan)
    assert md.startswith(f"## Menu #{plan['menu_id']} — days 1-3, 1 meal/day")
    for d in plan["days"]:
        prefix = "+ " if d["role"] == "top-up" else ""
        assert f"| {d['day']} | {prefix}{d['title']} (id {d['recipe_id']})" in md
    assert "**Daily totals vs target**" in md
    for t in plan["daily_totals"]:
        assert f"| {t['day']} | {t['kcal']} / {plan['daily_target_kcal']} |" in md
    assert f"{plan['daily_target_kcal']} kcal" in md
    assert "**Ingredients:**" in md


def test_render_menu_summary_multi_meal_and_portions():
    from aboyeur.tools.menu import render_menu_summary
    plan = {
        "menu_id": 7,
        "meals_per_day": 2,
        "daily_target_kcal": 2000,
        "daily_target_macros": {"protein_g": 150, "carb_g": 200, "fat_g": 70, "fiber_g": 30},
        "days": [
            {"day": 3, "meal": 2, "role": "main", "recipe_id": 2, "title": "Lentil Stew",
             "cuisine": None, "portion_multiplier": 1.0, "macro_fit": 0.5},
            {"day": 3, "meal": 1, "role": "main", "recipe_id": 1, "title": "Eggs",
             "cuisine": "French", "portion_multiplier": 1.5, "recipe_servings": 4, "macro_fit": 0.9},
            {"day": 3, "meal": 1, "role": "top-up", "recipe_id": 9, "title": "Toast",
             "cuisine": "French", "portion_multiplier": 1.0, "macro_fit": 0.4},
        ],
        "daily_totals": [
            {"day": 3, "kcal": 1500, "protein_g": 100, "carb_g": 150, "fat_g": 50, "fiber_g": 20,
             "target_kcal": 2000, "status": "under", "macro_status": "off",
             "gap_notes": ["protein 50 g under"]},
        ],
        "overlap_score": 10, "overlap_floor_met": False, "macro_fit_avg": 0.6,
        "ingredient_summary": {"total_unique": 5, "shared_count": 1,
                               "shared": [{"ingredient": "egg", "recipes": 2}]},
        "low_confidence_nutrition": [9],
    }
    md = render_menu_summary(plan)
    lines = md.split("\n")
    assert lines[0] == "## Menu #7 — day 3, 2 meals/day"
    assert "| Day | Meal | Dish |" in md
    # Sorted by meal then main-before-top-up, regardless of input order.
    order = [l for l in lines if l.startswith("| 3 |")]
    assert "Eggs (id 1)" in order[0] and "1.5 servings (recipe makes 4)" in order[0]
    assert "+ Toast (id 9) — second dish" in order[1]
    assert "Lentil Stew (id 2)" in order[2] and "| — |" in order[2]
    assert "| 3 | 1500 / 2000 | 100 / 150 g |" in md
    assert "off — protein 50 g under" in md
    assert "below the 25 floor" in md
    assert "egg (2)" in md
    assert "**Nutrition uncertain for:** Toast (id 9)" in md


def test_agent_hands_the_model_the_summary_markdown_not_the_table_json():
    from aboyeur.agent import _tool_result_text
    result = {
        "menu_id": 4, "summary_markdown": "## Menu #4 — days 1-2, 1 meal/day\n| Day | ...",
        "days": [{"day": 1, "recipe_id": 11}, {"day": 2, "recipe_id": 12}],
        "daily_totals": [{"day": 1}, {"day": 2}],
        "ingredient_summary": {"total_unique": 3}, "targets": {"x": 1},
        "overlap_score": 30, "macro_fit_avg": 0.7,
        "low_confidence_nutrition": [12, 500, 501],  # trimmed to picked ids
        "meals_per_day": 1, "appended_days": [2],
    }
    text = _tool_result_text("plan_menu", result)
    assert text.startswith("## Menu #4 — days 1-2, 1 meal/day\n| Day | ...")  # real newlines
    assert '"days": [' not in text and '"daily_totals"' not in text
    assert '"menu_id": 4' in text and '"day_numbers": [1, 2]' in text
    assert '"appended_days": [2]' in text
    assert '"low_confidence_nutrition": [12]' in text
    # swap_menu_item shares the contract; a cannot_build result stays JSON.
    assert _tool_result_text("swap_menu_item", result).startswith("## Menu #4")
    assert _tool_result_text("plan_menu", {"error": "cannot_build", "reasons": []}).startswith("{")


def test_widen_pool_ranks_by_shared_ingredients_and_skips_desserts(
    db_conn: sqlite3.Connection, config: Config, client_id: int,
):
    from aboyeur.tools import menu as menu_mod
    from aboyeur.tools.profile import get_profile
    profile = get_profile(db_conn, client_id)
    anchor = db_conn.execute(
        "SELECT id, ingredients_json FROM recipes WHERE lower(title) LIKE '%gosht%' LIMIT 1"
    ).fetchone()
    seeds = optimizer.meaningful_ingredients(json.loads(anchor["ingredients_json"]))
    assert seeds
    got = menu_mod._widen_pool(
        db_conn, config, profile, pantry=set(), exclude_ids={anchor["id"]}, exclude_titles=set(),
        seed_ingredients=seeds, want=6, uncached_cap=6,
    )
    assert got and len(got) <= 6
    assert anchor["id"] not in {c["id"] for c in got}
    assert not any(optimizer.is_menu_ineligible(c["title"], c["ingredients"], c.get("course")) for c in got)
    overlaps = [len(optimizer.meaningful_ingredients(c["ingredients"]) & seeds) for c in got]
    assert overlaps == sorted(overlaps, reverse=True)  # best-shared first
    assert overlaps[0] > 0


def test_widen_pool_caps_uncached_nutrition_lookups(
    db_conn: sqlite3.Connection, config: Config, client_id: int,
):
    from aboyeur.tools import menu as menu_mod
    from aboyeur.tools.profile import get_profile
    profile = get_profile(db_conn, client_id)
    got = menu_mod._widen_pool(
        db_conn, config, profile, pantry=set(), exclude_ids=set(), exclude_titles=set(),
        seed_ingredients=set(), want=50, uncached_cap=2,
    )
    # No nutrition rows exist in a fresh fixture DB, so everything is
    # uncached and the cap is the whole budget.
    assert len(got) == 2
