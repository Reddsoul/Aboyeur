"""feature-spec-2026-09-26.md — macro overrides, client notes, tolerance,
PDF scaling, tools-available. One file per spec item, pure-logic tests
first, DB-backed tool tests where a spec item needs the real schema.
"""

from __future__ import annotations

import sqlite3

import pytest

from aboyeur.config import Config
from aboyeur.engines import nutrition as nutrition_engine
from aboyeur.engines import optimizer
from aboyeur.export_pdf import scale_ingredient_line, scale_recipe_ingredients_markdown
from aboyeur.migrate import migrate
from aboyeur.tools.nutrition import estimate_nutrition, set_recipe_macro_override
from aboyeur.tools.profile import get_profile, update_profile


# ---------------------------------------------------------------------------
# #1 manual macro override
# ---------------------------------------------------------------------------

def test_set_recipe_macro_override_wins_over_calculated(db_conn: sqlite3.Connection, config: Config):
    row = db_conn.execute("SELECT id FROM recipes LIMIT 1").fetchone()
    rid = row["id"]
    # Seed a calculated row first, as a normal estimate would.
    db_conn.execute(
        "INSERT INTO nutrition (recipe_id, kcal, protein_g, carbs_g, fat_g, fiber_g, "
        "confidence, estimated_at) VALUES (?, 100, 5, 10, 2, 1, 'medium', 'x')",
        (rid,),
    )
    db_conn.commit()

    out = set_recipe_macro_override(
        db_conn, rid, {"kcal": 500, "protein_g": 40, "carb_g": 40, "fat_g": 15}
    )
    assert out["is_override"] is True

    result = estimate_nutrition(db_conn, config, [rid])
    assert result[rid]["kcal"] == 500.0
    assert result[rid]["confidence"] == "override"

    cleared = set_recipe_macro_override(db_conn, rid, None, clear=True)
    assert cleared["is_override"] is False
    restored = estimate_nutrition(db_conn, config, [rid])
    assert restored[rid]["kcal"] == 100.0  # the calculated row underneath, untouched


def test_set_recipe_macro_override_rejects_atwater_inconsistent(db_conn: sqlite3.Connection):
    row = db_conn.execute("SELECT id FROM recipes LIMIT 1").fetchone()
    out = set_recipe_macro_override(
        db_conn, row["id"], {"kcal": 900, "protein_g": 1, "carb_g": 1, "fat_g": 1}
    )
    assert "error" in out


def test_set_recipe_macro_override_unknown_recipe(db_conn: sqlite3.Connection):
    out = set_recipe_macro_override(db_conn, 999_999, {"kcal": 100, "protein_g": 5, "carb_g": 5, "fat_g": 5})
    assert "error" in out


def test_validated_recipe_macros_allows_over_100g_serving():
    # A real serving can carry more than 100g of protein+carb+fat combined —
    # validated_per_100g's 105g cap must not apply here.
    macros = nutrition_engine.validated_recipe_macros(
        {"kcal": 900, "protein_g": 60, "carb_g": 80, "fat_g": 40}
    )
    assert macros is not None
    assert macros["kcal"] == 900.0


# ---------------------------------------------------------------------------
# #1b manual member target override (daily kcal/macros, not just recipes)
# ---------------------------------------------------------------------------

def test_member_target_override_wins_over_computed_bmr(db_conn: sqlite3.Connection, client_id: int):
    update_profile(db_conn, client_id, {"add_member": {
        "id": "sam", "label": "Sam", "sex": "m", "age": 30,
        "height_cm": 180, "weight_kg": 80, "activity": "moderate",
    }})
    before = get_profile(db_conn, client_id)["targets"]["sam"]
    assert before["source"] == "bmr"

    update_profile(db_conn, client_id, {"set_member_target_override": {
        "id": "sam", "kcal": 2000, "protein_g": 180, "carb_g": 150, "fat_g": 60,
    }})
    after = get_profile(db_conn, client_id)["targets"]["sam"]
    assert after["source"] == "override"
    assert after["daily_kcal"] == 2000
    # The override's own macros must reach daily_macros — this is what
    # plan_menu actually reads for its target_macros, not the fixed
    # 30/40/30 split the computed path would otherwise apply.
    assert after["daily_macros"]["protein_g"] == 180.0
    assert after["daily_macros"]["carb_g"] == 150.0
    assert after["daily_macros"]["fat_g"] == 60.0
    # bmr_kcal stays populated for reference even while overridden.
    assert after["bmr_kcal"] == before["bmr_kcal"]


