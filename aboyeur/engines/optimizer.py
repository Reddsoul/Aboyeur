"""Menu optimizer — picks WHICH dishes go in a week's plan.

Two stages:

1. Greedy dish selection, scoring each candidate against what's already
   chosen:  w1*ingredient_overlap + w2*pantry_overlap + w3*macro_fit
            - w4*same_book_penalty - w5*protein_repeat_penalty
   (overlap-only fallback when no dish has resolved nutrition data).

2. Per-day nutrient fit (_fill_daily_kcal): add top-up dishes for calories,
   swap dishes that wreck the day's macro SHAPE (_repair_day_macros), then
   hand off to engines/day_solver.solve_day_portions, which sets each dish's
   serving multiplier so the DAY lands on kcal AND protein/carb/fat/fiber.
   A day that still can't clear every macro makes select_menu return
   feasible=False, and plan_menu then refuses rather than ship a bad plan.

"Protein present (unless vegetarian mode)" is a heavy per-pick penalty, not a
hard filter — a hard filter can starve a genuinely vegetable-forward pool.
"Dessert / drink / bare component as a meal" IS a hard filter
(is_menu_ineligible): dropping those never empties a real recipe pool, and
the classified `course` column (aboyeur/classify_recipes.py) is the strong
signal, with the title/ingredient keyword lists as the backstop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from aboyeur.engines.nutrition import fold_plural

W_INGREDIENT_OVERLAP = 1.0
W_PANTRY_OVERLAP = 2.0
W_MACRO_FIT = 3.0
# Book spread and protein variety are tie-breakers, not objectives — meeting
# the nutrients is the first goal and a week that repeats a protein or leans
# on one book is fine if that is what hits the targets with the fewest
# groceries. Kept small on purpose (was 2.0 / 1.5, which actively fought
# adequacy).
W_SAME_BOOK_PENALTY = 0.4
W_PROTEIN_REPEAT_PENALTY = 0.4
# Slot 1 of a multi-meal day wants a breakfast-course dish; every later slot
# in that day deprioritizes one (a classified breakfast dish CAN still fill
# a dinner slot if nothing better exists — this is a scoring nudge, not a
# hard filter, same reasoning as the missing-protein penalty above). Only
# applies when meals_per_day > 1: a single-meal (OMAD) plan has no "slot 1"
# to distinguish. See feature-spec-2026-09-26.md #2.
BREAKFAST_SLOT_BONUS = 1.5
BREAKFAST_OFFSLOT_PENALTY = 1.5
PROTEIN_MISSING_PENALTY = 2.5
# A dish with no nutrition data (kcal 0) can't be graded or portion-solved;
# when the pool has dishes WITH data, prefer those. Uniform across an
# all-unknown pool, so it never changes the order there.
UNKNOWN_NUTRITION_PENALTY = 2.0
CONSTRAINT_NUDGE = 3.0

# Variety (profile 0-100, default 50) sets the plan-wide overlap floor and
# how many meal slots may repeat a dish already on the plan:
#
#   0    the same dishes every day — one per meal slot, repeated
#   25   floor 65: dishes repeat as needed to share 60-80% of ingredients
#   50   floor 50
#   75   floor 25: few repeats, 20-40% sharing is fine
#   100  floor 0, no repeats, nothing pulls picks toward sharing
#
# If the first greedy pass lands under the floor, selection is retried with
# the overlap weight doubled (then quadrupled, ...) and the best attempt
# wins — trading some macro fit for shared ingredients, which is the whole
# point of planning a week at once. Only when unique dishes still can't
# reach the floor are repeats spent, one at a time, fewest first.
DEFAULT_VARIETY = 50
# Legacy name: the floor a default-variety plan is held to.
MIN_OVERLAP_SCORE = 50
_OVERLAP_ESCALATION = (1.0, 2.0, 4.0, 8.0)


def clamp_variety(variety) -> int:
    try:
        v = int(round(float(variety)))
    except (TypeError, ValueError):
        v = DEFAULT_VARIETY
    return max(0, min(100, v))


def overlap_floor_for(variety) -> int:
    """0->80, 50->50, 100->0, linear in between."""
    v = clamp_variety(variety)
    if v <= 50:
        return round(80 - 30 * v / 50)
    return round(50 - 50 * (v - 50) / 50)


def repeat_budget_for(variety, total_picks: int, meals_per_day: int) -> int:
    """How many of the plan's slots may be a repeat of another slot's dish.
    Day one is always distinct dishes; at variety 0 every later slot repeats."""
    v = clamp_variety(variety)
    if v >= 100:
        return 0
    later = max(0, total_picks - meals_per_day)
    return int(round(later * (1 - v / 100)))

# A day is "on target" when its total kcal is within this fraction of the
# household's daily need. Days under it get a second dish added to a meal
# (max 2 dishes per meal) until they land inside the band.
DAILY_KCAL_TOLERANCE = 0.15
# Protein/carb/fat are looser than kcal — the corpus estimate is noisier per
# macro than in aggregate — but a day is only "ok" when every macro clears
# this, not just calories. A 2400 kcal day that is 380 g carb / 25 g protein
# is not on target however close its calorie count lands.
DAILY_MACRO_TOLERANCE = 0.25
# Absolute-gram backstop on top of the percentage tolerance above (feature-
# spec-2026-09-26.md #3): 25% of a small target (20g fat) is unreasonably
# tight, but 25% of a large one (160g protein = 40g slop) is too loose to
# call "on target". A day fails a macro when it misses by MORE than both —
# the percentage tolerance stays the backstop for small targets, this one
# catches the high-target case the percentage alone lets through. Per DAY
# (summed across meals), matching where day_macro_status already operates —
# per-meal would force the raw-ingredient fallback into constant use.
DAILY_MACRO_TOLERANCE_G = 10.0
MAX_DISHES_PER_MEAL = 2

# A whole meal (every dish in one day+meal slot, after portion scaling) is
# "coffee only" — and its day infeasible — when it lands under BOTH this
# fraction of the per-meal target AND this absolute floor. The absolute floor
# keeps a deliberately uneven day (the solver making one meal bigger to hit
# the day's macros) from being rejected just for having a lighter meal, while
# still catching menu-60's "just 112 kcal of coffee" / "160 kcal of potatoes".
MEAL_MIN_KCAL_FRACTION = 0.45
MEAL_MIN_KCAL_ABS = 350.0

# After top-ups, each day's portions are scaled to close the remaining kcal
# gap. Macros are per-serving, so a 2.1x multiplier means "eat 2.1 servings
# of the dish as written" — independent of how many people the recipe batch
# feeds (a serves-10 recipe's per-serving macros are already 1/10 of the
# batch). Clamped: below 0.75 a portion stops being a real serving, above
# 2.0 the plan should pick bigger dishes instead of doubling-and-a-half a
# garnish ("3 servings of rice" is what a 3.0 ceiling produced).
PORTION_MULT_MIN = 0.75
PORTION_MULT_MAX = 2.0

# Ingredients that appear in nearly every recipe — not meaningful for overlap
# scoring. Using these to drive selection causes same-book/same-staples
# clustering instead of genuinely shared ingredients.
UNIVERSAL_STAPLES = {
    "salt", "pepper", "butter", "flour", "water", "oil", "olive oil", "vegetable oil",
    "sugar", "vinegar", "onion", "garlic", "eggs", "egg", "milk", "cream", "stock", "broth",
    "rice", "bread", "bread crumbs", "breadcrumbs", "bay leaf", "parsley", "thyme",
    "lemon juice", "lemon",
}

# All title-classification matching is phrase/word-boundary based, NOT bare
# substring — "ale" must not fire on "kale", "tea" must not fire on "steak".
# Multi-word entries match as an ordered phrase on word boundaries.
_DESSERT_TITLE_WORDS = {
    "pie", "cake", "cupcake", "shortcake", "pudding", "tart", "tarte", "tartlet",
    "waffle", "cookie", "biscuit", "cracknel", "pastry", "custard", "ice cream",
    "sorbet", "sherbet", "mousse", "brownie", "muffin", "scone", "donut",
    "doughnut", "charlotte", "compote", "strudel", "torte", "flan", "praline",
    "macaroon", "macaron", "meringue", "gingerbread", "ginger snap",
    "gingersnap", "shortbread", "ladyfinger", "eclair", "profiterole",
    "parfait", "sundae", "trifle", "syllabub", "fool", "junket", "fudge",
    "toffee", "nougat", "candy", "caramel", "marshmallow", "fritter", "turnover",
    "cobbler", "crumble", "buckle", "clafoutis", "panna cotta", "gelato",
    "kulfi", "halva", "halwa", "baklava", "cheesecake", "gateau", "petit four",
    "sweetmeat", "confection", "bonbon", "truffle", "divinity", "brittle",
    "popcorn", "cracker jack", "crackerjack", "caramel corn", "kiss", "kisses",
    "sugarplum", "lollipop", "lozenge", "comfit", "sugar candy", "rock candy",
}

_DRINK_TITLE_WORDS = {
    "smoothie", "cocktail", "mocktail", "juice", "milkshake",
    "sangria", "margarita", "daiquiri", "mojito", "lemonade", "limeade",
    "latte", "cappuccino", "espresso", "coffee", "cafe au lait", "cafe noir",
    "tea", "iced tea", "chai", "cocoa", "hot chocolate", "chocolate milk",
    "bubble tea", "spritzer", "frappe", "hot toddy", "toddy",
    "eggnog", "julep", "punch", "cordial", "shrub", "negus", "posset",
    "sangaree", "wassail", "grog", "highball",
    "spritz", "aperitif", "digestif", "liqueur",
    "kvass", "horchata", "lassi",
    "kefir", "kombucha", "tisane", "beverage",
}

# Not a meal: served with or on something else. Sauces, dressings, fillings,
# icings, spreads, condiments, garnishes, pastry doughs, spice blends.
_COMPONENT_TITLE_WORDS = {
    "sauce", "gravy", "dressing", "vinaigrette", "filling", "frosting",
    "icing", "glaze", "marinade", "rub", "spice mix", "spice blend",
    "seasoning", "stuffing", "forcemeat", "relish", "chutney", "pickle",
    "preserve", "jam", "jelly", "marmalade", "conserve", "compound butter",
    "flavored butter", "spread", "dip", "coulis", "reduction", "syrup",
    "simple syrup", "pastry cream", "pie crust", "pie shell", "pastry dough",
    "pie dough", "puff paste", "batter", "roux", "custard sauce", "hard sauce",
    "caramel sauce", "curd", "topping", "sprinkle", "dusting", "garnish",
    "croutons", "breadcrumbs", "bread crumbs", "seasoned crumbs",
}

# Ingredient names that mark a "recipe" as essentially confectionery — if a
# short ingredient list is dominated by these and carries no protein/veg, the
# dish is a component/sweet, not a meal.
_CONFECTIONERY_INGREDIENTS = {
    "sugar", "powdered sugar", "confectioners sugar", "icing sugar",
    "brown sugar", "corn syrup", "golden syrup", "molasses", "molass", "honey",
    "maple syrup", "treacle", "glucose", "gelatin", "gelatine", "cornstarch",
    "corn starch", "cocoa", "chocolate", "vanilla", "food coloring",
}

_PROTEIN_WORDS = {
    "chicken", "beef", "pork", "lamb", "fish", "salmon", "shrimp", "tofu",
    "egg", "eggs", "cheese", "lentil", "chickpea", "turkey", "duck", "crab",
    "tuna", "sausage", "ham", "cod", "clam", "mussel", "lobster", "scallop",
    "squid", "veal", "tempeh", "seitan", "prawn", "steak", "bean", "beans",
}

# One canonical "main protein" per recipe, checked in order, for repeat-penalty tracking.
_MAIN_PROTEINS = [
    "chicken", "beef", "pork", "lamb", "fish", "salmon", "shrimp",
    "tofu", "turkey", "duck", "egg", "lentil", "chickpea",
]


@dataclass
class MenuPick:
    recipe_id: int
    title: str
    book: str
    path: str
    ingredients: list[str]
    shared_ingredients: list[str]
    macro_fit: float
    pick_reason: str
    day: int = 0
    meal: int = 1
    role: str = "main"  # "main" or "top-up" (second dish added to hit daily kcal)
    kcal: float = 0.0
    protein_g: float = 0.0
    carb_g: float = 0.0
    fat_g: float = 0.0
    fiber_g: float = 0.0
    # Servings of the dish to eat (macros above are already scaled by it).
    portion_multiplier: float = 1.0
    # How many servings the recipe as written yields (None if unparsed) —
    # lets the caller phrase "recipe serves 10, cook 1/5 of it" honestly.
    servings: int | None = None


# Savory dishes that contain a dessert keyword ("pie", "cobbler", "roll") —
# checked before the dessert list so a shepherd's pie is a main, not cake.
_SAVORY_OVERRIDE_PHRASES = {
    "shepherd's pie", "shepherds pie", "cottage pie", "pot pie", "meat pie",
    "fish pie", "chicken pie", "turkey pie", "pork pie", "beef pie",
    "steak pie", "game pie", "rabbit pie", "veal pie", "ham pie",
    "tamale pie", "frito pie", "shepherd pie", "chicken pot pie",
    "spaghetti pie", "tomato pie", "onion pie", "spinach pie",
    "cabbage roll", "egg roll", "spring roll", "sausage roll", "meat roll",
    "cheese biscuit", "chicken biscuit",
}
_WORD_RE = re.compile(r"[a-z']+")


def _phrase_hit(text: str, phrases: set[str]) -> bool:
    """True if any phrase appears in `text` on word boundaries — so "ale"
    matches "pale ale" but not "kale", and "ginger snap" matches "Ginger
    Snaps" (words are singular-folded first) but only as the full phrase."""
    words = [fold_plural(w) for w in _WORD_RE.findall(text.lower())]
    if not words:
        return False
    joined = " " + " ".join(words) + " "
    return any(f" {fold_plural(p)} " in joined for p in phrases)


