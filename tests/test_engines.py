"""Phase 2 tests — pure engine logic, no DB, no network."""

from __future__ import annotations

from aboyeur.engines import allergens, nutrition, optimizer, shopping


# ---------------------------------------------------------------------------
# allergens
# ---------------------------------------------------------------------------

def test_expand_category_allergen_covers_real_ingredients():
    expanded = allergens.expand("shellfish")
    assert "shrimp" in expanded
    assert "octopus" in expanded
    assert "shellfish" in expanded  # literal term still included


def test_expand_literal_ingredient_is_unchanged():
    assert allergens.expand("peanut") == ["peanut"]


def test_expand_all_merges_multiple_terms():
    expanded = allergens.expand_all(["peanut", "shellfish"])
    assert "peanut" in expanded
    assert "shrimp" in expanded


# ---------------------------------------------------------------------------
# nutrition
# ---------------------------------------------------------------------------

def test_bmr_mifflin_male_vs_female_offset():
    male = nutrition.bmr_mifflin("m", 30, 178, 80)
    female = nutrition.bmr_mifflin("f", 30, 178, 80)
    assert male - female == 166.0  # +5 vs -161


def test_tdee_activity_multiplier():
    bmr = 1700.0
    assert nutrition.tdee(bmr, "sedentary") == bmr * 1.2
    assert nutrition.tdee(bmr, "very_active") == bmr * 1.9


def test_per_meal_target_splits_30_40_30():
    t = nutrition.per_meal_target(2000, 4)
    assert t["kcal"] == 500.0
    assert t["protein_g"] == round(0.30 * 500 / 4, 1)


def test_daily_kcal_for_member_prefers_anthropometrics():
    member = {"sex": "m", "age": 30, "height_cm": 178, "weight_kg": 80, "activity": "moderate"}
    assert nutrition.daily_kcal_for_member(member) == nutrition.tdee(
        nutrition.bmr_mifflin("m", 30, 178, 80), "moderate"
    )


def test_daily_kcal_for_member_falls_back_to_daily_kcal():
    member = {"daily_kcal": 1800}
    assert nutrition.daily_kcal_for_member(member) == 1800.0


def test_daily_kcal_for_member_falls_back_to_sex_aware_default_without_inventing_age():
    # No daily_kcal AND no full anthropometrics (age missing) — must NOT
    # silently invent an age to force the BMR path; must use the generic
    # sex-aware default instead.
    assert nutrition.daily_kcal_for_member({"sex": "f"}) == 1800.0
    assert nutrition.daily_kcal_for_member({"sex": "m"}) == 2300.0
    assert nutrition.daily_kcal_for_member({}) == 2000.0


def test_household_targets_labels_source_correctly():
    profile = {
        "meals_per_day": 3,
        "household": [
            {"id": "adult_1", "label": "Jose", "sex": "m", "age": 30, "height_cm": 178, "weight_kg": 80},
            {"id": "kid_1", "label": "Kid", "daily_kcal": 1600},
        ],
    }
    targets = nutrition.household_targets(profile)
    assert targets["adult_1"]["source"] == "bmr"
    assert targets["kid_1"]["source"] == "fallback"
    assert targets["kid_1"]["daily_kcal"] == 1600


def test_household_targets_member_meals_per_day_overrides_household_default():
    profile = {
        "meals_per_day": 1,  # household default: OMAD
        "household": [
            {"id": "omad_adult", "daily_kcal": 2400},
            {"id": "regular_kid", "daily_kcal": 1600, "meals_per_day": 3},
        ],
    }
    targets = nutrition.household_targets(profile)
    # No personal override -> inherits the household default (OMAD).
    assert targets["omad_adult"]["per_meal"]["kcal"] == 2400.0
    # Personal override -> NOT forced onto the household's OMAD schedule.
    assert targets["regular_kid"]["per_meal"]["kcal"] == round(1600 / 3, 1)