def test_member_target_override_clears_back_to_computed(db_conn: sqlite3.Connection, client_id: int):
    update_profile(db_conn, client_id, {"add_member": {
        "id": "sam", "label": "Sam", "sex": "m", "age": 30,
        "height_cm": 180, "weight_kg": 80, "activity": "moderate",
    }})
    before = get_profile(db_conn, client_id)["targets"]["sam"]["daily_kcal"]
    update_profile(db_conn, client_id, {"set_member_target_override": {
        "id": "sam", "kcal": 2000, "protein_g": 180, "carb_g": 150, "fat_g": 60,
    }})
    update_profile(db_conn, client_id, {"clear_member_target_override": "sam"})
    after = get_profile(db_conn, client_id)["targets"]["sam"]
    assert after["source"] == "bmr"
    assert after["daily_kcal"] == before


def test_member_target_override_rejects_atwater_inconsistent(db_conn: sqlite3.Connection, client_id: int):
    update_profile(db_conn, client_id, {"add_member": {"id": "sam", "label": "Sam"}})
    with pytest.raises(ValueError):
        update_profile(db_conn, client_id, {"set_member_target_override": {
            "id": "sam", "kcal": 2000, "protein_g": 1, "carb_g": 1, "fat_g": 1,
        }})


def test_member_target_override_from_stated_macros_derives_kcal(db_conn: sqlite3.Connection, client_id: int):
    # "190-210 C, 110-130 P, 65-75 F, >=25 fiber" — midpoints, no kcal given.
    update_profile(db_conn, client_id, {"add_member": {
        "id": "b", "label": "B", "sex": "f", "age": 27,
        "height_cm": 175, "weight_kg": 66, "activity": "active", "goal": "lose", "goal_weight_kg": 60,
    }})
    update_profile(db_conn, client_id, {"set_member_target_override": {
        "id": "b", "protein_g": 120, "carb_g": 200, "fat_g": 70, "fiber_g": 25,
    }})
    t = get_profile(db_conn, client_id)["targets"]["b"]
    assert t["source"] == "override"
    assert t["daily_kcal"] == 4 * 120 + 4 * 200 + 9 * 70
    assert t["daily_macros"]["fiber_g"] == 25.0


def test_member_target_override_partial_keeps_other_macros(db_conn: sqlite3.Connection, client_id: int):
    # "she needs 140g protein" must not zero carbs/fat.
    update_profile(db_conn, client_id, {"add_member": {
        "id": "b", "label": "B", "sex": "f", "age": 27,
        "height_cm": 175, "weight_kg": 66, "activity": "active",
    }})
    before = get_profile(db_conn, client_id)["targets"]["b"]["daily_macros"]
    update_profile(db_conn, client_id, {"set_member_target_override": {"id": "b", "protein_g": 140}})
    after = get_profile(db_conn, client_id)["targets"]["b"]["daily_macros"]
    assert after["protein_g"] == 140.0
    assert after["carb_g"] == pytest.approx(before["carb_g"], abs=0.1)
    assert after["fat_g"] == pytest.approx(before["fat_g"], abs=0.1)
    assert after["kcal"] == pytest.approx(4 * 140 + 4 * before["carb_g"] + 9 * before["fat_g"], abs=1)


def test_member_target_override_kcal_only_scales_split(db_conn: sqlite3.Connection, client_id: int):
    update_profile(db_conn, client_id, {"add_member": {
        "id": "b", "label": "B", "sex": "f", "age": 27,
        "height_cm": 175, "weight_kg": 66, "activity": "active",
    }})
    before = get_profile(db_conn, client_id)["targets"]["b"]["daily_macros"]
    update_profile(db_conn, client_id, {"set_member_target_override": {"id": "b", "kcal": 1950}})
    after = get_profile(db_conn, client_id)["targets"]["b"]
    assert after["daily_kcal"] == 1950
    ratio = 1950 / before["kcal"]
    assert after["daily_macros"]["protein_g"] == pytest.approx(before["protein_g"] * ratio, abs=0.2)


