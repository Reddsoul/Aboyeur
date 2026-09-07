"""Phase 2 tests — pure engine logic, no DB, no network."""

from __future__ import annotations

from aboyeur.data import components
from aboyeur.engines import allergens, kit, nutrition, optimizer, shopping


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


def test_goal_lose_applies_deficit_to_maintenance():
    # 2440 maintenance + lose -> 1690, inside the textbook 1500-1900 band.
    member = {"daily_kcal": 2440, "goal": "lose"}
    assert nutrition.maintenance_kcal_for_member(member) == 2440.0
    assert nutrition.daily_kcal_for_member(member) == 1690.0


def test_goal_gain_applies_surplus():
    assert nutrition.daily_kcal_for_member({"daily_kcal": 2000, "goal": "gain"}) == 2400.0


def test_goal_lose_floors_at_minimum_intake():
    # Deficit must never push below the standard minimums (1200 f / 1500 m).
    assert nutrition.daily_kcal_for_member({"sex": "f", "daily_kcal": 1600, "goal": "lose"}) == 1200.0
    assert nutrition.daily_kcal_for_member({"sex": "m", "daily_kcal": 1900, "goal": "lose"}) == 1500.0
    # Unknown sex gets the conservative 1500 floor.
    assert nutrition.daily_kcal_for_member({"daily_kcal": 1900, "goal": "lose"}) == 1500.0


def test_goal_floor_never_exceeds_maintenance():
    # Maintenance already below the floor: don't force intake UP to the floor.
    assert nutrition.daily_kcal_for_member({"sex": "m", "daily_kcal": 1400, "goal": "lose"}) == 1400.0


def test_goal_weight_drives_deficit_with_taper():
    # Far from goal (>5 kg): 0.75 kg/week -> 825 kcal/day deficit.
    far = {"daily_kcal": 2440, "weight_kg": 95, "goal_weight_kg": 80, "goal": "lose"}
    assert nutrition.goal_adjust_kcal(far) == -7700.0 * 0.75 / 7
    # Close (2-5 kg): 0.5 kg/week -> 550/day.
    near = {"daily_kcal": 2440, "weight_kg": 83, "goal_weight_kg": 80, "goal": "lose"}
    assert nutrition.goal_adjust_kcal(near) == -7700.0 * 0.5 / 7
    # Nearly there (<2 kg): 0.25 kg/week -> 275/day.
    close = {"daily_kcal": 2440, "weight_kg": 81, "goal_weight_kg": 80, "goal": "lose"}
    assert nutrition.goal_adjust_kcal(close) == -7700.0 * 0.25 / 7


def test_goal_weight_drives_gain_surplus():
    member = {"weight_kg": 60, "goal_weight_kg": 70, "goal": "gain"}
    assert nutrition.goal_adjust_kcal(member) == 7700.0 * 0.35 / 7
    member_near = {"weight_kg": 68, "goal_weight_kg": 70, "goal": "gain"}
    assert nutrition.goal_adjust_kcal(member_near) == 7700.0 * 0.25 / 7


def test_goal_without_goal_weight_uses_fixed_fallback():
    assert nutrition.goal_adjust_kcal({"goal": "lose"}) == -750.0
    assert nutrition.goal_adjust_kcal({"goal": "gain"}) == 400.0
    assert nutrition.goal_adjust_kcal({"goal": "maintain", "weight_kg": 90, "goal_weight_kg": 80}) == 0.0


def test_household_targets_include_goal_and_maintenance():
    profile = {
        "meals_per_day": 3,
        "household": [{"id": "a", "daily_kcal": 2440, "goal": "lose"}],
    }
    t = nutrition.household_targets(profile)["a"]
    assert t["goal"] == "lose"
    assert t["maintenance_kcal"] == 2440
    assert t["daily_kcal"] == 1690
    assert t["daily_macros"]["protein_g"] == round(0.30 * 1690 / 4.0, 1)


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


def test_select_menu_caps_dishes_per_meal():
    # Normally MAX_DISHES_PER_MEAL; one extra dish is allowed only when even
    # max portion scaling can't stretch the picked dishes to the day target.
    mains = [_candidate(1, "Tiny Main", "BookA", ["chicken"], kcal=100)]
    fillers = [
        _candidate(10 + i, f"Filler {i}", f"FBook{i}", ["rice flour", f"f_{i}"], kcal=100)
        for i in range(1, 6)
    ]
    result = optimizer.select_menu(mains + fillers, days=1, pantry=set(), target_kcal=1000)
    assert len([p for p in result["picks"] if p.day == 1]) <= optimizer.MAX_DISHES_PER_MEAL + 1