def test_scale_recipe_clamps_multiplier():
    macros = {"calories": 100, "protein_g": 10, "carb_g": 10, "fat_g": 5}
    scaled = nutrition.scale_recipe(macros, target_kcal=1000)  # 10x -> clamped to 4x
    assert scaled["portion_multiplier"] == 4.0

    scaled_low = nutrition.scale_recipe(macros, target_kcal=1)  # -> clamped to 0.25x
    assert scaled_low["portion_multiplier"] == 0.25


def test_scale_recipe_defaults_to_1x_when_base_unknown():
    scaled = nutrition.scale_recipe({"calories": 0}, target_kcal=500)
    assert scaled["portion_multiplier"] == 1.0


def test_flag_off_target():
    assert nutrition.flag_off_target(700, 500) == "over"
    assert nutrition.flag_off_target(300, 500) == "under"
    assert nutrition.flag_off_target(520, 500) is None
    assert nutrition.flag_off_target(999, 0) is None


# ---------------------------------------------------------------------------
# shopping
# ---------------------------------------------------------------------------

def test_categorize_ingredient_sections():
    assert shopping.categorize_ingredient("chicken thighs") == "protein"
    assert shopping.categorize_ingredient("fresh ginger") == "produce"
    assert shopping.categorize_ingredient("whole milk") == "dairy"
    assert shopping.categorize_ingredient("ground cumin") == "spice"
    assert shopping.categorize_ingredient("basmati rice") == "pantry"
    assert shopping.categorize_ingredient("") == "pantry"


def test_build_shopping_list_groups_and_flags_pantry_have():
    picks = [
        {"ingredients": ["chicken", "ginger", "salt"]},
        {"ingredients": ["chicken", "garlic", "salt"]},
    ]
    result = shopping.build_shopping_list(picks, pantry={"salt"}, dislikes=[])
    assert result["total_items"] == 4  # chicken, ginger, salt, garlic
    salt_item = next(it for s in result["sections"].values() for it in s if it["name"] == "salt")
    assert salt_item["have"] is True
    assert salt_item["used_in_recipes"] == 2
    chicken_item = next(it for s in result["sections"].values() for it in s if it["name"] == "chicken")
    assert chicken_item["have"] is False


def test_build_shopping_list_flags_dislikes():
    picks = [{"ingredients": ["eggplant"]}]
    result = shopping.build_shopping_list(picks, pantry=set(), dislikes=["eggplant"])
    item = result["sections"]["produce"][0]
    assert item["flag"] == "sub needed"


# ---------------------------------------------------------------------------
# optimizer
# ---------------------------------------------------------------------------

def test_is_dessert():
    assert optimizer.is_dessert("Chocolate Cake")
    assert not optimizer.is_dessert("Achar Gosht")


def test_is_drink():
    assert optimizer.is_drink("Mango Lassi Smoothie")
    assert optimizer.is_drink("Classic Margarita")
    assert not optimizer.is_drink("Achar Gosht")


def test_has_protein_and_main_protein():
    assert optimizer.has_protein(["chicken", "onion"])
    assert not optimizer.has_protein(["flour", "sugar", "butter"])
    assert optimizer.main_protein(["diced chicken breast", "onion"]) == "chicken"
    assert optimizer.main_protein(["flour", "sugar"]) is None


def test_meaningful_ingredients_excludes_staples():
    m = optimizer.meaningful_ingredients(["salt", "chicken", "garlic", "ginger"])
    assert m == {"chicken", "ginger"}


def test_macro_fit_neutral_when_no_target_or_estimate():
    assert optimizer.macro_fit({"calories": 0}, 500) == 0.5
    assert optimizer.macro_fit({"calories": 500}, 0) == 0.5


def test_macro_fit_perfect_and_off_target():
    assert optimizer.macro_fit({"calories": 500}, 500) == 1.0
    assert optimizer.macro_fit({"calories": 250}, 500) == 0.5


def test_compute_overlap_score_needs_two_plus_picks():
    one = [optimizer.MenuPick(1, "A", "book", "path.md", ["chicken"], [], 1.0, "")]
    assert optimizer.compute_overlap_score(one) == 0