# "Chicken with Mushroom Sauce" is a dish; "Mushroom Sauce" is a component.
# A title that describes a food *accompanied by* something (" with ",
# " in ", " and ", " on ", " over ", " topped ", " smothered ") is a
# composed dish even if the accompaniment is a sauce/juice/wine.
_ACCOMPANIMENT_RE = re.compile(
    r"\b(with|in|on|over|topped|smothered|served|garnished)\b"
)


def _is_accompanied_dish(title: str) -> bool:
    return bool(_ACCOMPANIMENT_RE.search((title or "").lower()))


def is_dessert(title: str) -> bool:
    if _phrase_hit(title, _SAVORY_OVERRIDE_PHRASES):
        return False
    return _phrase_hit(title, _DESSERT_TITLE_WORDS)


def is_drink(title: str) -> bool:
    if _phrase_hit(title, _DRINK_TITLE_WORDS) and not _is_accompanied_dish(title):
        return True
    return False


def is_component(title: str, ingredients: list[str] | None = None) -> bool:
    """A sauce/dressing/filling/condiment/dough — something served with a
    meal, not as one. Title phrases are the strong signal (but "X with Y
    Sauce" is a dish, not a sauce); a very short ingredient list dominated by
    sugar/syrup/thickener with no protein or vegetable is the backstop
    (catches "Orange Filling")."""
    if _phrase_hit(title, _COMPONENT_TITLE_WORDS) and not _is_accompanied_dish(title):
        return True
    ings = ingredients or []
    meaningful = meaningful_ingredients(ings)
    if 0 < len(meaningful) <= 3 and not has_protein(ings):
        folded = {fold_plural(i) for i in ings}
        confectionery = {fold_plural(c) for c in _CONFECTIONERY_INGREDIENTS}
        if meaningful and meaningful <= confectionery:
            return True
    return False


# Courses that can never be a menu meal / can always be one. When a recipe has
# been classified (aboyeur/classify_recipes.py), that verdict wins over the
# keyword heuristics below.
_INELIGIBLE_COURSES = {"dessert", "drink", "component"}
_MEAL_COURSES = {"main", "side", "breakfast"}