def test_member_target_override_rejects_unknown_member(db_conn: sqlite3.Connection, client_id: int):
    with pytest.raises(ValueError):
        update_profile(db_conn, client_id, {"set_member_target_override": {
            "id": "nobody", "kcal": 2000, "protein_g": 150, "carb_g": 150, "fat_g": 60,
        }})


def test_profile_save_preserves_target_override_across_household_form_save(
    db_conn: sqlite3.Connection, client_id: int,
):
    from backend.profile_ui import save_profile

    update_profile(db_conn, client_id, {"add_member": {
        "id": "sam", "label": "Sam", "sex": "m", "age": 30,
        "height_cm": 180, "weight_kg": 80, "activity": "moderate",
    }})
    update_profile(db_conn, client_id, {"set_member_target_override": {
        "id": "sam", "kcal": 2000, "protein_g": 180, "carb_g": 150, "fat_g": 60,
    }})
    # A wholesale profile-form save (Profile.tsx's "Save changes") re-sends
    # the household without knowing about target_override — it must not
    # silently drop the override.
    saved = save_profile(db_conn, client_id, {
        "household": [{"id": "sam", "label": "Sam", "sex": "m", "age": "31",
                        "height_cm": "180", "weight_kg": "80", "activity": "moderate"}],
        "allergies": [], "dislikes": [], "likes": [], "cuisine_affinity": [],
        "diet_modes": [], "meals_per_day": 3, "units": "metric",
    })
    assert saved["targets"]["sam"]["source"] == "override"
    assert saved["targets"]["sam"]["daily_kcal"] == 2000


# ---------------------------------------------------------------------------
# #2 course-vs-slot + meal-type hints + meal-prep
# ---------------------------------------------------------------------------

def _candidate(id_, title, course, ingredients=None, kcal=400):
    return {
        "id": id_, "title": title, "book": "b", "path": f"/{id_}", "cuisine": None,
        "servings": 4, "course": course, "meal_complete": 1,
        "ingredients": ingredients or ["chicken", "rice"],
        "macros": {"calories": kcal, "protein_g": 30, "carb_g": 40, "fat_g": 10, "fiber_g": 5},
        "confidence": "medium",
    }


def test_breakfast_course_preferred_for_slot_one_not_later():
    candidates = [
        _candidate(1, "Oatmeal", "breakfast"),
        _candidate(2, "Roast Chicken Dinner", "main"),
        _candidate(3, "Beef Stew", "main"),
        _candidate(4, "Grilled Salmon", "main"),
    ]
    result = optimizer.select_menu(
        candidates, days=1, pantry=set(), target_kcal=400, meals_per_day=2, variety=100,
    )
    picks = {p.meal: p.title for p in result["picks"] if p.role == "main"}
    assert picks[1] == "Oatmeal"
    assert picks[2] != "Oatmeal"


def test_meal_type_hint_overrides_classified_course():
    # "Steak" is classified main, but the client's own hint says it's their
    # breakfast — the hint should win slot 1 over an actual breakfast dish
    # when it's a better fit otherwise, at minimum it must not be penalized
    # for sitting in slot 1.
    candidates = [
        _candidate(1, "Steak Frites", "main", ingredients=["steak", "potato"]),
        _candidate(2, "Beef Stew", "main"),
        _candidate(3, "Grilled Salmon", "main"),
    ]
    hints = {"steak": ["breakfast"]}
    result = optimizer.select_menu(
        candidates, days=1, pantry=set(), target_kcal=400, meals_per_day=2, variety=100,
        meal_type_hints=hints,
    )
    picks = {p.meal: p.title for p in result["picks"] if p.role == "main"}
    assert picks[1] == "Steak Frites"