def _candidate(id_, title, book, ingredients, kcal=500, cuisine=None):
    return {"id": id_, "title": title, "book": book, "ingredients": ingredients,
            "macros": {"calories": kcal}, "cuisine": cuisine}


def test_select_menu_excludes_desserts_from_dinner_pool():
    candidates = [
        _candidate(1, "Chocolate Cake", "BookA", ["flour", "sugar", "egg"]),
        _candidate(2, "Chicken Curry", "BookB", ["chicken", "ginger", "garlic"]),
        _candidate(3, "Beef Stew", "BookC", ["beef", "carrot", "onion"]),
    ]
    result = optimizer.select_menu(candidates, days=2, pantry=set(), target_kcal=500)
    titles = {p.title for p in result["picks"]}
    assert "Chocolate Cake" not in titles


def test_select_menu_never_repeats_a_recipe():
    candidates = [_candidate(i, f"Dish {i}", f"Book{i}", ["chicken", "ginger"]) for i in range(5)]
    result = optimizer.select_menu(candidates, days=5, pantry=set(), target_kcal=500)
    ids = [p.recipe_id for p in result["picks"]]
    assert len(ids) == len(set(ids)) == 5


def test_select_menu_rewards_ingredient_overlap():
    candidates = [
        _candidate(1, "Chicken Ginger Stir-fry", "BookA", ["chicken", "ginger", "scallion"]),
        _candidate(2, "Ginger Beef", "BookB", ["beef", "ginger", "soy sauce"]),
        _candidate(3, "Plain Rice Pilaf", "BookC", ["rice", "cardamom"]),
    ]
    result = optimizer.select_menu(candidates, days=2, pantry=set(), target_kcal=500)
    titles = [p.title for p in result["picks"]]
    # The two ginger dishes share a meaningful ingredient; pilaf shares nothing.
    assert "Plain Rice Pilaf" not in titles


def test_select_menu_meals_per_day_multiplies_pick_count_in_one_call():
    # days x meals_per_day picks from a single select_menu call — this is the
    # fix for a real incident: the LLM used to call plan_menu once PER MEAL
    # (N full LLM round trips), which burned 60 sequential tool-call rounds
    # over ~10 minutes on an 18-meal request and still hit the round cap
    # before producing anything. One call must do all N meals now.
    candidates = [_candidate(i, f"Dish {i}", f"Book{i}", ["chicken", "ginger"]) for i in range(10)]
    result = optimizer.select_menu(candidates, days=2, pantry=set(), target_kcal=500, meals_per_day=3)
    assert len(result["picks"]) == 6
    ids = [p.recipe_id for p in result["picks"]]
    assert len(ids) == len(set(ids))  # still never repeats


def test_select_menu_never_starves_pool_when_all_are_desserts():
    candidates = [_candidate(i, f"Cake {i}", f"Book{i}", ["flour", "sugar"]) for i in range(3)]
    result = optimizer.select_menu(candidates, days=3, pantry=set(), target_kcal=500)
    assert len(result["picks"]) == 3


def test_select_menu_excludes_drinks_from_dinner_pool():
    candidates = [
        _candidate(1, "Mango Smoothie", "BookA", ["mango", "yogurt", "ice"]),
        _candidate(2, "Chicken Curry", "BookB", ["chicken", "ginger", "garlic"]),
        _candidate(3, "Beef Stew", "BookC", ["beef", "carrot", "onion"]),
    ]
    result = optimizer.select_menu(candidates, days=2, pantry=set(), target_kcal=500)
    titles = {p.title for p in result["picks"]}
    assert "Mango Smoothie" not in titles


def test_select_menu_hard_filters_to_cuisine_affinity():
    candidates = [
        _candidate(1, "Fried Fish", "BookA", ["fish", "flour"], cuisine="American"),
        _candidate(2, "Basic Omelet", "BookB", ["eggs", "milk"], cuisine="French"),
        _candidate(3, "Chile Con Carne", "BookC", ["beef", "chile"], cuisine="Mexican"),
        _candidate(4, "Chana Masala", "BookD", ["chickpea", "tomato"], cuisine="Indian"),
        _candidate(5, "Tamales", "BookE", ["cornmeal", "pork"], cuisine="Mexican"),
    ]
    result = optimizer.select_menu(
        candidates, days=3, pantry=set(), target_kcal=500,
        cuisine_affinity=["Indian", "Mexican"],
    )
    cuisines = {c["cuisine"] for c in candidates if c["id"] in {p.recipe_id for p in result["picks"]}}
    assert cuisines <= {"Indian", "Mexican"}
    assert "American" not in cuisines
    assert "French" not in cuisines


