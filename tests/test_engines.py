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
    one = [optimizer.MenuPick(1, "A", "book", ["chicken"], [], 1.0, "")]
    assert optimizer.compute_overlap_score(one) == 0


def _candidate(id_, title, book, ingredients, kcal=500):
    return {"id": id_, "title": title, "book": book, "ingredients": ingredients,
            "macros": {"calories": kcal}}


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


def test_select_menu_never_starves_pool_when_all_are_desserts():
    candidates = [_candidate(i, f"Cake {i}", f"Book{i}", ["flour", "sugar"]) for i in range(3)]
    result = optimizer.select_menu(candidates, days=3, pantry=set(), target_kcal=500)
    assert len(result["picks"]) == 3