def test_hinted_meal_types_matches_title_and_ingredients():
    c = {"title": "Morning Bowl", "ingredients": ["steak", "eggs"]}
    assert optimizer.hinted_meal_types(c, {"steak": ["breakfast"]}) == {"breakfast"}
    assert optimizer.hinted_meal_types(c, {"tofu": ["lunch"]}) is None
    assert optimizer.hinted_meal_types(c, None) is None


def test_meal_prep_requested_filters_pool_when_enough_remain():
    candidates = [
        _candidate(1, "Weeknight Stir Fry", "main"),
        _candidate(2, "Big Batch Chili", "main"),
    ]
    candidates[0]["meal_prep"] = False
    candidates[1]["meal_prep"] = True
    # Only the meal-prep dish should ever be picked when a second day's worth
    # is needed and the filter kicks in (needs >= needed_distinct to admit).
    result = optimizer.select_menu(
        candidates, days=1, pantry=set(), target_kcal=400, meals_per_day=1, variety=100,
        meal_prep_requested=True,
    )
    assert len(candidates) >= 1  # sanity: fixture is non-empty
    # With only one meal_prep=True dish and needed_distinct=1, the filter
    # keeps the pool at exactly that one dish.
    picks = [p.title for p in result["picks"]]
    assert picks == ["Big Batch Chili"]


# ---------------------------------------------------------------------------
# #3 macro tolerance ±10g + raw-ingredient fallback
# ---------------------------------------------------------------------------

def test_day_macro_status_flags_gap_over_10g_even_within_25pct_band():
    # 160g protein target, 25% band = 40g — a 15g miss passes the old rule,
    # must fail the new absolute-gram one.
    totals = {"kcal": 2500, "protein_g": 145, "carb_g": 250, "fat_g": 80}
    kcal_status, macro_status, notes = optimizer.day_macro_status(
        totals, target_kcal=2500,
        target_macros={"protein_g": 160, "carb_g": 250, "fat_g": 80},
    )
    assert kcal_status == "ok"
    assert macro_status == "off"
    assert any("protein" in n for n in notes)


def test_day_macro_status_percentage_still_governs_small_targets():
    # 20g fat target: the 25% band (5g) is tighter than the flat 10g figure,
    # so a small target isn't loosened to ±10g.
    totals = {"kcal": 2000, "protein_g": 100, "carb_g": 200, "fat_g": 27}
    _, macro_status, notes = optimizer.day_macro_status(
        totals, target_kcal=2000,
        target_macros={"protein_g": 100, "carb_g": 200, "fat_g": 20},
    )
    assert macro_status == "off"
    assert any("fat" in n for n in notes)


def test_day_macro_status_ok_within_the_tighter_band():
    totals = {"kcal": 2505, "protein_g": 158, "carb_g": 248, "fat_g": 79}
    kcal_status, macro_status, notes = optimizer.day_macro_status(
        totals, target_kcal=2500,
        target_macros={"protein_g": 160, "carb_g": 250, "fat_g": 80},
    )
    assert kcal_status == "ok"
    assert macro_status == "ok"
    assert notes == []


def test_partial_usda_status_extrapolates_instead_of_zeroing(db_conn, config, monkeypatch):
    from aboyeur.engines import nutrition as ne

    calls = {"n": 0}

    def fake_lookup(conn, cfg, name):
        calls["n"] += 1
        # Only 1 of 4 resolves — under the 50% bar that would read "success".
        if calls["n"] == 1:
            return {"kcal": 200, "protein_g": 20, "carb_g": 10, "fat_g": 5, "fiber_g": 2, "status": "success"}
        return {"kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "fiber_g": 0, "status": "failed"}

    monkeypatch.setattr(ne, "lookup_ingredient_macros", fake_lookup)
    result = ne.estimate_recipe_macros(
        db_conn, config, ["a", "b", "c", "d"], servings=1,
    )
    assert result["usda_status"] == "partial"
    # Extrapolated from the 2 resolved ingredients over all 4 -> ~2x a
    # single resolved ingredient's contribution, not half of it.
    assert result["calories"] > 200


# ---------------------------------------------------------------------------
# #4 PDF ingredient scaling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line,mult,expected", [
    ("6 eggs, beaten", 0.5, "3 eggs, beaten"),
    ("2 tablespoons olive oil", 1.5, "3 tablespoons olive oil"),
    ("1/2 cup rice", 3, "1 ½ cup rice"),
    ("salt to taste", 2, "salt to taste"),
    ("1 1/2 cups sugar", 2, "3 cups sugar"),
    ("2 (14.5-ounce) cans tomatoes", 2, "4 (14.5-ounce) cans tomatoes"),
])
def test_scale_ingredient_line(line, mult, expected):
    assert scale_ingredient_line(line, mult) == expected