def test_select_menu_no_third_dish_when_scaling_can_reach_target():
    # Two 550 kcal dishes reach a 2323 kcal day within the portion clamp —
    # the normal two-dish cap holds.
    candidates = [
        _candidate(i, f"Dish {i}", f"Book{i}", ["chicken", f"x_{i}"], kcal=550)
        for i in range(1, 6)
    ]
    result = optimizer.select_menu(candidates, days=1, pantry=set(), target_kcal=2323)
    assert len(result["picks"]) <= optimizer.MAX_DISHES_PER_MEAL


def test_select_menu_scales_portions_to_reach_omad_target():
    # OMAD: 2323 kcal in one meal. Two single servings (~1100 kcal) can never
    # reach it — portion scaling must close the rest of the gap.
    candidates = [
        _candidate(i, f"Dish {i}", f"Book{i}", ["chicken", "ginger", f"x_{i}"], kcal=550)
        for i in range(1, 6)
    ]
    result = optimizer.select_menu(candidates, days=1, pantry=set(), target_kcal=2323)
    dt = result["daily_totals"][0]
    assert dt["status"] == "ok", dt
    assert abs(dt["kcal"] - 2323) <= 5
    for p in result["picks"]:
        assert p.portion_multiplier > 1.0


def test_select_menu_portion_scaling_respects_clamp():
    # A 100 kcal dish against a 1000 kcal day: honest answer is "still under",
    # not a 10x portion of a garnish.
    candidates = [_candidate(1, "Tiny", "BookA", ["chicken"], kcal=100)]
    result = optimizer.select_menu(candidates, days=1, pantry=set(), target_kcal=1000)
    p = result["picks"][0]
    assert p.portion_multiplier == optimizer.PORTION_MULT_MAX
    assert result["daily_totals"][0]["status"] == "under"


def test_scale_day_portions_scales_macros_with_kcal():
    pick = optimizer.MenuPick(1, "A", "book", "p.md", ["chicken"], [], 0.5, "",
                              day=1, meal=1, kcal=500, protein_g=40, carb_g=30, fat_g=20)
    optimizer.scale_day_portions([pick], daily_target=1000, per_meal_kcal=1000)
    assert pick.portion_multiplier == 2.0
    assert pick.kcal == 1000
    assert pick.protein_g == 80
    assert pick.carb_g == 60
    assert pick.fat_g == 40


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

def test_validated_macros_rejects_impossible_grams():
    # Observed in the wild: branded "salt" entry claiming 1330 g carbs/100 g.
    food = {"foodNutrients": [{"nutrientId": 1005, "value": 1330.0}]}
    assert nutrition._validated_macros(food) is None


def test_validated_macros_rejects_kcal_macro_disagreement():
    # Label says 500 kcal but the macros only account for ~96 — junk entry.
    food = {"foodNutrients": [
        {"nutrientId": 1008, "value": 500}, {"nutrientId": 1003, "value": 5},
        {"nutrientId": 1005, "value": 10}, {"nutrientId": 1004, "value": 4},
    ]}
    assert nutrition._validated_macros(food) is None


def test_validated_macros_recomputes_missing_kcal():
    # Some Foundation entries omit nutrient 1008 — kcal comes from Atwater.
    food = {"foodNutrients": [
        {"nutrientId": 1003, "value": 10}, {"nutrientId": 1005, "value": 20},
        {"nutrientId": 1004, "value": 10},
    ]}
    m = nutrition._validated_macros(food)
    assert m["kcal"] == 4 * 10 + 4 * 20 + 9 * 10


def test_validated_macros_rejects_all_zero_entry():
    # Empty nutrient sets are missing data, not a macro-free food.
    assert nutrition._validated_macros({"foodNutrients": []}) is None


def test_validated_macros_accepts_consistent_entry():
    food = {"foodNutrients": [
        {"nutrientId": 1008, "value": 165}, {"nutrientId": 1003, "value": 31},
        {"nutrientId": 1005, "value": 0}, {"nutrientId": 1004, "value": 3.6},
    ]}
    m = nutrition._validated_macros(food)
    assert m == {"kcal": 165.0, "protein_g": 31.0, "carb_g": 0.0, "fat_g": 3.6,
                 "status": "success"}


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


# ---------------------------------------------------------------------------
# plural folding + duplicate-dish guard (2026-08-19 audit fixes)
# ---------------------------------------------------------------------------

