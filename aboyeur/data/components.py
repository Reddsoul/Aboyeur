"""Macro Kit component library — hand-curated whole foods, by column.

A Macro Kit is a mix-and-match grocery template: a few foods per column
(protein / carb base / volume veg / fat / sauce ...) plus a per-meal gram
rule, so a member hits their macros by assembling a bowl instead of
following a recipe. These are the foods it can pick from.

Why hand-curated and not corpus-derived: the recipe corpus is Victorian /
Edwardian cookbook prose. "Take a nice fat fowl" does not resolve to a
per-100 g macro row, and no amount of parsing makes it one. Whole-food
names, on the other hand, resolve through the existing USDA pipeline
(engines/nutrition.lookup_ingredient_macros) at high confidence.

Row shape:

    name              canonical, lowercase, singular — user-facing label
    role              one of ROLES
    usda_query        what actually gets sent to USDA (raw form preferred)
    default_serving_g one normal serving, RAW / as-purchased
    min_g / max_g     the band the portion solver may move within
    diet_ok           every diet mode under which this food is allowed
    tags              free-form; used for notes/filtering, not for math
    macros_override   optional per-100 g {kcal, protein_g, carb_g, fat_g}

RAW / as-purchased weights throughout — dry rice, dry oats, raw chicken.
That matches what `lookup_ingredient_macros` already prefers (it ranks a
"raw" USDA description above a prepared one) and what a person actually
buys and weighs. No cooked-weight conversions anywhere in the kit path.

macros_override is for foods where a USDA search is pointless or wrong,
not a general escape hatch: leafy greens and condiments (search noise, and
the answer is ~0 either way), packaged items with no meaningful Foundation
entry (konjac noodles, sugar-free syrup, light mayo, whey powder). Every
override is still run through the same per-100 g sanity gate USDA hits get
(engines/nutrition.validated_per_100g), so a typo fails loudly.
"""

from __future__ import annotations

ROLES = (
    "protein",
    "dairy_protein",
    "carb_base",
    "veg_volume",
    "fruit",
    "fat",
    "sauce",
)

DIET_MODES = ("omnivore", "pescatarian", "vegetarian", "vegan")

# Shorthand for the diet_ok column. A food is offered when the member's
# effective diet is in its list, so each food carries every diet it's
# allowed under, not just the strictest one.
_ALL = ["omnivore", "pescatarian", "vegetarian", "vegan"]
_VEGETARIAN = ["omnivore", "pescatarian", "vegetarian"]
_PESCATARIAN = ["omnivore", "pescatarian"]
_OMNIVORE = ["omnivore"]