def is_menu_ineligible(
    title: str, ingredients: list[str] | None = None, course: str | None = None
) -> bool:
    """Dessert, drink, or bare component — never a meal on its own. Uses the
    classified `course` when present, else the title/ingredient heuristics."""
    c = (course or "").strip().lower()
    if c in _INELIGIBLE_COURSES:
        return True
    if c in _MEAL_COURSES:
        return False
    return is_dessert(title) or is_drink(title) or is_component(title, ingredients)


def has_protein(ingredients: list[str]) -> bool:
    joined = " ".join(i.lower() for i in ingredients)
    return any(p in joined for p in _PROTEIN_WORDS)


# A "side" is menu-eligible (not dessert/drink/component) but is not itself a
# dinner — pairing one with an actual main is the point of this distinction
# (see select_menu's main_pool / accompaniment_pool split).
_ACCOMPANIMENT_COURSES = {"side"}


def equipment_blocked(equipment: list[str] | None, tools_available: list[str] | None) -> bool:
    """True if this recipe needs a piece of equipment the client doesn't
    have (feature-spec-2026-09-26.md #5). Opt-out by design, matching how
    diet_modes/allergies treat an empty list: an unclassified recipe
    (equipment is None/empty — aboyeur/classify_equipment.py hasn't run on
    it yet) is never blocked, and a client with no tools_available set at
    all (None) is never filtered either — profile.DEFAULT_PROFILE seeds a
    common-kitchen default rather than leaving new clients at zero tools."""
    if not equipment or tools_available is None:
        return False
    available = {t.strip().lower() for t in tools_available}
    return any((t or "").strip().lower() not in available for t in equipment)


def hinted_meal_types(c: dict, hints: dict[str, list[str]] | None) -> set[str] | None:
    """Per-client meal-type hint override (feature-spec-2026-09-26.md #2) —
    "steak is breakfast for me" overrides the classified course for THIS
    client's slot-fit scoring only; it never mutates recipes.course, so it
    can't leak into another client's plan. Matches a hint key against the
    recipe's title or ingredients on word boundaries; returns the hinted
    meal types (lowercased) on the first hint that fires, else None so the
    caller falls back to the classified course."""
    if not hints:
        return None
    ing_text = " ".join(c.get("ingredients") or [])
    for key, meal_types in hints.items():
        key = (key or "").strip().lower()
        if not key or not meal_types:
            continue
        if _phrase_hit(c.get("title", ""), {key}) or _phrase_hit(ing_text, {key}):
            return {str(m).strip().lower() for m in meal_types}
    return None


def is_meal_main_eligible(candidate: dict) -> bool:
    """True if this candidate can anchor a meal (be the role="main" pick),
    not just accompany one.

    Only a classified 'side' is demoted — dessert/drink/component are already
    dropped from the pool upstream by is_menu_ineligible before this ever
    runs. 'unknown' (unclassified, or a unit-test candidate with no course at
    all) stays eligible: there's no reliable title/ingredient heuristic for
    "is this a side" the way there is for "is this a dessert", and gating on
    has_protein would wrongly demote a genuine meat-free main (a nut roast,
    a bean stew) that just doesn't use one of the tracked protein words."""
    course = (candidate.get("course") or "").strip().lower()
    return course not in _ACCOMPANIMENT_COURSES


def main_protein(ingredients: list[str]) -> str | None:
    joined = " ".join(i.lower() for i in ingredients)
    for p in _MAIN_PROTEINS:
        if p in joined:
            return p
    return None


# Staples folded to singular once, so 'egg'/'eggs' style variants match the
# folded ingredient names below.
_FOLDED_STAPLES = {fold_plural(s) for s in UNIVERSAL_STAPLES}


def meaningful_ingredients(ingredients: list[str]) -> set[str]:
    """Lowercased, plural-folded ingredient names minus universal staples —
    'potatoes' and 'potato' count as the same ingredient for overlap."""
    folded = (fold_plural(i) for i in ingredients)
    return {i for i in folded if i and i not in _FOLDED_STAPLES}


_TITLE_PAREN_RE = re.compile(r"\([^)]*\)")
_TITLE_JUNK_RE = re.compile(r"[^a-z0-9 ]+")


def norm_title(title: str) -> str:
    """Casefold + strip parentheticals/punctuation, for duplicate-dish
    detection: 'VEGETABLE DINNER, EASILY DIGESTED' and 'Vegetable Dinner,
    Easily Digested (Pulmentarium Ad Ventrem)' normalize identically —
    the corpus contains such double-indexed recipes, and a menu must never
    serve the same dish twice under two ids."""
    s = _TITLE_PAREN_RE.sub(" ", (title or "").lower())
    s = _TITLE_JUNK_RE.sub(" ", s)
    return " ".join(s.split())


def macro_fit(macros: dict, target_kcal: float, target_macros: dict | None = None) -> float:
    """0..1, 1 = spot on target. 0.5 (neutral) if there's no target or no estimate.

    When target_macros ({protein_g, carb_g, fat_g, fiber_g}) is given and the
    candidate has macro data, the fit blends calorie closeness (45%) with macro
    closeness (55%) — meeting the nutrients is the first goal, so the macros
    now weigh more than calories, not less.
    """
    kcal = macros.get("calories") or 0
    if not target_kcal or not kcal:
        return 0.5
    fit = max(0.0, 1.0 - abs(kcal - target_kcal) / target_kcal)
    if target_macros:
        macro_fits = []
        for key in ("protein_g", "carb_g", "fat_g", "fiber_g"):
            t = target_macros.get(key) or 0
            v = macros.get(key)
            if t and v is not None:
                macro_fits.append(max(0.0, 1.0 - abs(float(v) - t) / t))
        if macro_fits:
            fit = 0.45 * fit + 0.55 * (sum(macro_fits) / len(macro_fits))
    return fit


def daily_macros_from_per_meal(target_macros: dict | None, meals_per_day: int) -> dict | None:
    """Scale per-meal macro targets up to a whole-day target — day_macro_status
    grades day totals, so it needs day-level targets, not per-meal ones."""
    if not target_macros:
        return None
    m = max(1, int(meals_per_day or 1))
    return {k: float(v or 0) * m for k, v in target_macros.items()}


def day_macro_status(
    totals: dict, target_kcal: float, target_macros: dict | None
) -> tuple[str, str, list[str]]:
    """Grade one day. Returns (kcal_status, macro_status, gap_notes).

    `target_kcal` and `target_macros` are both DAY-level here (use
    daily_macros_from_per_meal to convert a per-meal macro dict first).

    kcal_status: unknown | under | over | ok  (within DAILY_KCAL_TOLERANCE).
    macro_status: unknown | ok | off  — "ok" only when kcal AND every one of
    protein/carb/fat is within tolerance. Calorie-close but protein-short is
    "off", and that is what decides whether a plan is deliverable.

    A macro fails when it's outside BOTH DAILY_MACRO_TOLERANCE (percentage)
    AND DAILY_MACRO_TOLERANCE_G (absolute grams) — the percentage alone is
    too loose at a high target (25% of 160g protein is 40g slop) and the
    absolute figure alone is too tight at a low one (10g on a 20g fat
    target), so a day only fails when it's genuinely off by both measures.
    """
    kcal = float(totals.get("kcal") or totals.get("calories") or 0)
    if kcal <= 0 or not target_kcal:
        return "unknown", "unknown", []
    if kcal < target_kcal * (1 - DAILY_KCAL_TOLERANCE):
        kcal_status = "under"
    elif kcal > target_kcal * (1 + DAILY_KCAL_TOLERANCE):
        kcal_status = "over"
    else:
        kcal_status = "ok"

    notes: list[str] = []
    if kcal_status != "ok":
        notes.append(f"{round(kcal)} kcal vs {round(target_kcal)} target")
    if target_macros:
        for key, label in (("protein_g", "protein"), ("carb_g", "carb"), ("fat_g", "fat")):
            t = float(target_macros.get(key) or 0)
            if t <= 0:
                continue
            v = float(totals.get(key) or 0)
            # Pass only within the TIGHTER of the two bands: percentage governs
            # a small target (20g fat -> a 5g band, well under 10g), the flat
            # 10g figure governs a large one (160g protein -> a 40g percentage
            # band would let a 15g miss through; 10g catches it).
            tolerance_g = min(DAILY_MACRO_TOLERANCE_G, DAILY_MACRO_TOLERANCE * t)
            if abs(v - t) > tolerance_g:
                notes.append(f"{label} {round(v)} g vs {round(t)} g target")
    macro_status = "ok" if (kcal_status == "ok" and not notes) else "off"

    # Fiber is reported and flagged when short, but does not by itself fail a
    # day — the corpus estimate for fiber is the weakest of the five, and a
    # low-fiber week is a note for the nutritionist, not a hard block. (Phase
    # B's day solver optimizes it directly.)
    if target_macros and target_macros.get("fiber_g"):
        ft = float(target_macros["fiber_g"])
        fv = float(totals.get("fiber_g") or 0)
        if fv and fv < ft * (1 - DAILY_MACRO_TOLERANCE):
            notes.append(f"fiber {round(fv)} g vs {round(ft)} g target (low)")
    return kcal_status, macro_status, notes