def test_meaningful_ingredients_folds_plurals():
    m = optimizer.meaningful_ingredients(["potatoes", "potato", "carrots"])
    assert m == {"potato", "carrot"}


def test_meaningful_ingredients_excludes_plural_staples():
    # 'onions' must be recognized as the staple 'onion'.
    m = optimizer.meaningful_ingredients(["onions", "chicken"])
    assert m == {"chicken"}


def test_norm_title_merges_double_indexed_variants():
    # Real corpus pair: same dish indexed twice from the same book.
    a = optimizer.norm_title("VEGETABLE DINNER, EASILY DIGESTED")
    b = optimizer.norm_title("Vegetable Dinner, Easily Digested (Pulmentarium Ad Ventrem)")
    assert a == b == "vegetable dinner easily digested"


def test_select_menu_never_serves_duplicate_titles():
    candidates = [
        _candidate(1, "NUT ROAST", "BookA", ["walnut", "breadcrumb", "sage"]),
        _candidate(2, "Nut Roast (Deluxe)", "BookB", ["walnut", "breadcrumb", "sage"]),
        _candidate(3, "Chicken Curry", "BookC", ["chicken", "ginger", "garlic"]),
        _candidate(4, "Beef Stew", "BookD", ["beef", "carrot", "turnip"]),
    ]
    result = optimizer.select_menu(candidates, days=3, pantry=set(), target_kcal=500)
    norm_titles = [optimizer.norm_title(p.title) for p in result["picks"]]
    assert len(norm_titles) == len(set(norm_titles)) == 3


def test_build_shopping_list_folds_plural_variants_without_canonical_map():
    picks = [
        {"ingredients": ["potatoes"]},
        {"ingredients": ["potato"]},
    ]
    result = shopping.build_shopping_list(picks, pantry=set(), dislikes=[])
    items = [it for s in result["sections"].values() for it in s]
    assert len(items) == 1
    assert items[0]["name"] == "potato"
    assert items[0]["used_in_recipes"] == 2
    assert items[0]["merged_from"] == ["potatoes"]


def test_build_shopping_list_canonical_map_merges_and_sums_grams():
    picks = [
        {"ingredients": ["irish potato", "salt"]},
        {"ingredients": ["boiled potato"]},
    ]
    canonical = {
        "irish potato": {"food": "potato", "grams": 150.0},
        "boiled potato": {"food": "potato", "grams": 120.0},
        "salt": {"food": "salt", "grams": 5.0},
    }
    result = shopping.build_shopping_list(picks, pantry=set(), dislikes=[], canonical=canonical)
    items = {it["name"]: it for s in result["sections"].values() for it in s}
    assert set(items) == {"potato", "salt"}
    assert items["potato"]["approx_grams"] == 270
    assert items["potato"]["used_in_recipes"] == 2
    assert sorted(items["potato"]["merged_from"]) == ["boiled potato", "irish potato"]


# ---------------------------------------------------------------------------
# Macro Kit: the component library
# ---------------------------------------------------------------------------

def test_component_library_rows_are_well_formed():
    """A malformed row must fail here, not silently produce a broken kit."""
    assert len(components.COMPONENTS) >= 60
    names = [c["name"] for c in components.COMPONENTS]
    assert len(names) == len(set(names)), "duplicate component names"
    for c in components.COMPONENTS:
        where = c.get("name", "<unnamed>")
        assert c["name"] and c["name"] == c["name"].lower().strip(), where
        assert c["role"] in components.ROLES, where
        assert c["usda_query"].strip(), where
        assert c["diet_ok"], where
        assert set(c["diet_ok"]) <= set(components.DIET_MODES), where
        assert 0 < c["min_g"] <= c["default_serving_g"] <= c["max_g"], where
        assert isinstance(c["tags"], list), where


def test_every_role_has_components_for_every_diet():
    """A vegan asking for a kit must not get an empty protein column."""
    for diet in components.DIET_MODES:
        for role in components.ROLES:
            pool = [c for c in components.COMPONENTS
                    if c["role"] == role and diet in c["diet_ok"]]
            assert pool, f"no {role} components for {diet}"


def test_curated_macros_overrides_pass_the_usda_sanity_gate():
    """Hand-entered macros go through the same physical checks a USDA hit
    does — grams must fit in 100 g, energy must agree with its own Atwater
    energy — so a typo fails loudly instead of poisoning a kit."""
    for c in components.COMPONENTS:
        override = c.get("macros_override")
        if not override:
            continue
        if not any(override[k] for k in ("kcal", "protein_g", "carb_g", "fat_g")):
            continue  # deliberate zero (herbs, sugar-free syrup)
        assert nutrition.validated_per_100g(override) is not None, c["name"]