def test_scale_ingredient_line_discrete_rounds_up_with_note():
    out = scale_ingredient_line("3 chicken breasts", 1.4)
    assert out.startswith("5 chicken breasts")
    assert "rounded up" in out


def test_scale_recipe_ingredients_markdown_leaves_instructions_alone():
    md = (
        "# Title\n\n## Ingredients\n\n- 2 eggs\n- 1 cup flour\n\n"
        "## Instructions\n\n1. Mix the eggs and flour.\n"
    )
    scaled = scale_recipe_ingredients_markdown(md, 2.0)
    assert "- 4 eggs" in scaled
    assert "- 2 cup flour" in scaled
    assert "1. Mix the eggs and flour." in scaled  # untouched


def test_scale_recipe_ingredients_markdown_noop_at_1x():
    md = "# T\n\n## Ingredients\n\n- 2 eggs\n"
    assert scale_recipe_ingredients_markdown(md, 1.0) == md


# ---------------------------------------------------------------------------
# #5 tools-available / equipment filtering
# ---------------------------------------------------------------------------

def test_equipment_blocked_only_when_classified_and_missing():
    assert optimizer.equipment_blocked(None, ["oven"]) is False  # unclassified
    assert optimizer.equipment_blocked([], ["oven"]) is False  # classified, needs nothing
    assert optimizer.equipment_blocked(["blender"], None) is False  # client unset -> no filter
    assert optimizer.equipment_blocked(["blender"], ["oven", "stovetop"]) is True
    assert optimizer.equipment_blocked(["oven"], ["oven", "stovetop"]) is False


def test_select_menu_hard_excludes_recipe_needing_missing_equipment():
    candidates = [
        _candidate(1, "Blender Smoothie Bowl", "main"),
        _candidate(2, "Stovetop Chicken", "main"),
    ]
    candidates[0]["equipment"] = ["blender"]
    candidates[1]["equipment"] = ["stovetop"]
    result = optimizer.select_menu(
        candidates, days=1, pantry=set(), target_kcal=400, meals_per_day=1,
        tools_available=["stovetop"],
    )
    picks = [p.title for p in result["picks"]]
    assert picks == ["Stovetop Chicken"]


def test_classify_equipment_keyword_scan():
    from aboyeur.classify_equipment import keyword_equipment
    tags = keyword_equipment("Blend the mixture in a blender, then bake in the oven.")
    assert "blender" in tags
    assert "oven" in tags
    assert "wok" not in tags


def test_classify_meal_prep_batch_vs_last_minute():
    from aboyeur.classify_meal_prep import is_meal_prep_friendly
    assert is_meal_prep_friendly("Beef Stew", "main") is True
    assert is_meal_prep_friendly("Grilled Salmon Stir Fry", "main") is False
    assert is_meal_prep_friendly("Big Batch Chili", "side") is False  # not a main


# ---------------------------------------------------------------------------
# migration idempotency for the new columns
# ---------------------------------------------------------------------------

def test_migrate_adds_new_columns_idempotently(db_conn: sqlite3.Connection):
    migrate(db_conn)
    migrate(db_conn)  # must not raise on a second run
    nutrition_cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(nutrition)")}
    recipe_cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(recipes)")}
    assert {"override_json", "is_override"} <= nutrition_cols
    assert {"equipment_json", "equipment_classified_at", "meal_prep", "meal_prep_at"} <= recipe_cols