def _pantry_overlap(ingredients: list[str], pantry: set[str]) -> float:
    if not ingredients:
        return 0.0
    hits = sum(1 for ing in ingredients if any(p in ing.lower() or ing.lower() in p for p in pantry))
    return hits / len(ingredients)


def compute_overlap_score(picks: list[MenuPick]) -> int:
    """0-100. Higher = more meaningful-ingredient sharing across the picks."""
    if len(picks) < 2:
        return 0
    sets = [meaningful_ingredients(p.ingredients) for p in picks]
    total_slots = sum(len(s) for s in sets)
    if total_slots == 0:
        return 0
    unique = len(set().union(*sets))
    base = (1.0 - unique / total_slots) * 100.0
    return max(0, min(100, round(base)))


def select_menu(
    candidates: list[dict],
    days: int,
    pantry: set[str],
    target_kcal: float,
    diet_modes: list[str] | None = None,
    constraints: dict[str, int] | None = None,
    cuisine_affinity: list[str] | None = None,
    meals_per_day: int = 1,
    target_macros: dict | None = None,
    start_day: int = 1,
    day_numbers: list[int] | None = None,
    seed_ingredients: set[str] | None = None,
    variety: int = DEFAULT_VARIETY,
    tools_available: list[str] | None = None,
    meal_type_hints: dict[str, list[str]] | None = None,
    meal_prep_requested: bool = False,
) -> dict:
    """Greedy selection: `days * meals_per_day` picks, each maximizing
    overlap+pantry+macro fit against what's already chosen, minus same-book
    and protein-repeat penalties.

    meals_per_day multiplies the pick count in this one Python pass rather
    than requiring the caller to invoke select_menu/plan_menu once per meal —
    that per-meal-round-trip approach used to mean N full LLM turns (search +
    plan) for an N-meals-per-day request, which is genuinely slow against a
    local model (observed: an 18-meal request burned 60 sequential LLM turns
    over ~10 minutes and still hit the round cap before finishing). One call
    here does all N meals' worth of selection in a single pass instead.

    candidates: [{id, title, book, ingredients: [...], macros: {"calories": ...}}]
    constraints: optional {keyword: min_count} the caller wants honored,
                 e.g. {"fish": 2} for "2 fish nights" — a scoring nudge, not a hard
                 requirement (never fails the run if under-satisfied).
    cuisine_affinity: optional list of preferred cuisines, e.g. ["Indian", "Mexican"].
                 Hard-filters the pool to matching cuisines first (case-insensitive
                 exact match against each candidate's cuisine), same "never starve"
                 fallback as the dessert/drink filter — if too few candidates match,
                 the filter is dropped rather than returning an empty/short plan.
    start_day: the day number the first pick gets. 1 for a fresh plan; an
                 existing plan being extended passes its max day + 1 so every
                 pick, top-up note, daily total and infeasibility reason
                 carries the real day number instead of restarting at 1
                 (see tools/menu.py plan_menu's extend_menu_id).
    day_numbers: explicit day number per planned day (len == days), for
                 re-planning a non-contiguous set of failed days (2, 5, 7)
                 while the good ones are kept. Overrides start_day.
    seed_ingredients: meaningful ingredients already committed elsewhere in
                 the plan (kept days, an extended menu's existing days). Picks
                 are scored for overlap with these from the first slot, so a
                 repaired/extended day reuses what's already on the shopping
                 list instead of starting its own.
    variety: 0-100 (see overlap_floor_for / repeat_budget_for). Decides the
                 overlap floor, whether dishes may repeat across days, and at
                 100 switches off the pull toward shared ingredients.
    """
    diet_modes = diet_modes or []
    variety = clamp_variety(variety)
    overlap_floor = overlap_floor_for(variety)
    vegetarian = "vegetarian" in diet_modes or "vegan" in diet_modes
    constraints = constraints or {}
    cuisine_affinity = [c.lower() for c in (cuisine_affinity or [])]
    meals_per_day = max(1, int(meals_per_day or 1))
    start_day = max(1, int(start_day or 1))
    if day_numbers is not None:
        if len(day_numbers) != days:
            raise ValueError(f"day_numbers has {len(day_numbers)} entries for {days} days")
        day_numbers = [int(d) for d in day_numbers]
    seed_ingredients = set(seed_ingredients or ())
    total_picks = days * meals_per_day
    daily_target = (target_kcal or 0) * meals_per_day
    repeat_budget = repeat_budget_for(variety, total_picks, meals_per_day)
    # Distinct dishes the pool must supply: every slot at variety 100, one
    # per meal slot at 0. Pool-size checks below are against this, not
    # total_picks — a same-every-day plan needs two dishes, not fourteen.
    needed_distinct = max(1, total_picks - repeat_budget)
    # Spread penalties matter more the more variety is asked for; at 100
    # nothing should pull two picks toward each other.
    spread_mult = 3.0 if variety >= 100 else 1.0
    overlap_base = 0.0 if variety >= 100 else W_INGREDIENT_OVERLAP

    infeasible_reasons: list[str] = []

    # Dessert / drink / bare-component dishes are NEVER re-admitted, even when
    # that leaves the pool too thin — serving cake because the vegetarian
    # OMAD pool ran short is exactly the failure this guards against. A thin
    # pool is an infeasibility signal (surfaced below), not a reason to relax
    # the rule.
    base_pool = [
        c for c in candidates
        if not is_menu_ineligible(c["title"], c.get("ingredients", []), c.get("course"))
    ]
    if len(base_pool) < needed_distinct:
        infeasible_reasons.append(
            f"only {len(base_pool)} usable dishes in the candidate pool for "
            f"{total_picks} meal slots (desserts, drinks and condiments excluded) "
            f"— search more recipes or widen the books/cuisines"
        )

    # Drop duplicate dishes (same normalized title, first id wins) so a
    # double-indexed recipe can't be served twice — same never-starve rule.
    seen_titles: set[str] = set()
    deduped = []
    for c in base_pool:
        nt = norm_title(c["title"])
        if nt in seen_titles:
            continue
        seen_titles.add(nt)
        deduped.append(c)
    if len(deduped) >= needed_distinct:
        base_pool = deduped

    if cuisine_affinity:
        cuisine_pool = [c for c in base_pool if (c.get("cuisine") or "").lower() in cuisine_affinity]
        if len(cuisine_pool) >= needed_distinct:
            base_pool = cuisine_pool

    # Hard-exclude recipes needing equipment the client doesn't have — a
    # recipe you can't cook shouldn't appear regardless of how well it
    # scores otherwise (feature-spec-2026-09-26.md #5). Unclassified/no-
    # equipment recipes are never touched by this (equipment_blocked
    # returns False for them), so it never thins the pool just because the
    # keyword classifier hasn't reached a recipe yet.
    base_pool = [
        c for c in base_pool
        if not equipment_blocked(c.get("equipment"), tools_available)
    ]
    if len(base_pool) < needed_distinct:
        infeasible_reasons.append(
            f"only {len(base_pool)} dishes usable with the client's available "
            f"equipment for {total_picks} meal slots — search more recipes "
            f"or add tools to tools_available"
        )

    # Meal-prep: same never-starve shape as cuisine_affinity — filter to
    # batch-cook-friendly dishes only when enough remain to fill the plan
    # (feature-spec-2026-09-26.md #2).
    if meal_prep_requested:
        meal_prep_pool = [c for c in base_pool if c.get("meal_prep")]
        if len(meal_prep_pool) >= needed_distinct:
            base_pool = meal_prep_pool

    # A meal's primary (role="main") pick must be able to anchor a meal — a
    # side dish is menu-eligible but is an accompaniment, not a dinner on its
    # own (this is what let "Curried Rice"/"Deviled Eggs" become a day's only
    # dish before). Sides stay in the wider pool for _fill_daily_kcal's
    # top-ups, where pairing one with the chosen main is exactly the point.
    main_pool = [c for c in base_pool if is_meal_main_eligible(c)]
    accompaniment_pool = [c for c in base_pool if not is_meal_main_eligible(c)]
    # A dish with no nutrition data can't be graded or portion-solved. When
    # enough dishes WITH data exist to fill every slot, plan only from those
    # — an "unknown" day is a plan that can't be checked, not a plan. Same
    # never-starve shape as the cuisine filter: with too few known dishes the
    # unknowns stay in (and UNKNOWN_NUTRITION_PENALTY nudges past them).
    known_mains = [c for c in main_pool if float(c.get("macros", {}).get("calories") or 0) > 0]
    if len(known_mains) >= needed_distinct:
        main_pool = known_mains
    if len(main_pool) < needed_distinct:
        infeasible_reasons.append(
            f"only {len(main_pool)} main-eligible dishes for {total_picks} meal "
            f"slots — classified sides can accompany a meal but can't anchor "
            f"one; search more recipes or widen the books/cuisines"
        )

    def _day_of(slot: int) -> int:
        day_idx = slot // meals_per_day
        return day_numbers[day_idx] if day_numbers is not None else day_idx + start_day

    def _pick_from(c: dict, slot: int, shared: list[str], reason: str) -> MenuPick:
        return MenuPick(
            recipe_id=c["id"], title=c["title"], book=c["book"], path=c.get("path"),
            ingredients=c["ingredients"], shared_ingredients=shared,
            macro_fit=round(macro_fit(c.get("macros", {}), target_kcal, target_macros), 2),
            pick_reason=reason,
            day=_day_of(slot),
            meal=slot % meals_per_day + 1,
            kcal=float(c.get("macros", {}).get("calories") or 0),
            protein_g=float(c.get("macros", {}).get("protein_g") or 0),
            carb_g=float(c.get("macros", {}).get("carb_g") or 0),
            fat_g=float(c.get("macros", {}).get("fat_g") or 0),
            fiber_g=float(c.get("macros", {}).get("fiber_g") or 0),
            servings=c.get("servings"),
        )

    def _greedy(overlap_weight: float, n_picks: int = total_picks) -> tuple[list[MenuPick], list[dict], dict[str, int], set[str]]:
        pool = list(main_pool)
        picks: list[MenuPick] = []
        selected_meaningful: set[str] = set(seed_ingredients)
        book_counts: dict[str, int] = {}
        protein_counts: dict[str, int] = {}
        constraint_counts: dict[str, int] = {kw: 0 for kw in constraints}

        def score(c: dict) -> float:
            ings = c["ingredients"]
            m_ings = meaningful_ingredients(ings)
            overlap = len(m_ings & selected_meaningful)
            pantry_s = _pantry_overlap(ings, pantry)
            macro_s = macro_fit(c.get("macros", {}), target_kcal, target_macros)
            book_pen = book_counts.get(c["book"], 0)
            mp = main_protein(ings)
            protein_pen = protein_counts.get(mp, 0) if mp else 0
            missing_protein_pen = 0.0 if vegetarian or has_protein(ings) else PROTEIN_MISSING_PENALTY
            unknown_pen = 0.0 if float(c.get("macros", {}).get("calories") or 0) > 0 else UNKNOWN_NUTRITION_PENALTY

            # Course vs slot position: slot 1 of a multi-meal day wants
            # breakfast, later slots don't. A per-client meal_type_hints
            # entry overrides the classified course for this check only.
            slot_adj = 0.0
            if meals_per_day > 1:
                hinted = hinted_meal_types(c, meal_type_hints)
                is_breakfast = ("breakfast" in hinted) if hinted is not None else (
                    (c.get("course") or "").strip().lower() == "breakfast"
                )
                meal_number = slot % meals_per_day + 1
                if is_breakfast:
                    slot_adj = BREAKFAST_SLOT_BONUS if meal_number == 1 else -BREAKFAST_OFFSLOT_PENALTY

            s = (overlap_weight * overlap
                 + W_PANTRY_OVERLAP * pantry_s
                 + W_MACRO_FIT * macro_s
                 - W_SAME_BOOK_PENALTY * spread_mult * book_pen
                 - W_PROTEIN_REPEAT_PENALTY * spread_mult * protein_pen
                 - missing_protein_pen
                 - unknown_pen
                 + slot_adj)

            joined = " ".join(ings).lower() + " " + c["title"].lower()
            for kw, target_n in constraints.items():
                if constraint_counts[kw] < target_n and kw.lower() in joined:
                    s += CONSTRAINT_NUDGE
            return s

        for slot in range(min(n_picks, len(pool))):
            pool.sort(key=score, reverse=True)
            best = pool.pop(0)

            m_ings = meaningful_ingredients(best["ingredients"])
            shared = sorted(m_ings & selected_meaningful)
            reason = (
                "best pantry/macro fit" if not picks and not shared
                else f"shares {len(shared)} ingredients"
            )
            picks.append(_pick_from(best, slot, shared, reason))
            selected_meaningful |= m_ings
            book_counts[best["book"]] = book_counts.get(best["book"], 0) + 1
            mp = main_protein(best["ingredients"])
            if mp:
                protein_counts[mp] = protein_counts.get(mp, 0) + 1

            joined = " ".join(best["ingredients"]).lower() + " " + best["title"].lower()
            for kw in constraint_counts:
                if kw.lower() in joined:
                    constraint_counts[kw] += 1

        return picks, pool, constraint_counts, selected_meaningful

    if variety <= 0 and days > 1:
        # Same dishes every day: plan ONE day fully — mains, top-ups, portion
        # solve — then copy it to every other day. Top-ups included, or the
        # "same meals" promise breaks the moment a day needs a second dish.
        picks, leftover_pool, constraint_counts, selected_meaningful = _greedy(
            W_INGREDIENT_OVERLAP, n_picks=meals_per_day
        )
        day_one, day_one_totals, _ = _fill_daily_kcal(
            picks, leftover_pool + accompaniment_pool, selected_meaningful,
            days=1, meals_per_day=meals_per_day, daily_target=daily_target,
            target_macros=target_macros, per_meal_kcal=target_kcal,
        )
        picks, daily_totals = [], []
        for day_idx in range(days):
            day = _day_of(day_idx * meals_per_day)
            for src in day_one:
                dup = replace(src, day=day)
                if day_idx:
                    dup.pick_reason = f"repeat of day {day_one[0].day} (variety 0)"
                picks.append(dup)
            for t in day_one_totals:
                daily_totals.append({**t, "day": day})
        repeats_used = len(day_one) * (days - 1)
        overlap_score = compute_overlap_score(picks)
        return _select_result(
            picks, daily_totals, infeasible_reasons, total_picks=total_picks,
            overlap_score=overlap_score, overlap_floor=overlap_floor,
            # The structure IS the floor here: nothing could share more.
            floor_met=True, variety=variety, repeats=repeats_used,
            constraint_counts=constraint_counts,
        )
    else:
        # Overlap floor: retry with the overlap weight doubled each attempt
        # until the plan-wide score clears it; keep the best attempt seen.
        best_attempt = None
        for mult in _OVERLAP_ESCALATION:
            attempt = _greedy(overlap_base * mult)
            attempt_score = compute_overlap_score(attempt[0])
            if best_attempt is None or attempt_score > best_attempt[1]:
                best_attempt = (attempt, attempt_score)
            if attempt_score >= overlap_floor or overlap_base == 0:
                best_attempt = (attempt, attempt_score)
                break
        (picks, leftover_pool, constraint_counts, selected_meaningful), _ = best_attempt
        picks, leftover_pool, repeats_used = _spend_repeats(
            picks, leftover_pool, total_picks=total_picks, meals_per_day=meals_per_day,
            budget=repeat_budget, floor=overlap_floor, day_of=_day_of, variety=variety,
        )
        selected_meaningful = set(seed_ingredients)
        for p in picks:
            selected_meaningful |= meaningful_ingredients(p.ingredients)

    # Top-ups draw from unpicked mains AND the accompaniment pool (sides etc.)
    # — a side is exactly what should fill out a meal that's short on kcal
    # next to an already-chosen main.
    topup_pool = leftover_pool + accompaniment_pool

    # Below 50 the user asked for repetition, so a top-up dish may serve
    # several days while budget remains (never twice on one day) — otherwise
    # every under-fed day adds a fresh dish and dilutes the sharing the
    # repeats just bought. At 50+ top-ups stay fresh: repeats there come only
    # from the floor mechanism above, never spent for their own sake.
    reuse_budget = max(0, repeat_budget - repeats_used) if variety < 50 else 0
    picks, daily_totals, reused = _fill_daily_kcal(
        picks, topup_pool, selected_meaningful,
        days=days, meals_per_day=meals_per_day, daily_target=daily_target,
        target_macros=target_macros, per_meal_kcal=target_kcal,
        reuse_budget=reuse_budget,
    )
    repeats_used += reused

    overlap_score = compute_overlap_score(picks)
    return _select_result(
        picks, daily_totals, infeasible_reasons, total_picks=total_picks,
        overlap_score=overlap_score, overlap_floor=overlap_floor,
        floor_met=overlap_score >= overlap_floor, variety=variety,
        repeats=repeats_used, constraint_counts=constraint_counts,
    )


