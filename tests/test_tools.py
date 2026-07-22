"""Phase 2 tests — the 9 LLM-facing tools, against the real fixture DB.

No network: config.usda_api_key is None, so nutrition estimates come back
confidence="low" rather than hitting USDA. That's the correct behavior to
test — Phase 2's acceptance bar is tool wiring + allergy safety, not live
USDA data.
"""

from __future__ import annotations

import json
import sqlite3

from aboyeur.config import Config
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
