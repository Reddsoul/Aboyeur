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
from aboyeur.tools import (
    estimate_nutrition,
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

def test_get_profile_default_is_empty_but_shaped(db_conn: sqlite3.Connection):
    profile = get_profile(db_conn)
    assert profile["allergies"] == []
    assert profile["targets"] == {}


def test_update_profile_allergy_roundtrip(db_conn: sqlite3.Connection):
    update_profile(db_conn, {"add_allergies": ["Peanut", "shellfish"]})
    profile = get_profile(db_conn)
    assert profile["allergies"] == ["peanut", "shellfish"]

    update_profile(db_conn, {"remove_allergies": ["peanut"]})
    profile = get_profile(db_conn)
    assert profile["allergies"] == ["shellfish"]


def test_update_profile_add_member_computes_targets(db_conn: sqlite3.Connection):
    update_profile(db_conn, {"add_member": {
        "id": "adult_1", "label": "Jose", "sex": "m", "age": 30,
        "height_cm": 178, "weight_kg": 80, "activity": "moderate",
    }})
    profile = get_profile(db_conn)
    assert "adult_1" in profile["targets"]
    assert profile["targets"]["adult_1"]["source"] == "bmr"


def test_update_profile_unrelated_field_never_touches_allergies(db_conn: sqlite3.Connection):
    update_profile(db_conn, {"add_allergies": ["peanut"]})
    update_profile(db_conn, {"set_meals_per_day": 4})
    profile = get_profile(db_conn)
    assert profile["allergies"] == ["peanut"]
    assert profile["meals_per_day"] == 4


def test_update_profile_add_member_merges_not_replaces(db_conn: sqlite3.Connection):
    update_profile(db_conn, {"add_member": {
        "id": "adult_1", "label": "Jose", "sex": "m", "age": 30,
        "height_cm": 178, "weight_kg": 80, "activity": "moderate",
    }})
    # Later patch only sets meals_per_day for this one person — anthropometrics
    # must survive, otherwise the BMR target silently degrades to the fallback.
    update_profile(db_conn, {"add_member": {"id": "adult_1", "meals_per_day": 1}})
    profile = get_profile(db_conn)
    member = next(m for m in profile["household"] if m["id"] == "adult_1")
    assert member["meals_per_day"] == 1
    assert member["height_cm"] == 178
    assert member["weight_kg"] == 80
    assert profile["targets"]["adult_1"]["source"] == "bmr"


def test_update_profile_add_member_accepts_list(db_conn: sqlite3.Connection):
    # The local model sometimes sends add_member as a list despite the schema
    # saying object — this used to raise TypeError and kill the stream.
    update_profile(db_conn, {"add_member": [
        {"id": "adult_1", "label": "Jose"},
        {"id": "kid_1", "label": "Sam", "meals_per_day": 1},
    ]})
    profile = get_profile(db_conn)
    assert {m["id"] for m in profile["household"]} == {"adult_1", "kid_1"}


def test_update_profile_add_member_rejects_non_dict(db_conn: sqlite3.Connection):
    with pytest.raises(ValueError, match="add_member"):
        update_profile(db_conn, {"add_member": "adult_1"})
    with pytest.raises(ValueError, match="add_member"):
        update_profile(db_conn, {"add_member": {"label": "no id"}})


# ---------------------------------------------------------------------------
# manage_pantry
# ---------------------------------------------------------------------------

def test_manage_pantry_add_list_remove_clear(db_conn: sqlite3.Connection):
    manage_pantry(db_conn, "add", ["ginger", "soy sauce"])
    state = manage_pantry(db_conn, "list")
    assert {i["item"] for i in state} == {"ginger", "soy sauce"}

    manage_pantry(db_conn, "remove", ["ginger"])
    state = manage_pantry(db_conn, "list")
    assert {i["item"] for i in state} == {"soy sauce"}

    manage_pantry(db_conn, "clear")
    assert manage_pantry(db_conn, "list") == []


def test_manage_pantry_unknown_action_raises(db_conn: sqlite3.Connection):
    import pytest
    with pytest.raises(ValueError):
        manage_pantry(db_conn, "bogus")


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

def test_plan_menu_returns_requested_days(db_conn: sqlite3.Connection, config: Config):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, candidate_ids=ids, days=3)
    assert len(plan["days"]) == 3
    assert "overlap_score" in plan
    assert "macro_fit_avg" in plan
    assert "menu_id" in plan


def test_plan_menu_persists_to_menus_table(db_conn: sqlite3.Connection, config: Config):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, candidate_ids=ids, days=3)
    row = db_conn.execute("SELECT status FROM menus WHERE id = ?", (plan["menu_id"],)).fetchone()
    assert row is not None
    assert row["status"] == "draft"


def test_plan_menu_never_repeats_a_recipe(db_conn: sqlite3.Connection, config: Config):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, candidate_ids=ids, days=5)
    recipe_ids = [d["recipe_id"] for d in plan["days"]]
    assert len(recipe_ids) == len(set(recipe_ids))