def _select_result(
    picks: list[MenuPick], daily_totals: list[dict], infeasible_reasons: list[str], *,
    total_picks: int, overlap_score: int, overlap_floor: int, floor_met: bool,
    variety: int, repeats: int, constraint_counts: dict[str, int],
) -> dict:
    # Feasibility: every day must clear both the calorie band and every macro
    # (day_macro_status == "ok"). Days with no nutrition data ("unknown") are
    # not counted as failures here — they carry their own low-confidence flag.
    main_slots = sum(1 for p in picks if p.role == "main")
    off_days = [d for d in daily_totals if d.get("macro_status") == "off"]
    for d in off_days:
        note = "; ".join(d.get("gap_notes") or []) or "misses target"
        infeasible_reasons.append(f"day {d['day']}: {note}")
    if main_slots < total_picks:
        infeasible_reasons.append(
            f"could only fill {main_slots} of {total_picks} meal slots"
        )
    return {
        "picks": picks,
        "feasible": not infeasible_reasons,
        "infeasible_reasons": infeasible_reasons,
        "overlap_score": overlap_score,
        "overlap_floor": overlap_floor,
        "overlap_floor_met": floor_met,
        "variety": variety,
        "repeats": repeats,
        "macro_fit_avg": round(sum(p.macro_fit for p in picks) / len(picks), 2) if picks else 0.0,
        "constraints_met": constraint_counts,
        "daily_totals": daily_totals,
    }