COMPONENTS: list[dict] = [
    # -----------------------------------------------------------------
    # protein — the bowl's anchor. Order matters: the kit shortlists by
    # (confidence, list order), so the leading entries are the ones a
    # first-pass kit will actually hand someone.
    # -----------------------------------------------------------------
    {
        "name": "chicken breast", "role": "protein",
        "usda_query": "chicken breast raw",
        "default_serving_g": 150, "min_g": 80, "max_g": 300,
        "diet_ok": _OMNIVORE, "tags": ["lean", "meal-prep"],
        "macros_override": None,
    },
    {
        "name": "turkey breast", "role": "protein",
        "usda_query": "turkey breast raw",
        "default_serving_g": 150, "min_g": 80, "max_g": 300,
        "diet_ok": _OMNIVORE, "tags": ["lean"],
        "macros_override": None,
    },
    {
        "name": "lean ground beef", "role": "protein",
        "usda_query": "ground beef 93% lean raw",
        "default_serving_g": 130, "min_g": 80, "max_g": 250,
        "diet_ok": _OMNIVORE, "tags": ["iron"],
        # SR "Beef, ground, 93% lean meat / 7% fat, raw" — the cleaner strips
        # "ground", "lean" and "raw", leaving "beef", which matches a much
        # fattier cut.
        "macros_override": {"kcal": 152, "protein_g": 20.8, "carb_g": 0.0, "fat_g": 7.0},
    },
    {
        "name": "ground chicken breast", "role": "protein",
        "usda_query": "ground chicken breast raw",
        "default_serving_g": 150, "min_g": 80, "max_g": 300,
        "diet_ok": _OMNIVORE, "tags": ["lean", "fast"],
        "macros_override": None,
    },
    {
        "name": "pork tenderloin", "role": "protein",
        "usda_query": "pork tenderloin raw",
        "default_serving_g": 150, "min_g": 80, "max_g": 280,
        "diet_ok": _OMNIVORE, "tags": ["lean"],
        "macros_override": None,
    },
    {
        "name": "shrimp", "role": "protein",
        "usda_query": "shrimp raw",
        "default_serving_g": 150, "min_g": 80, "max_g": 300,
        "diet_ok": _PESCATARIAN, "tags": ["lean", "fast"],
        "macros_override": None,
    },
    {
        "name": "canned tuna", "role": "protein",
        "usda_query": "tuna light canned in water drained",
        "default_serving_g": 120, "min_g": 60, "max_g": 250,
        "diet_ok": _PESCATARIAN, "tags": ["lean", "no-cook", "shelf-stable"],
        "macros_override": None,
    },
    {
        "name": "tilapia", "role": "protein",
        "usda_query": "tilapia raw",
        "default_serving_g": 150, "min_g": 80, "max_g": 300,
        "diet_ok": _PESCATARIAN, "tags": ["lean"],
        "macros_override": None,
    },
    {
        "name": "cod", "role": "protein",
        "usda_query": "cod atlantic raw",
        "default_serving_g": 150, "min_g": 80, "max_g": 300,
        "diet_ok": _PESCATARIAN, "tags": ["lean"],
        "macros_override": None,
    },
    {
        "name": "salmon fillet", "role": "protein",
        "usda_query": "salmon atlantic raw",
        "default_serving_g": 140, "min_g": 80, "max_g": 250,
        "diet_ok": _PESCATARIAN, "tags": ["omega-3", "fat-source"],
        "macros_override": None,
    },
    {
        "name": "egg whites", "role": "protein",
        "usda_query": "egg white raw",
        "default_serving_g": 200, "min_g": 100, "max_g": 400,
        "diet_ok": _VEGETARIAN, "tags": ["lean", "high-volume"],
        "macros_override": None,
    },
    {
        "name": "whole eggs", "role": "protein",
        "usda_query": "egg whole raw",
        "default_serving_g": 100, "min_g": 50, "max_g": 200,
        "diet_ok": _VEGETARIAN, "tags": ["fat-source", "breakfast"],
        # SR "Egg, whole, raw, fresh" — the query cleaner drops "whole" and
        # "raw", and a search for "egg" returns the egg-WHITE row (0 g fat),
        # which is the one macro this food is on the list for.
        "macros_override": {"kcal": 143, "protein_g": 12.6, "carb_g": 0.7, "fat_g": 9.5},
    },
    {
        "name": "firm tofu", "role": "protein",
        "usda_query": "tofu firm raw",
        "default_serving_g": 200, "min_g": 100, "max_g": 350,
        "diet_ok": _ALL, "tags": ["plant-protein"],
        "macros_override": None,
    },
    {
        "name": "tempeh", "role": "protein",
        "usda_query": "tempeh",
        "default_serving_g": 120, "min_g": 60, "max_g": 250,
        "diet_ok": _ALL, "tags": ["plant-protein", "fermented"],
        "macros_override": None,
    },
    {
        "name": "edamame", "role": "protein",
        "usda_query": "edamame frozen unprepared",
        "default_serving_g": 150, "min_g": 80, "max_g": 300,
        "diet_ok": _ALL, "tags": ["plant-protein", "fiber"],
        "macros_override": None,
    },
    {
        "name": "red lentils", "role": "protein",
        "usda_query": "lentils raw",
        "default_serving_g": 80, "min_g": 40, "max_g": 160,
        "diet_ok": _ALL, "tags": ["plant-protein", "dry-weight", "fiber"],
        "macros_override": None,
    },
    {
        # Sold prepared, so "as purchased" is the cooked block — USDA has no
        # clean Foundation row for it (the nearest is vital wheat gluten
        # flour, which is 3x as dense and would silently triple the macros).
        "name": "seitan", "role": "protein",
        "usda_query": "seitan wheat gluten",
        "default_serving_g": 120, "min_g": 60, "max_g": 250,
        "diet_ok": _ALL, "tags": ["plant-protein", "gluten"],
        "macros_override": {"kcal": 141, "protein_g": 25.0, "carb_g": 4.0, "fat_g": 2.0},
    },
    {
        "name": "textured vegetable protein", "role": "protein",
        "usda_query": "textured vegetable protein soy",
        "default_serving_g": 40, "min_g": 20, "max_g": 90,
        "diet_ok": _ALL, "tags": ["plant-protein", "dry-weight", "shelf-stable"],
        "macros_override": {"kcal": 327, "protein_g": 51.0, "carb_g": 34.0, "fat_g": 1.2},
    },

    # -----------------------------------------------------------------
    # dairy_protein — protein that doubles as a base/sauce (BWS leans on
    # 0% Greek yogurt for exactly this).
    # -----------------------------------------------------------------
    {
        "name": "0% greek yogurt", "role": "dairy_protein",
        "usda_query": "yogurt greek plain nonfat",
        "default_serving_g": 170, "min_g": 100, "max_g": 400,
        "diet_ok": _VEGETARIAN, "tags": ["lean", "no-cook", "sauce-base"],
        "macros_override": None,
    },
    {
        "name": "low-fat cottage cheese", "role": "dairy_protein",
        "usda_query": "cottage cheese lowfat 1% milkfat",
        "default_serving_g": 150, "min_g": 100, "max_g": 300,
        "diet_ok": _VEGETARIAN, "tags": ["lean", "no-cook"],
        "macros_override": None,
    },
    {
        "name": "skim milk", "role": "dairy_protein",
        "usda_query": "milk nonfat fluid",
        "default_serving_g": 250, "min_g": 100, "max_g": 500,
        "diet_ok": _VEGETARIAN, "tags": ["no-cook"],
        "macros_override": None,
    },
    {
        "name": "part-skim mozzarella", "role": "dairy_protein",
        "usda_query": "mozzarella cheese part skim",
        "default_serving_g": 30, "min_g": 15, "max_g": 60,
        "diet_ok": _VEGETARIAN, "tags": ["flavor"],
        "macros_override": None,
    },
    {
        "name": "whey protein powder", "role": "dairy_protein",
        "usda_query": "whey protein powder",
        "default_serving_g": 30, "min_g": 15, "max_g": 60,
        "diet_ok": _VEGETARIAN, "tags": ["supplement", "convenience"],
        "macros_override": {"kcal": 380, "protein_g": 80.0, "carb_g": 8.0, "fat_g": 3.0},
    },
    {
        "name": "unsweetened soy milk", "role": "dairy_protein",
        "usda_query": "soymilk unsweetened",
        "default_serving_g": 250, "min_g": 100, "max_g": 500,
        "diet_ok": _ALL, "tags": ["plant-protein", "no-cook"],
        "macros_override": None,
    },

    # -----------------------------------------------------------------
    # carb_base — the main carb lever the portion solver moves. Grain and
    # legume weights are DRY; potato/sweet potato are raw.
    #
    # min_g is a genuinely small serving (20 g dry grain), not a comfortable
    # one. The solver clamps into the band, so a flattering floor turns a
    # low-carb target into a carb overshoot instead of into a small portion.
    # -----------------------------------------------------------------
    {
        "name": "white rice", "role": "carb_base",
        "usda_query": "rice white long grain raw",
        "default_serving_g": 80, "min_g": 20, "max_g": 200,
        "diet_ok": _ALL, "tags": ["dry-weight", "endurance", "meal-prep"],
        "macros_override": None,
    },
    {
        "name": "brown rice", "role": "carb_base",
        "usda_query": "rice brown long grain raw",
        "default_serving_g": 80, "min_g": 20, "max_g": 200,
        "diet_ok": _ALL, "tags": ["dry-weight", "fiber"],
        "macros_override": None,
    },
    {
        "name": "rolled oats", "role": "carb_base",
        "usda_query": "oats rolled dry",
        "default_serving_g": 60, "min_g": 20, "max_g": 150,
        "diet_ok": _ALL, "tags": ["dry-weight", "breakfast", "fiber"],
        "macros_override": None,
    },
    {
        "name": "potato", "role": "carb_base",
        "usda_query": "potatoes raw flesh and skin",
        "default_serving_g": 300, "min_g": 60, "max_g": 700,
        "diet_ok": _ALL, "tags": ["high-satiety", "whole-food"],
        "macros_override": None,
    },
    {
        "name": "sweet potato", "role": "carb_base",
        "usda_query": "sweet potato raw",
        "default_serving_g": 250, "min_g": 60, "max_g": 600,
        "diet_ok": _ALL, "tags": ["high-satiety", "endurance"],
        # SR "Sweet potato, raw, unprepared" — search returns a leaf/greens row
        # at half the carbohydrate.
        "macros_override": {"kcal": 86, "protein_g": 1.6, "carb_g": 20.1, "fat_g": 0.1},
    },
    {
        "name": "pasta", "role": "carb_base",
        "usda_query": "spaghetti dry enriched",
        "default_serving_g": 80, "min_g": 20, "max_g": 180,
        "diet_ok": _ALL, "tags": ["dry-weight", "endurance", "gluten"],
        "macros_override": None,
    },
    {
        "name": "whole wheat pasta", "role": "carb_base",
        "usda_query": "spaghetti whole wheat dry",
        "default_serving_g": 80, "min_g": 20, "max_g": 180,
        "diet_ok": _ALL, "tags": ["dry-weight", "fiber", "gluten"],
        # SR "Spaghetti, whole-wheat, dry" — "whole" and "dry" are stripped, so
        # this resolved to the same row as plain pasta.
        "macros_override": {"kcal": 348, "protein_g": 14.6, "carb_g": 74.7, "fat_g": 1.4},
    },
    {
        "name": "quinoa", "role": "carb_base",
        "usda_query": "quinoa uncooked",
        "default_serving_g": 70, "min_g": 20, "max_g": 150,
        "diet_ok": _ALL, "tags": ["dry-weight", "plant-protein"],
        # SR "Quinoa, uncooked" — "uncooked" is stripped as a descriptor and the
        # search then ranks "Quinoa, cooked" first, which is a third of the
        # density and wrong against a dry-weight band.
        "macros_override": {"kcal": 368, "protein_g": 14.1, "carb_g": 64.2, "fat_g": 6.1},
    },
    {
        "name": "couscous", "role": "carb_base",
        "usda_query": "couscous dry",
        "default_serving_g": 70, "min_g": 20, "max_g": 150,
        "diet_ok": _ALL, "tags": ["dry-weight", "fast", "gluten"],
        "macros_override": None,
    },
    {
        "name": "bulgur", "role": "carb_base",
        "usda_query": "bulgur dry",
        "default_serving_g": 70, "min_g": 20, "max_g": 150,
        "diet_ok": _ALL, "tags": ["dry-weight", "fiber", "gluten"],
        # SR "Bulgur, dry" — resolved to the couscous row without this.
        "macros_override": {"kcal": 342, "protein_g": 12.3, "carb_g": 75.9, "fat_g": 1.3},
    },
    {
        "name": "rice noodles", "role": "carb_base",
        "usda_query": "rice noodles dry",
        "default_serving_g": 80, "min_g": 20, "max_g": 180,
        "diet_ok": _ALL, "tags": ["dry-weight", "gluten-free"],
        "macros_override": None,
    },
    {
        "name": "cornmeal", "role": "carb_base",
        "usda_query": "cornmeal degermed enriched yellow",
        "default_serving_g": 70, "min_g": 20, "max_g": 150,
        "diet_ok": _ALL, "tags": ["dry-weight", "gluten-free"],
        "macros_override": None,
    },
    {
        "name": "flour tortilla", "role": "carb_base",
        "usda_query": "tortillas ready to bake flour",
        "default_serving_g": 90, "min_g": 30, "max_g": 180,
        "diet_ok": _ALL, "tags": ["gluten", "fast"],
        "macros_override": None,
    },
    {
        "name": "corn tortilla", "role": "carb_base",
        "usda_query": "tortillas ready to bake corn",
        "default_serving_g": 90, "min_g": 30, "max_g": 180,
        "diet_ok": _ALL, "tags": ["gluten-free", "fast"],
        "macros_override": None,
    },
    {
        "name": "whole wheat bread", "role": "carb_base",
        "usda_query": "bread whole wheat",
        "default_serving_g": 80, "min_g": 20, "max_g": 160,
        "diet_ok": _ALL, "tags": ["gluten", "fast", "fiber"],
        "macros_override": None,
    },
    {
        "name": "sourdough bread", "role": "carb_base",
        "usda_query": "bread french or vienna",
        "default_serving_g": 80, "min_g": 20, "max_g": 160,
        "diet_ok": _ALL, "tags": ["gluten", "fast"],
        # SR "Bread, french or vienna (includes sourdough)".
        "macros_override": {"kcal": 272, "protein_g": 10.8, "carb_g": 51.9, "fat_g": 2.4},
    },
    {
        "name": "bagel", "role": "carb_base",
        "usda_query": "bagels plain enriched",
        "default_serving_g": 95, "min_g": 25, "max_g": 190,
        "diet_ok": _ALL, "tags": ["gluten", "endurance", "fast"],
        "macros_override": None,
    },
    {
        "name": "chickpeas", "role": "carb_base",
        "usda_query": "chickpeas garbanzo beans dry raw",
        "default_serving_g": 70, "min_g": 20, "max_g": 150,
        "diet_ok": _ALL, "tags": ["dry-weight", "plant-protein", "fiber"],
        "macros_override": None,
    },
    {
        "name": "black beans", "role": "carb_base",
        "usda_query": "black beans mature seeds raw",
        "default_serving_g": 70, "min_g": 20, "max_g": 150,
        "diet_ok": _ALL, "tags": ["dry-weight", "plant-protein", "fiber"],
        "macros_override": None,
    },
    {
        "name": "honey", "role": "carb_base",
        "usda_query": "honey",
        "default_serving_g": 30, "min_g": 5, "max_g": 80,
        "diet_ok": _VEGETARIAN, "tags": ["fast-carb", "endurance", "condiment"],
        "macros_override": None,
    },
    {
        # Near-calorie-free noodle. USDA has no usable entry; this is the
        # label value for a standard drained 200 g bag scaled to 100 g.
        "name": "konjac noodles", "role": "carb_base",
        "usda_query": "konjac shirataki noodles",
        "default_serving_g": 200, "min_g": 50, "max_g": 400,
        "diet_ok": _ALL, "tags": ["ultra-low-calorie", "cut", "high-volume"],
        "macros_override": {"kcal": 8, "protein_g": 0.1, "carb_g": 3.0, "fat_g": 0.0},
    },

    # -----------------------------------------------------------------
    # veg_volume — the "fill the bowl" column. Anchored at a fixed weight
    # per meal, so these are picked for volume per calorie.
    # -----------------------------------------------------------------
    {
        "name": "romaine lettuce", "role": "veg_volume",
        "usda_query": "lettuce cos or romaine raw",
        "default_serving_g": 250, "min_g": 100, "max_g": 500,
        "diet_ok": _ALL, "tags": ["raw", "high-volume"],
        "macros_override": {"kcal": 17, "protein_g": 1.2, "carb_g": 3.3, "fat_g": 0.3},
    },
    {
        "name": "spinach", "role": "veg_volume",
        "usda_query": "spinach raw",
        "default_serving_g": 200, "min_g": 80, "max_g": 400,
        "diet_ok": _ALL, "tags": ["raw", "high-volume", "iron"],
        "macros_override": {"kcal": 23, "protein_g": 2.9, "carb_g": 3.6, "fat_g": 0.4},
    },
    {
        "name": "cucumber", "role": "veg_volume",
        "usda_query": "cucumber with peel raw",
        "default_serving_g": 300, "min_g": 100, "max_g": 600,
        "diet_ok": _ALL, "tags": ["raw", "high-volume", "no-cook"],
        "macros_override": {"kcal": 15, "protein_g": 0.7, "carb_g": 3.6, "fat_g": 0.1},
    },
    {
        "name": "kale", "role": "veg_volume",
        "usda_query": "kale raw",
        "default_serving_g": 150, "min_g": 60, "max_g": 300,
        "diet_ok": _ALL, "tags": ["high-volume"],
        "macros_override": {"kcal": 35, "protein_g": 2.9, "carb_g": 4.4, "fat_g": 1.5},
    },
    {
        "name": "kimchi", "role": "veg_volume",
        "usda_query": "kimchi cabbage",
        "default_serving_g": 150, "min_g": 50, "max_g": 300,
        "diet_ok": _ALL, "tags": ["fermented", "flavor", "no-cook"],
        "macros_override": {"kcal": 15, "protein_g": 1.1, "carb_g": 2.4, "fat_g": 0.5},
    },
    {
        "name": "broccoli", "role": "veg_volume",
        "usda_query": "broccoli raw",
        "default_serving_g": 250, "min_g": 100, "max_g": 500,
        "diet_ok": _ALL, "tags": ["high-volume", "fiber"],
        "macros_override": None,
    },
    {
        "name": "cauliflower", "role": "veg_volume",
        "usda_query": "cauliflower raw",
        "default_serving_g": 250, "min_g": 100, "max_g": 500,
        "diet_ok": _ALL, "tags": ["high-volume", "fiber"],
        "macros_override": None,
    },
    {
        "name": "cabbage", "role": "veg_volume",
        "usda_query": "cabbage raw",
        "default_serving_g": 250, "min_g": 100, "max_g": 500,
        "diet_ok": _ALL, "tags": ["high-volume", "cheap"],
        "macros_override": None,
    },
    {
        "name": "zucchini", "role": "veg_volume",
        "usda_query": "squash summer zucchini raw",
        "default_serving_g": 250, "min_g": 100, "max_g": 500,
        "diet_ok": _ALL, "tags": ["high-volume"],
        "macros_override": None,
    },
    {
        "name": "mushrooms", "role": "veg_volume",
        "usda_query": "mushrooms white raw",
        "default_serving_g": 200, "min_g": 80, "max_g": 400,
        "diet_ok": _ALL, "tags": ["umami", "high-volume"],
        "macros_override": None,
    },
    {
        "name": "bell pepper", "role": "veg_volume",
        "usda_query": "peppers sweet red raw",
        "default_serving_g": 200, "min_g": 80, "max_g": 400,
        "diet_ok": _ALL, "tags": ["raw", "vitamin-c"],
        "macros_override": None,
    },
    {
        "name": "tomato", "role": "veg_volume",
        "usda_query": "tomatoes red ripe raw",
        "default_serving_g": 200, "min_g": 80, "max_g": 400,
        "diet_ok": _ALL, "tags": ["raw", "no-cook"],
        "macros_override": None,
    },
    {
        "name": "green beans", "role": "veg_volume",
        "usda_query": "green snap beans raw",
        "default_serving_g": 200, "min_g": 80, "max_g": 400,
        "diet_ok": _ALL, "tags": ["fiber"],
        "macros_override": None,
    },
    {
        "name": "asparagus", "role": "veg_volume",
        "usda_query": "asparagus raw",
        "default_serving_g": 200, "min_g": 80, "max_g": 400,
        "diet_ok": _ALL, "tags": ["fiber"],
        "macros_override": None,
    },
    {
        "name": "brussels sprouts", "role": "veg_volume",
        "usda_query": "brussels sprouts raw",
        "default_serving_g": 200, "min_g": 80, "max_g": 400,
        "diet_ok": _ALL, "tags": ["fiber"],
        "macros_override": None,
    },
    {
        "name": "carrots", "role": "veg_volume",
        "usda_query": "carrots raw",
        "default_serving_g": 200, "min_g": 80, "max_g": 400,
        "diet_ok": _ALL, "tags": ["raw", "cheap", "sweet"],
        "macros_override": None,
    },
    {
        "name": "pumpkin", "role": "veg_volume",
        "usda_query": "pumpkin raw",
        "default_serving_g": 250, "min_g": 100, "max_g": 500,
        "diet_ok": _ALL, "tags": ["high-volume", "sweet"],
        "macros_override": None,
    },
    {
        "name": "butternut squash", "role": "veg_volume",
        "usda_query": "squash winter butternut raw",
        "default_serving_g": 250, "min_g": 100, "max_g": 500,
        "diet_ok": _ALL, "tags": ["sweet"],
        "macros_override": None,
    },

    # -----------------------------------------------------------------
    # fruit — optional in the bowl (solved at 0 g by default), on the
    # shopping list as a snack/topping.
    # -----------------------------------------------------------------
    {
        "name": "strawberries", "role": "fruit",
        "usda_query": "strawberries raw",
        "default_serving_g": 200, "min_g": 80, "max_g": 500,
        "diet_ok": _ALL, "tags": ["high-volume", "no-cook"],
        "macros_override": None,
    },
    {
        "name": "banana", "role": "fruit",
        "usda_query": "bananas raw",
        "default_serving_g": 120, "min_g": 60, "max_g": 300,
        "diet_ok": _ALL, "tags": ["endurance", "fast-carb", "potassium"],
        "macros_override": None,
    },
    {
        "name": "watermelon", "role": "fruit",
        "usda_query": "watermelon raw",
        "default_serving_g": 300, "min_g": 100, "max_g": 600,
        "diet_ok": _ALL, "tags": ["high-volume", "no-cook"],
        "macros_override": None,
    },
    {
        "name": "cantaloupe", "role": "fruit",
        "usda_query": "melons cantaloupe raw",
        "default_serving_g": 250, "min_g": 100, "max_g": 500,
        "diet_ok": _ALL, "tags": ["high-volume", "no-cook"],
        "macros_override": None,
    },
    {
        "name": "blueberries", "role": "fruit",
        "usda_query": "blueberries raw",
        "default_serving_g": 150, "min_g": 60, "max_g": 300,
        "diet_ok": _ALL, "tags": ["no-cook"],
        "macros_override": None,
    },
    {
        "name": "apple", "role": "fruit",
        "usda_query": "apples raw with skin",
        "default_serving_g": 180, "min_g": 80, "max_g": 400,
        "diet_ok": _ALL, "tags": ["fiber", "no-cook", "portable"],
        # SR "Apples, raw, with skin".
        "macros_override": {"kcal": 52, "protein_g": 0.3, "carb_g": 13.8, "fat_g": 0.2},
    },
    {
        "name": "orange", "role": "fruit",
        "usda_query": "oranges raw",
        "default_serving_g": 180, "min_g": 80, "max_g": 400,
        "diet_ok": _ALL, "tags": ["vitamin-c", "no-cook", "portable"],
        # SR "Oranges, raw, all commercial varieties" — search returns orange
        # PEEL (97 kcal, 25 g carb).
        "macros_override": {"kcal": 47, "protein_g": 0.9, "carb_g": 11.8, "fat_g": 0.1},
    },
    {
        "name": "medjool dates", "role": "fruit",
        "usda_query": "dates medjool",
        "default_serving_g": 50, "min_g": 20, "max_g": 120,
        "diet_ok": _ALL, "tags": ["endurance", "fast-carb", "portable"],
        "macros_override": None,
    },

    # -----------------------------------------------------------------
    # fat — one serving per meal (BWS "pick 1"). Small bands: these are
    # the densest thing in the bowl and the easiest way to blow a target.
    # -----------------------------------------------------------------
    {
        "name": "olive oil", "role": "fat",
        "usda_query": "olive oil",
        "default_serving_g": 14, "min_g": 5, "max_g": 30,
        "diet_ok": _ALL, "tags": ["cooking"],
        "macros_override": None,
    },
    {
        "name": "avocado", "role": "fat",
        "usda_query": "avocados raw",
        "default_serving_g": 100, "min_g": 40, "max_g": 200,
        "diet_ok": _ALL, "tags": ["whole-food", "fiber", "no-cook"],
        "macros_override": None,
    },
    {
        "name": "almonds", "role": "fat",
        "usda_query": "almonds raw",
        "default_serving_g": 25, "min_g": 10, "max_g": 60,
        "diet_ok": _ALL, "tags": ["whole-food", "portable"],
        "macros_override": None,
    },
    {
        "name": "walnuts", "role": "fat",
        "usda_query": "walnuts english raw",
        "default_serving_g": 25, "min_g": 10, "max_g": 60,
        "diet_ok": _ALL, "tags": ["omega-3", "portable"],
        "macros_override": None,
    },
    {
        "name": "peanut butter", "role": "fat",
        "usda_query": "peanut butter smooth",
        "default_serving_g": 30, "min_g": 10, "max_g": 60,
        "diet_ok": _ALL, "tags": ["no-cook"],
        # SR "Peanut butter, smooth style, without salt" — search returns the
        # REDUCED-FAT product (34 g fat, 36 g carb).
        "macros_override": {"kcal": 598, "protein_g": 22.2, "carb_g": 22.3, "fat_g": 51.4},
    },
    {
        "name": "tahini", "role": "fat",
        "usda_query": "sesame butter tahini",
        "default_serving_g": 25, "min_g": 10, "max_g": 50,
        "diet_ok": _ALL, "tags": ["sauce-base"],
        "macros_override": None,
    },
    {
        "name": "chia seeds", "role": "fat",
        "usda_query": "chia seeds dried",
        "default_serving_g": 25, "min_g": 10, "max_g": 50,
        "diet_ok": _ALL, "tags": ["fiber", "omega-3"],
        "macros_override": None,
    },
    {
        "name": "olives", "role": "fat",
        "usda_query": "olives ripe canned",
        "default_serving_g": 40, "min_g": 15, "max_g": 80,
        "diet_ok": _ALL, "tags": ["no-cook", "flavor"],
        # SR "Olives, ripe, canned (small-extra large)" — the search matches
        # "Bananas, ripe and slightly ripe, raw" on the shared word "ripe",
        # and the raw-form preference then picks it. A fat component with
        # 0 g fat is worse than no component.
        "macros_override": {"kcal": 116, "protein_g": 0.8, "carb_g": 6.0, "fat_g": 10.9},
    },
    {
        "name": "light mayonnaise", "role": "fat",
        "usda_query": "mayonnaise light",
        "default_serving_g": 15, "min_g": 5, "max_g": 45,
        "diet_ok": _VEGETARIAN, "tags": ["sauce-base", "no-cook"],
        "macros_override": {"kcal": 233, "protein_g": 0.4, "carb_g": 3.3, "fat_g": 23.3},
    },
    {
        "name": "light cream cheese", "role": "fat",
        "usda_query": "cream cheese light",
        "default_serving_g": 30, "min_g": 10, "max_g": 60,
        "diet_ok": _VEGETARIAN, "tags": ["sauce-base", "no-cook"],
        "macros_override": {"kcal": 201, "protein_g": 7.9, "carb_g": 8.2, "fat_g": 15.3},
    },

    # -----------------------------------------------------------------
    # sauce — flavour, effectively free on the macro budget. All curated:
    # a USDA search for "hot sauce" returns branded noise and the honest
    # answer is ~0 either way.
    # -----------------------------------------------------------------
    {
        "name": "spices and herbs", "role": "sauce",
        "usda_query": "spices dried",
        "default_serving_g": 5, "min_g": 1, "max_g": 15,
        "diet_ok": _ALL, "tags": ["free", "seasoning"],
        "macros_override": {"kcal": 0, "protein_g": 0.0, "carb_g": 0.0, "fat_g": 0.0},
    },
    {
        "name": "hot sauce", "role": "sauce",
        "usda_query": "hot sauce",
        "default_serving_g": 15, "min_g": 5, "max_g": 40,
        "diet_ok": _ALL, "tags": ["free", "no-cook"],
        "macros_override": {"kcal": 12, "protein_g": 0.5, "carb_g": 1.8, "fat_g": 0.4},
    },
    {
        "name": "soy sauce", "role": "sauce",
        "usda_query": "soy sauce",
        "default_serving_g": 15, "min_g": 5, "max_g": 45,
        "diet_ok": _ALL, "tags": ["free", "umami", "soy"],
        "macros_override": {"kcal": 53, "protein_g": 8.1, "carb_g": 4.9, "fat_g": 0.6},
    },
    {
        "name": "sriracha", "role": "sauce",
        "usda_query": "sriracha chili sauce",
        "default_serving_g": 10, "min_g": 5, "max_g": 30,
        "diet_ok": _ALL, "tags": ["free", "no-cook"],
        "macros_override": {"kcal": 93, "protein_g": 1.9, "carb_g": 19.2, "fat_g": 0.9},
    },
    {
        "name": "mustard", "role": "sauce",
        "usda_query": "mustard prepared yellow",
        "default_serving_g": 10, "min_g": 5, "max_g": 30,
        "diet_ok": _ALL, "tags": ["free", "no-cook"],
        "macros_override": {"kcal": 66, "protein_g": 4.4, "carb_g": 5.8, "fat_g": 3.3},
    },
    {
        "name": "salsa", "role": "sauce",
        "usda_query": "salsa ready to serve",
        "default_serving_g": 40, "min_g": 15, "max_g": 100,
        "diet_ok": _ALL, "tags": ["free", "no-cook"],
        "macros_override": {"kcal": 29, "protein_g": 1.5, "carb_g": 6.6, "fat_g": 0.2},
    },
    {
        "name": "fish sauce", "role": "sauce",
        "usda_query": "fish sauce",
        "default_serving_g": 10, "min_g": 5, "max_g": 30,
        "diet_ok": _PESCATARIAN, "tags": ["free", "umami"],
        "macros_override": {"kcal": 35, "protein_g": 5.1, "carb_g": 3.6, "fat_g": 0.0},
    },
    {
        "name": "fat-free tomato sauce", "role": "sauce",
        "usda_query": "tomato sauce canned",
        "default_serving_g": 60, "min_g": 20, "max_g": 150,
        "diet_ok": _ALL, "tags": ["free"],
        "macros_override": {"kcal": 29, "protein_g": 1.3, "carb_g": 6.6, "fat_g": 0.2},
    },
    {
        "name": "lemon juice", "role": "sauce",
        "usda_query": "lemon juice raw",
        "default_serving_g": 15, "min_g": 5, "max_g": 45,
        "diet_ok": _ALL, "tags": ["free", "no-cook"],
        "macros_override": {"kcal": 22, "protein_g": 0.4, "carb_g": 5.0, "fat_g": 0.2},
    },
    {
        "name": "balsamic vinegar", "role": "sauce",
        "usda_query": "vinegar balsamic",
        "default_serving_g": 15, "min_g": 5, "max_g": 45,
        "diet_ok": _ALL, "tags": ["free", "no-cook"],
        "macros_override": {"kcal": 88, "protein_g": 0.5, "carb_g": 17.0, "fat_g": 0.0},
    },
    {
        "name": "sugar-free syrup", "role": "sauce",
        "usda_query": "syrup sugar free",
        "default_serving_g": 20, "min_g": 5, "max_g": 60,
        "diet_ok": _ALL, "tags": ["free", "sweet"],
        "macros_override": {"kcal": 0, "protein_g": 0.0, "carb_g": 0.0, "fat_g": 0.0},
    },
]


def by_role(role: str) -> list[dict]:
    """Every component in one column, in curation order."""
    return [c for c in COMPONENTS if c["role"] == role]