def test_select_menu_cuisine_affinity_is_case_insensitive():
    candidates = [
        _candidate(1, "Chana Masala", "BookA", ["chickpea"], cuisine="indian"),
        _candidate(2, "Fried Fish", "BookB", ["fish"], cuisine="American"),
    ]
    result = optimizer.select_menu(
        candidates, days=1, pantry=set(), target_kcal=500, cuisine_affinity=["Indian"],
    )
    assert result["picks"][0].title == "Chana Masala"


def test_select_menu_cuisine_affinity_never_starves_pool():
    # Only 1 candidate matches the affinity but 3 days requested — the hard
    # filter must fall back rather than short the plan.
    candidates = [
        _candidate(1, "Chana Masala", "BookA", ["chickpea"], cuisine="Indian"),
        _candidate(2, "Fried Fish", "BookB", ["fish"], cuisine="American"),
        _candidate(3, "Basic Omelet", "BookC", ["eggs"], cuisine="French"),
    ]
    result = optimizer.select_menu(
        candidates, days=3, pantry=set(), target_kcal=500, cuisine_affinity=["Indian"],
    )
    assert len(result["picks"]) == 3


# ---------------------------------------------------------------------------
# overlap floor / daily kcal top-up / macro targets
# ---------------------------------------------------------------------------

def test_select_menu_retries_to_clear_overlap_floor():
    # Two clusters: high-kcal-fit loners vs slightly-off-target dishes that
    # share ingredients. A macro-first pass picks loners and lands under the
    # floor; the escalation pass must trade fit for sharing.
    loners = [
        _candidate(i, f"Loner {i}", f"Book{i}", [f"unique_{i}a", f"unique_{i}b", f"unique_{i}c"], kcal=500)
        for i in range(1, 5)
    ]
    sharers = [
        _candidate(10 + i, f"Sharer {i}", f"SBook{i}",
                   ["chicken thigh", "ginger root", "scallion", f"extra_{i}"], kcal=350)
        for i in range(1, 5)
    ]
    result = optimizer.select_menu(loners + sharers, days=3, pantry=set(), target_kcal=500)
    assert result["overlap_score"] >= optimizer.MIN_OVERLAP_SCORE
    assert result["overlap_floor_met"] is True


def test_select_menu_reports_floor_unmet_when_pool_shares_nothing():
    candidates = [
        _candidate(i, f"Dish {i}", f"Book{i}", [f"only_{i}x", f"only_{i}y"]) for i in range(1, 5)
    ]
    result = optimizer.select_menu(candidates, days=3, pantry=set(), target_kcal=500)
    assert result["overlap_floor_met"] is False  # best effort, honestly flagged


def test_select_menu_tops_up_under_target_days():
    # Mains are ~300 kcal against a 600 kcal/day need — every day runs under
    # and must get a second dish so the day total lands within tolerance.
    mains = [
        _candidate(i, f"Main {i}", f"Book{i}", ["chicken", "ginger", f"m_{i}"], kcal=300)
        for i in range(1, 4)
    ]
    fillers = [
        _candidate(10 + i, f"Filler {i}", f"FBook{i}", ["chicken", "rice flour", f"f_{i}"], kcal=300)
        for i in range(1, 4)
    ]
    result = optimizer.select_menu(mains + fillers, days=3, pantry=set(), target_kcal=600)
    for dt in result["daily_totals"]:
        assert dt["status"] == "ok", dt
    # every meal got a top-up dish, never more than MAX_DISHES_PER_MEAL
    for day in (1, 2, 3):
        day_picks = [p for p in result["picks"] if p.day == day]
        assert len(day_picks) == 2
        assert {p.role for p in day_picks} == {"main", "top-up"}