def test_swap_menu_item_replaces_the_right_day(db_conn: sqlite3.Connection, config: Config):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, candidate_ids=ids, days=3)
    used = {d["recipe_id"] for d in plan["days"]}
    replacement = next(i for i in ids if i not in used)

    swapped = swap_menu_item(db_conn, config, day=1, replacement_id=replacement, menu_id=plan["menu_id"])
    assert swapped["days"][0]["recipe_id"] == replacement
    assert swapped["days"][0]["pick_reason"] == "manual swap"


def test_swap_menu_item_defaults_to_most_recent_menu(db_conn: sqlite3.Connection, config: Config):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, candidate_ids=ids, days=3)
    used = {d["recipe_id"] for d in plan["days"]}
    replacement = next(i for i in ids if i not in used)

    swapped = swap_menu_item(db_conn, config, day=1, replacement_id=replacement)  # no menu_id
    assert swapped["menu_id"] == plan["menu_id"]
    assert swapped["days"][0]["recipe_id"] == replacement


def test_swap_menu_item_persists_the_update(db_conn: sqlite3.Connection, config: Config):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, candidate_ids=ids, days=3)
    used = {d["recipe_id"] for d in plan["days"]}
    replacement = next(i for i in ids if i not in used)
    swap_menu_item(db_conn, config, day=1, replacement_id=replacement, menu_id=plan["menu_id"])

    row = db_conn.execute("SELECT days_json FROM menus WHERE id = ?", (plan["menu_id"],)).fetchone()
    stored = json.loads(row["days_json"])
    assert stored["days"][0]["recipe_id"] == replacement


def test_plan_menu_meals_per_day_grouped_into_day_and_meal_in_one_call(
    db_conn: sqlite3.Connection, config: Config
):
    # The actual fix for a real incident: plan_menu used to be called once
    # PER MEAL by the LLM (N sequential LLM round trips) — a 6-day x 3-meal
    # request burned all 60 tool-call rounds over ~10 minutes with zero
    # output. It must now produce every meal for every day from ONE call.
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, candidate_ids=ids, days=2, meals_per_day=3)
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
    db_conn: sqlite3.Connection, config: Config
):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, candidate_ids=ids, days=2, meals_per_day=2)
    used = {d["recipe_id"] for d in plan["days"]}
    replacement = next(i for i in ids if i not in used)

    swapped = swap_menu_item(
        db_conn, config, day=1, replacement_id=replacement, meal=2, menu_id=plan["menu_id"]
    )
    target = next(d for d in swapped["days"] if d["day"] == 1 and d["meal"] == 2)
    assert target["recipe_id"] == replacement
    assert target["pick_reason"] == "manual swap"
    # day 1 meal 1 must be untouched
    other = next(d for d in swapped["days"] if d["day"] == 1 and d["meal"] == 1)
    assert other["recipe_id"] != replacement


def test_swap_menu_item_unknown_day_errors(db_conn: sqlite3.Connection, config: Config):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, candidate_ids=ids, days=2)
    result = swap_menu_item(db_conn, config, day=99, replacement_id=ids[0], menu_id=plan["menu_id"])
    assert "error" in result


def test_swap_menu_item_no_menu_exists_errors(db_conn: sqlite3.Connection, config: Config):
    result = swap_menu_item(db_conn, config, day=1, replacement_id=1)
    assert "error" in result


# ---------------------------------------------------------------------------
# get_shopping_list
# ---------------------------------------------------------------------------

def test_get_shopping_list_from_menu_id(db_conn: sqlite3.Connection, config: Config):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, candidate_ids=ids, days=3)
    shopping_list = get_shopping_list(db_conn, plan["menu_id"])
    assert shopping_list["total_items"] > 0
    assert shopping_list["to_buy"] <= shopping_list["total_items"]


def test_get_shopping_list_defaults_to_most_recent_menu(db_conn: sqlite3.Connection, config: Config):
    ids = _all_ids(db_conn)
    plan_menu(db_conn, config, candidate_ids=ids, days=3)
    shopping_list = get_shopping_list(db_conn)  # no menu_id
    assert shopping_list["total_items"] > 0


def test_get_shopping_list_pantry_have_flag(db_conn: sqlite3.Connection, config: Config):
    ids = _all_ids(db_conn)
    plan = plan_menu(db_conn, config, candidate_ids=ids, days=2)
    all_ings = {
        ing for d in plan["days"] for ing in json.loads(
            db_conn.execute("SELECT ingredients_json FROM recipes WHERE id = ?",
                             (d["recipe_id"],)).fetchone()["ingredients_json"]
        )
    }
    manage_pantry(db_conn, "add", list(all_ings)[:1])
    shopping_list = get_shopping_list(db_conn, plan["menu_id"])
    haves = [it["have"] for s in shopping_list["sections"].values() for it in s]
    assert any(haves)