def _repeat_of(src: MenuPick, slot: int, day: int, meals_per_day: int, variety: int) -> MenuPick:
    return replace(
        src, day=day, meal=slot % meals_per_day + 1, role="main",
        shared_ingredients=sorted(meaningful_ingredients(src.ingredients)),
        pick_reason=f"repeat of day {src.day} (variety {variety})",
        portion_multiplier=1.0,
    )


def _spend_repeats(
    picks: list[MenuPick], leftover_pool: list[dict], *, total_picks: int,
    meals_per_day: int, budget: int, floor: int, day_of, variety: int,
) -> tuple[list[MenuPick], list[dict], int]:
    """Fill short slots, then trade the loneliest picks for repeats of a
    dish already on the plan — one at a time, stopping the moment the
    overlap floor is met or the budget is spent. A day never gets the same
    dish twice. Fewest repeats that satisfy the floor, by construction."""
    used = 0
    picks = list(picks)
    if budget <= 0 or not picks:
        return picks, leftover_pool, 0

    def _day_dishes(day: int) -> set[int]:
        return {p.recipe_id for p in picks if p.day == day}

    def _best_repeat_for(slot: int) -> MenuPick | None:
        day = day_of(slot)
        taken = _day_dishes(day)
        sources = {}
        for p in picks:
            if p.recipe_id not in taken and p.recipe_id not in sources:
                sources[p.recipe_id] = p
        if not sources:
            return None
        # The dish that lifts the plan-wide overlap most; same meal number
        # first so breakfast repeats breakfast, then macro fit.
        def gain(src: MenuPick) -> tuple:
            trial = picks + [_repeat_of(src, slot, day, meals_per_day, variety)]
            return (compute_overlap_score(trial), src.meal == slot % meals_per_day + 1, src.macro_fit)
        src = max(sources.values(), key=gain)
        return _repeat_of(src, slot, day, meals_per_day, variety)

    # 1. Slots the pool couldn't fill at all.
    while len(picks) < total_picks and used < budget:
        rep = _best_repeat_for(len(picks))
        if rep is None:
            break
        picks.append(rep)
        used += 1

    # 2. Floor: swap out the pick sharing the least with the rest of the plan.
    while used < budget and compute_overlap_score(picks) < floor:
        distinct = {p.recipe_id for p in picks}
        if len(distinct) <= meals_per_day:
            break  # already as repetitive as the structure allows
        counts = {}
        for p in picks:
            counts[p.recipe_id] = counts.get(p.recipe_id, 0) + 1
        candidates = []
        for i, p in enumerate(picks):
            if counts[p.recipe_id] > 1 or p.day == picks[0].day:
                continue  # keep day one distinct; never orphan a repeated dish's source
            others = set()
            for q in picks:
                if q is not p:
                    others |= meaningful_ingredients(q.ingredients)
            mine = meaningful_ingredients(p.ingredients)
            lonely = len(mine - others) / max(1, len(mine))
            candidates.append((lonely, -p.macro_fit, i))
        if not candidates:
            break
        _, _, idx = max(candidates)
        victim = picks[idx]
        slot = next(
            (s for s in range(total_picks) if day_of(s) == victim.day and s % meals_per_day + 1 == victim.meal),
            idx,
        )
        without = picks[:idx] + picks[idx + 1:]
        picks_backup = picks
        picks = without
        rep = _best_repeat_for(slot)
        if rep is None:
            picks = picks_backup
            break
        picks.insert(idx, rep)
        leftover_pool = leftover_pool + [{
            "id": victim.recipe_id, "title": victim.title, "book": victim.book,
            "path": victim.path, "ingredients": victim.ingredients, "servings": victim.servings,
            "macros": {"calories": victim.kcal, "protein_g": victim.protein_g,
                       "carb_g": victim.carb_g, "fat_g": victim.fat_g, "fiber_g": victim.fiber_g},
        }]
        used += 1
    return picks, leftover_pool, used


def scale_day_portions(
    day_picks: list[MenuPick],
    daily_target: float,
    per_meal_kcal: float,
    target_macros: dict | None = None,
) -> None:
    """Scale one day's portions in place so its total kcal hits the daily
    target (clamped to PORTION_MULT_MIN..MAX). Macros are per-serving, so the
    multiplier reads as "servings to eat" no matter how many the recipe batch
    feeds. Then re-score each meal's macro_fit from the combined scaled
    macros — the fit should describe what's actually served, not one
    unscaled serving against a whole-meal target.
    """
    day_kcal = sum(p.kcal for p in day_picks)
    if day_kcal <= 0 or not daily_target:
        return
    mult = max(PORTION_MULT_MIN, min(PORTION_MULT_MAX, daily_target / day_kcal))
    if abs(mult - 1.0) >= 0.005:
        for p in day_picks:
            _apply_multiplier(p, mult)
    _rescore_meal_fits(day_picks, per_meal_kcal, target_macros)