def test_select_menu_never_stacks_more_than_two_dishes_per_meal():
    mains = [_candidate(1, "Tiny Main", "BookA", ["chicken"], kcal=100)]
    fillers = [
        _candidate(10 + i, f"Filler {i}", f"FBook{i}", ["rice flour", f"f_{i}"], kcal=100)
        for i in range(1, 6)
    ]
    result = optimizer.select_menu(mains + fillers, days=1, pantry=set(), target_kcal=1000)
    assert len([p for p in result["picks"] if p.day == 1]) <= optimizer.MAX_DISHES_PER_MEAL


def test_select_menu_days_without_nutrition_data_flagged_unknown():
    candidates = [_candidate(i, f"Dish {i}", f"Book{i}", ["chicken"], kcal=0) for i in range(1, 4)]
    result = optimizer.select_menu(candidates, days=3, pantry=set(), target_kcal=500)
    assert all(dt["status"] == "unknown" for dt in result["daily_totals"])
    assert all(p.role == "main" for p in result["picks"])  # no fake top-ups


def test_macro_fit_blends_macro_targets():
    target_macros = {"protein_g": 40, "carb_g": 60, "fat_g": 20}
    on_macros = optimizer.macro_fit(
        {"calories": 500, "protein_g": 40, "carb_g": 60, "fat_g": 20}, 500, target_macros)
    off_macros = optimizer.macro_fit(
        {"calories": 500, "protein_g": 5, "carb_g": 120, "fat_g": 60}, 500, target_macros)
    assert on_macros == 1.0
    assert off_macros < on_macros


# ---------------------------------------------------------------------------
# ingredient name cleaning for USDA lookups
# ---------------------------------------------------------------------------

def test_clean_ingredient_name_strips_recipe_prose():
    assert nutrition.clean_ingredient_name("one-half cupful of syrup") == "syrup"
    assert nutrition.clean_ingredient_name("stiffly beaten whites of the eggs") == "whites eggs"
    assert nutrition.clean_ingredient_name("chicken broth or water") == "chicken broth"
    assert nutrition.clean_ingredient_name("salt and ground pepper to taste") == "salt pepper"


def test_clean_ingredient_name_plain_food_untouched():
    assert nutrition.clean_ingredient_name("jam") == "jam"
    assert nutrition.clean_ingredient_name("Dry Sherry") == "dry sherry"


# ---------------------------------------------------------------------------
# nutrition frontmatter persistence
# ---------------------------------------------------------------------------

_RECIPE_MD = """---
title: Test Dish
cuisine: Test
ingredients:
  - 6 eggs
tags:
  - recipe
---

# Test Dish

## Ingredients
- 6 eggs
"""


def test_write_nutrition_frontmatter_inserts_and_replaces(tmp_path):
    p = tmp_path / "dish.md"
    p.write_text(_RECIPE_MD)
    macros = {"calories": 350.0, "protein_g": 20.0, "carb_g": 10.0, "fat_g": 25.0}
    assert nutrition.write_nutrition_frontmatter(str(p), macros, "medium", "2026-08-17T00:00:00Z")
    text = p.read_text()
    assert "kcal: 350.0" in text
    assert text.count("nutrition:") == 1
    assert "# Test Dish" in text  # body untouched

    # Second write replaces the block instead of stacking a duplicate.
    assert nutrition.write_nutrition_frontmatter(
        str(p), {**macros, "calories": 400.0}, "medium", "2026-08-18T00:00:00Z")
    text = p.read_text()
    assert "kcal: 400.0" in text
    assert "kcal: 350.0" not in text
    assert text.count("nutrition:") == 1


def test_write_nutrition_frontmatter_missing_file_returns_false(tmp_path):
    macros = {"calories": 1, "protein_g": 1, "carb_g": 1, "fat_g": 1}
    assert not nutrition.write_nutrition_frontmatter(
        str(tmp_path / "nope.md"), macros, "medium", "now")