# ---------------------------------------------------------------------------
# Macro Kit: macro strategies
# ---------------------------------------------------------------------------

def test_endurance_strategy_sets_protein_per_kg_bodyweight():
    t = nutrition.resolve_macro_targets({"weight_kg": 70}, 2900, "endurance")
    assert t["protein_g"] == 126.0  # 1.8 g/kg
    assert "1.8 g/kg" in t["basis"]


def test_endurance_drops_to_lower_protein_when_it_crowds_out_carbs():
    """1.8 g/kg for a 90 kg athlete on 1600 kcal would be 40% of the budget,
    leaving no room for the carbohydrate an endurance plan exists to supply."""
    t = nutrition.resolve_macro_targets({"weight_kg": 90}, 1600, "endurance")
    assert t["protein_g"] == 144.0  # 1.6 g/kg
    assert "1.6 g/kg" in t["basis"]


def test_cut_strategy_sets_protein_per_kg_and_fat_share():
    t = nutrition.resolve_macro_targets({"weight_kg": 70}, 1250, "cut")
    assert t["protein_g"] == 154.0  # 2.2 g/kg
    assert t["fat_g"] == round(0.25 * 1250 / 9, 1)


def test_every_strategy_sums_to_daily_kcal_by_atwater():
    for strategy in nutrition.MACRO_STRATEGIES:
        for member in ({"weight_kg": 70}, {"weight_kg": 110}, {}):
            t = nutrition.resolve_macro_targets(member, 2400, strategy)
            atwater = 4 * t["protein_g"] + 4 * t["carb_g"] + 9 * t["fat_g"]
            assert abs(atwater - 2400) / 2400 < 0.02, (strategy, member, t)
            assert min(t["protein_g"], t["carb_g"], t["fat_g"]) >= 0


def test_strategies_fall_back_to_calorie_shares_without_bodyweight():
    """No weight_kg must never mean an invented weight — same rule as the
    BMR path's refusal to guess an age."""
    endurance = nutrition.resolve_macro_targets({}, 2400, "endurance")
    assert endurance["protein_g"] == round(0.25 * 2400 / 4, 1)
    assert "no bodyweight" in endurance["basis"]
    cut = nutrition.resolve_macro_targets({}, 2400, "cut")
    assert cut["protein_g"] == round(0.35 * 2400 / 4, 1)


def test_unknown_strategy_falls_back_to_balanced():
    t = nutrition.resolve_macro_targets({"weight_kg": 70}, 2000, "ketogenic-carnivore")
    assert t["strategy"] == "balanced"
    assert t["protein_g"] == round(0.30 * 2000 / 4, 1)


def test_balanced_strategy_matches_the_menu_paths_split():
    """A balanced kit and the menu path must not disagree about the same
    person's macros."""
    t = nutrition.resolve_macro_targets({}, 2000, "balanced")
    menu = nutrition.per_meal_target(2000, 1)
    for key in ("protein_g", "carb_g", "fat_g"):
        assert abs(t[key] - menu[key]) < 0.5


# ---------------------------------------------------------------------------
# Macro Kit: roster filtering
# ---------------------------------------------------------------------------

def test_effective_diet_takes_the_strictest_recognized_mode():
    assert kit.effective_diet(["keto", "vegetarian"]) == "vegetarian"
    assert kit.effective_diet(["vegetarian", "vegan"]) == "vegan"
    assert kit.effective_diet(["low-fodmap"]) == "omnivore"
    assert kit.effective_diet(None) == "omnivore"


def test_filter_components_respects_diet():
    vegan = {c["name"] for c in kit.filter_components("vegan")}
    assert "chicken breast" not in vegan
    assert "0% greek yogurt" not in vegan
    assert "firm tofu" in vegan

    pescatarian = {c["name"] for c in kit.filter_components("pescatarian")}
    assert "canned tuna" in pescatarian
    assert "chicken breast" not in pescatarian


def test_filter_components_expands_allergen_categories():
    """"shellfish" has to lose shrimp, and "dairy" the yogurt column — no
    component's name contains the category word itself."""
    names = {c["name"] for c in kit.filter_components("omnivore", allergies=["shellfish"])}
    assert "shrimp" not in names
    assert "chicken breast" in names

    dairy_free = {c["name"] for c in kit.filter_components("omnivore", allergies=["dairy"])}
    assert "0% greek yogurt" not in dairy_free
    assert "low-fat cottage cheese" not in dairy_free