def _apply_multiplier(p: "MenuPick", mult: float) -> None:
    p.portion_multiplier = round(p.portion_multiplier * mult, 2)
    p.kcal = round(p.kcal * mult, 1)
    p.protein_g = round(p.protein_g * mult, 1)
    p.carb_g = round(p.carb_g * mult, 1)
    p.fat_g = round(p.fat_g * mult, 1)
    p.fiber_g = round(p.fiber_g * mult, 1)


def _rescore_meal_fits(
    day_picks: list["MenuPick"], per_meal_kcal: float, target_macros: dict | None
) -> None:
    meals: dict[int, list[MenuPick]] = {}
    for p in day_picks:
        meals.setdefault(p.meal, []).append(p)
    for meal_picks in meals.values():
        combined = {
            "calories": sum(p.kcal for p in meal_picks),
            "protein_g": sum(p.protein_g for p in meal_picks),
            "carb_g": sum(p.carb_g for p in meal_picks),
            "fat_g": sum(p.fat_g for p in meal_picks),
        }
        fit = round(macro_fit(combined, per_meal_kcal, target_macros), 2)
        for p in meal_picks:
            p.macro_fit = fit


def solve_day_portions(
    day_picks: list["MenuPick"],
    daily_target: float,
    per_meal_kcal: float,
    target_macros: dict | None = None,
) -> None:
    """Set each dish's portion multiplier so the DAY's totals fit the full
    nutrient target — kcal AND protein/carb/fat/fiber — not just calories.

    Falls back to uniform calorie scaling (scale_day_portions) when there is
    no macro target or the day has no usable nutrition data. See
    engines/day_solver.py for the bounded least-squares solve.
    """
    day_kcal = sum(p.kcal for p in day_picks)
    if day_kcal <= 0 or not daily_target:
        _rescore_meal_fits(day_picks, per_meal_kcal, target_macros)
        return

    from aboyeur.engines import day_solver

    target = _daily_target_vector(daily_target, per_meal_kcal, target_macros)
    dish_macros = [
        {"kcal": p.kcal, "protein_g": p.protein_g, "carb_g": p.carb_g,
         "fat_g": p.fat_g, "fiber_g": p.fiber_g}
        for p in day_picks
    ]
    mults = day_solver.solve_day_multipliers(dish_macros, target)
    for p, m in zip(day_picks, mults):
        if abs(m - 1.0) >= 0.005:
            _apply_multiplier(p, m)
    _rescore_meal_fits(day_picks, per_meal_kcal, target_macros)


def _daily_target_vector(
    daily_target: float, per_meal_kcal: float, target_macros: dict | None
) -> dict:
    """{kcal, protein_g, carb_g, fat_g, fiber_g} at DAY scale, from the
    per-meal kcal + per-meal macro targets the optimizer works in."""
    target = {"kcal": daily_target}
    scale = (daily_target / per_meal_kcal) if per_meal_kcal else 1.0
    if target_macros:
        for k in ("protein_g", "carb_g", "fat_g", "fiber_g"):
            if target_macros.get(k):
                target[k] = float(target_macros[k]) * scale
    return target


def _dish_macro_dict(src) -> dict:
    """Per-serving macro dict from either a MenuPick or a candidate dict."""
    if isinstance(src, MenuPick):
        return {"kcal": src.kcal, "protein_g": src.protein_g, "carb_g": src.carb_g,
                "fat_g": src.fat_g, "fiber_g": src.fiber_g}
    m = src.get("macros", {})
    return {"kcal": float(m.get("calories") or 0), "protein_g": float(m.get("protein_g") or 0),
            "carb_g": float(m.get("carb_g") or 0), "fat_g": float(m.get("fat_g") or 0),
            "fiber_g": float(m.get("fiber_g") or 0)}


def _repair_day_macros(
    day_picks: list[MenuPick],
    pool: list[dict],
    day_target: dict,
    per_meal_kcal: float,
    target_macros: dict | None,
    selected_meaningful: set[str],
    picked_titles: set[str],
    max_swaps: int = 4,
) -> None:
    """When scaling portions can't fix a day's macro SHAPE (e.g. the greedy
    stacked three carb-forward dishes on one day), swap the worst-fitting
    dish for a pool candidate that moves the day's residual down. Evaluates
    each trial set through the same bounded portion solve the day will get.

    In place: mutates `day_picks` (swapping dish identity + macros), `pool`
    and `picked_titles`. Only ever swaps role="main" / "top-up" dishes for
    other real meals — never introduces a dessert/drink (pool is pre-filtered).
    """
    from aboyeur.engines import day_solver

    if not pool or not target_macros:
        return

    def residual_of(picks: list[MenuPick]) -> float:
        macros = [_dish_macro_dict(p) for p in picks]
        mults = day_solver.solve_day_multipliers(macros, day_target)
        return day_solver.residual(day_solver.combined_totals(macros, mults), day_target)

    # Portions aren't solved yet at this point — day_picks carry per-serving
    # macros, which is exactly what residual_of wants (it solves multipliers).
    cur = residual_of(day_picks)

    # Candidate replacements: closest by a cheap per-serving macro_fit, capped.
    ranked_pool = sorted(
        pool, key=lambda c: -macro_fit(c.get("macros", {}), per_meal_kcal, target_macros)
    )[:20]

    for _ in range(max_swaps):
        best_move = None  # (new_residual, pick_index, candidate)
        for i, p in enumerate(day_picks):
            for c in ranked_pool:
                nt = norm_title(c["title"])
                if nt in picked_titles:
                    continue
                trial = list(day_picks)
                cm = _dish_macro_dict(c)
                trial[i] = MenuPick(
                    recipe_id=c["id"], title=c["title"], book=c["book"], path=c.get("path"),
                    ingredients=c["ingredients"], shared_ingredients=[],
                    macro_fit=0.0, pick_reason="", day=p.day, meal=p.meal, role=p.role,
                    kcal=cm["kcal"], protein_g=cm["protein_g"], carb_g=cm["carb_g"],
                    fat_g=cm["fat_g"], fiber_g=cm["fiber_g"], servings=c.get("servings"),
                )
                r = residual_of(trial)
                if r < cur - 1e-4 and (best_move is None or r < best_move[0]):
                    best_move = (r, i, c)
        if best_move is None:
            break
        r, i, c = best_move
        old = day_picks[i]
        picked_titles.discard(norm_title(old.title))
        picked_titles.add(norm_title(c["title"]))
        cm = _dish_macro_dict(c)
        m_ings = meaningful_ingredients(c["ingredients"])
        day_picks[i] = MenuPick(
            recipe_id=c["id"], title=c["title"], book=c["book"], path=c.get("path"),
            ingredients=c["ingredients"],
            shared_ingredients=sorted(m_ings & selected_meaningful),
            macro_fit=0.0, pick_reason=f"macro repair (was {old.title})",
            day=old.day, meal=old.meal, role=old.role,
            kcal=cm["kcal"], protein_g=cm["protein_g"], carb_g=cm["carb_g"],
            fat_g=cm["fat_g"], fiber_g=cm["fiber_g"], servings=c.get("servings"),
        )
        selected_meaningful |= m_ings
        pool[:] = [x for x in pool if norm_title(x["title"]) not in picked_titles]
        ranked_pool = [x for x in ranked_pool if norm_title(x["title"]) not in picked_titles]
        cur = r


def _fill_daily_kcal(
    picks: list[MenuPick],
    pool: list[dict],
    selected_meaningful: set[str],
    days: int,
    meals_per_day: int,
    daily_target: float,
    target_macros: dict | None,
    per_meal_kcal: float,
    reuse_budget: int = 0,
) -> tuple[list[MenuPick], list[dict], int]:
    """Ensure each day's total kcal lands within DAILY_KCAL_TOLERANCE of the
    daily target by adding a second dish (role="top-up") to under-fed meals,
    max MAX_DISHES_PER_MEAL dishes per meal. Days with no kcal data at all
    are flagged "unknown" and left alone — no calorie math without calories.

    reuse_budget: how many top-ups may be a dish already used as a top-up on
    another day (low variety). 0 = every top-up is a fresh dish.

    Returns (all picks sorted by day/meal, per-day totals with status flags,
    top-ups that reused a dish).
    """
    by_day: dict[int, list[MenuPick]] = {}
    for p in picks:
        by_day.setdefault(p.day, []).append(p)

    picked_titles = {norm_title(p.title) for p in picks}
    pool = [
        c for c in pool
        if not is_menu_ineligible(c["title"], c.get("ingredients", []), c.get("course"))
        and float(c.get("macros", {}).get("calories") or 0) > 0
        and norm_title(c["title"]) not in picked_titles
    ]
    topup_titles: set[str] = set()  # used as a top-up somewhere already
    reused = 0

    for day in sorted(by_day):
        day_picks = by_day[day]
        day_kcal = sum(p.kcal for p in day_picks)
        if day_kcal <= 0 or not daily_target:
            continue  # no nutrition data for this day — can't top up honestly

        while day_kcal < daily_target * (1 - DAILY_KCAL_TOLERANCE) and pool:
            gap = daily_target - day_kcal
            # Meal with the fewest dishes, then the lowest kcal, gets the extra dish.
            dish_counts: dict[int, int] = {}
            meal_kcal: dict[int, float] = {}
            for p in day_picks:
                dish_counts[p.meal] = dish_counts.get(p.meal, 0) + 1
                meal_kcal[p.meal] = meal_kcal.get(p.meal, 0) + p.kcal
            open_meals = [m for m in dish_counts if dish_counts[m] < MAX_DISHES_PER_MEAL]
            if not open_meals and day_kcal * PORTION_MULT_MAX < daily_target * (1 - DAILY_KCAL_TOLERANCE):
                # Even max portion scaling can't stretch the picked dishes to
                # the day target (an OMAD day of small curries). One extra
                # dish per meal beats reporting a guaranteed-under day.
                open_meals = [m for m in dish_counts if dish_counts[m] < MAX_DISHES_PER_MEAL + 1]
            if not open_meals:
                break
            target_meal = min(open_meals, key=lambda m: (dish_counts[m], meal_kcal[m]))

            def top_up_score(c: dict) -> float:
                kcal = float(c["macros"]["calories"])
                closeness = max(0.0, 1.0 - abs(gap - kcal) / gap)
                overlap = len(meaningful_ingredients(c["ingredients"]) & selected_meaningful)
                # A side pairs naturally with whatever main already anchors the
                # meal — nudge toward that realistic plate shape, but it's a
                # tie-breaker: kcal fit and overlap still decide most picks.
                side_bonus = 0.5 if (c.get("course") or "").lower() == "side" else 0.0
                return W_MACRO_FIT * closeness + W_INGREDIENT_OVERLAP * overlap + side_bonus

            day_titles = {norm_title(p.title) for p in day_picks}
            day_pool = [c for c in pool if norm_title(c["title"]) not in day_titles]
            if not day_pool:
                break
            best = max(day_pool, key=top_up_score)
            best_kcal = float(best["macros"]["calories"])
            # Only add a dish that moves the day closer to target — never
            # overshoot past what leaving it under would cost.
            if abs((day_kcal + best_kcal) - daily_target) >= abs(day_kcal - daily_target):
                break
            bt = norm_title(best["title"])
            if bt in topup_titles:
                reused += 1
            topup_titles.add(bt)
            # A used top-up leaves the pool once no reuse budget remains.
            if reused >= reuse_budget:
                picked_titles |= topup_titles
                pool = [c for c in pool if norm_title(c["title"]) not in picked_titles]
            m_ings = meaningful_ingredients(best["ingredients"])
            shared = sorted(m_ings & selected_meaningful)
            day_picks.append(MenuPick(
                recipe_id=best["id"], title=best["title"], book=best["book"], path=best.get("path"),
                ingredients=best["ingredients"], shared_ingredients=shared,
                macro_fit=round(macro_fit(best.get("macros", {}), per_meal_kcal, target_macros), 2),
                pick_reason=f"top-up: day {day} was {round(gap)} kcal under target",
                day=day, meal=target_meal, role="top-up", kcal=best_kcal,
                protein_g=float(best["macros"].get("protein_g") or 0),
                carb_g=float(best["macros"].get("carb_g") or 0),
                fat_g=float(best["macros"].get("fat_g") or 0),
                fiber_g=float(best["macros"].get("fiber_g") or 0),
                servings=best.get("servings"),
            ))
            selected_meaningful |= m_ings
            day_kcal += best_kcal

        # If the day's dish MIX can't be scaled onto every macro (greedy
        # stacked, say, three carb-forward dishes here), swap the worst
        # offenders for pool dishes that fix the shape — then solve portions.
        day_target = _daily_target_vector(daily_target, per_meal_kcal, target_macros)
        if target_macros:
            from aboyeur.engines import day_solver
            trial_macros = [_dish_macro_dict(p) for p in day_picks]
            trial_mults = day_solver.solve_day_multipliers(trial_macros, day_target)
            if day_solver.residual(
                day_solver.combined_totals(trial_macros, trial_mults), day_target
            ) > 0.12:
                _repair_day_macros(
                    day_picks, pool, day_target, per_meal_kcal, target_macros,
                    selected_meaningful, picked_titles,
                )

        # Top-ups add whole dishes; the per-dish portion solve closes the rest
        # of the gap on every macro at once — without it a 1-meal (OMAD) day
        # maxes out at 2 single servings and can never reach a real daily
        # target, and uniform scaling would hit calories while missing protein.
        solve_day_portions(day_picks, daily_target, per_meal_kcal, target_macros)

    all_picks = sorted(
        (p for dps in by_day.values() for p in dps),
        key=lambda p: (p.day, p.meal, p.role != "main"),
    )
    daily_macros = daily_macros_from_per_meal(target_macros, meals_per_day)
    daily_totals = []
    for day in sorted(by_day):
        dps = by_day[day]
        totals = {
            "kcal": sum(p.kcal for p in dps),
            "protein_g": sum(p.protein_g for p in dps),
            "carb_g": sum(p.carb_g for p in dps),
            "fat_g": sum(p.fat_g for p in dps),
            "fiber_g": sum(p.fiber_g for p in dps),
        }
        status, macro_status, gap_notes = day_macro_status(totals, daily_target, daily_macros)
        # A meal that lands far under its own per-meal target ("coffee only")
        # makes the day infeasible regardless of the day-level number — but
        # only when we actually have nutrition data. A day with no kcal at all
        # is "unknown"/low-confidence, not "infeasible".
        meal_kcals: dict[int, float] = {}
        for p in dps:
            meal_kcals[p.meal] = meal_kcals.get(p.meal, 0.0) + p.kcal
        meal_floor = min(per_meal_kcal * MEAL_MIN_KCAL_FRACTION, MEAL_MIN_KCAL_ABS) if per_meal_kcal else 0
        thin_meals = [
            m for m, k in meal_kcals.items() if meal_floor and 0 < k < meal_floor
        ] if status != "unknown" else []
        if thin_meals:
            for m in sorted(thin_meals):
                gap_notes.append(
                    f"meal {m} only {round(meal_kcals[m])} kcal (needs ~{round(meal_floor)})"
                )
            macro_status = "off"
        daily_totals.append({
            "day": day, "kcal": round(totals["kcal"]),
            "protein_g": round(totals["protein_g"], 1),
            "carb_g": round(totals["carb_g"], 1),
            "fat_g": round(totals["fat_g"], 1),
            "fiber_g": round(totals["fiber_g"], 1),
            "target_kcal": round(daily_target), "status": status,
            "macro_status": macro_status, "gap_notes": gap_notes,
        })
    return all_picks, daily_totals, reused