def test_filter_components_honors_explicit_excludes():
    names = {c["name"] for c in kit.filter_components("omnivore", exclude_foods=["tofu"])}
    assert "firm tofu" not in names
    assert "tempeh" in names


# ---------------------------------------------------------------------------
# Macro Kit: the portion solve
# ---------------------------------------------------------------------------

def _comp(name, role, per100, min_g, max_g):
    return {
        "name": name, "role": role, "min_g": min_g, "max_g": max_g,
        "default_serving_g": min_g,
        "per_100g": dict(zip(("kcal", "protein_g", "carb_g", "fat_g"), per100)),
    }


_CHICKEN = _comp("chicken breast", "protein", (120, 22.5, 0, 2.6), 80, 300)
_RICE = _comp("white rice", "carb_base", (365, 7.1, 80, 0.7), 20, 200)
_BROCCOLI = _comp("broccoli", "veg_volume", (34, 2.8, 7, 0.4), 100, 500)
_OLIVE_OIL = _comp("olive oil", "fat", (884, 0, 0, 100), 5, 30)


def test_solve_bowl_lands_on_every_macro_for_a_reachable_target():
    target = {"kcal": 700.0, "protein_g": 50.0, "carb_g": 70.0, "fat_g": 23.0}
    bowl = kit.solve_bowl(target, _CHICKEN, _RICE, _BROCCOLI, _OLIVE_OIL)
    assert bowl["status"] == "ok"
    assert bowl["macros_on_target"]
    assert bowl["blocked_by"] == []
    for key in ("protein_g", "carb_g", "fat_g"):
        assert abs(bowl[key] - target[key]) <= 0.1 * target[key], (key, bowl)
    assert abs(bowl["kcal"] - target["kcal"]) <= 0.05 * target["kcal"]
    assert {i["name"] for i in bowl["items"]} == {
        "chicken breast", "white rice", "broccoli", "olive oil"}


def test_solve_bowl_reports_the_residual_when_a_band_blocks_the_target():
    """An unreachable protein target must come back named and quantified,
    not quietly clipped — same contract as the menu optimizer's under-days."""
    target = {"kcal": 900.0, "protein_g": 150.0, "carb_g": 40.0, "fat_g": 20.0}
    bowl = kit.solve_bowl(target, _CHICKEN, _RICE, _BROCCOLI, _OLIVE_OIL)
    assert "chicken breast" in bowl["blocked_by"]
    assert bowl["macros_on_target"] is False
    assert bowl["residual"]["protein_g"] < -20
    assert bowl["grams_by_role"]["protein"] == _CHICKEN["max_g"]


def test_solve_bowl_drops_the_carb_base_when_there_is_no_room_for_it():
    """At a small calorie target the honest answer is no carb base, not a
    forced minimum serving that overshoots carbs and starves protein.

    300 g of vegetables already carries more carbohydrate than this target
    allows, so a minimum serving of rice on top could only be paid for out
    of the protein — which is the macro the strategy set first.
    """
    target = {"kcal": 380.0, "protein_g": 50.0, "carb_g": 12.0, "fat_g": 8.0}
    bowl = kit.solve_bowl(target, _CHICKEN, _RICE, _BROCCOLI, _OLIVE_OIL)
    assert bowl["grams_by_role"]["carb_base"] == 0
    assert "white rice" not in {i["name"] for i in bowl["items"]}
    assert bowl["status"] == "ok"  # calories still land
    assert bowl["protein_g"] >= target["protein_g"]  # protein not sacrificed
    # ...and the carbohydrate overshoot the vegetables force is reported,
    # not hidden.
    assert bowl["residual"]["carb_g"] > 0
    assert bowl["macros_on_target"] is False


def test_solve_bowl_drops_the_fat_serving_when_the_protein_covers_it():
    target = {"kcal": 700.0, "protein_g": 60.0, "carb_g": 80.0, "fat_g": 7.0}
    bowl = kit.solve_bowl(target, _CHICKEN, _RICE, _BROCCOLI, _OLIVE_OIL)
    assert bowl["grams_by_role"]["fat"] == 0


def test_solve_bowl_anchors_vegetables_rather_than_solving_them():
    target = {"kcal": 700.0, "protein_g": 50.0, "carb_g": 70.0, "fat_g": 23.0}
    bowl = kit.solve_bowl(target, _CHICKEN, _RICE, _BROCCOLI, _OLIVE_OIL)
    assert bowl["grams_by_role"]["veg_volume"] == round(kit.VEG_ANCHOR_G)
