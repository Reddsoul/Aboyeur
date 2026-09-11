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
from dataclasses import dataclass

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
PROTEIN_MISSING_PENALTY = 2.5
CONSTRAINT_NUDGE = 3.0

# Hard floor on the plan-wide overlap_score (0-100). If the first greedy pass
# lands under it, selection is retried with the overlap weight doubled (then
# quadrupled, ...) and the best attempt wins — trading some macro fit for
# shared ingredients, which is the whole point of planning a week at once.
MIN_OVERLAP_SCORE = 25
_OVERLAP_ESCALATION = (1.0, 2.0, 4.0, 8.0)

# A day is "on target" when its total kcal is within this fraction of the
# household's daily need. Days under it get a second dish added to a meal
# (max 2 dishes per meal) until they land inside the band.
DAILY_KCAL_TOLERANCE = 0.15
# Protein/carb/fat are looser than kcal — the corpus estimate is noisier per
# macro than in aggregate — but a day is only "ok" when every macro clears
# this, not just calories. A 2400 kcal day that is 380 g carb / 25 g protein
# is not on target however close its calorie count lands.
DAILY_MACRO_TOLERANCE = 0.25
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
            if abs(v - t) / t > DAILY_MACRO_TOLERANCE:
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
    """
    diet_modes = diet_modes or []
    vegetarian = "vegetarian" in diet_modes or "vegan" in diet_modes
    constraints = constraints or {}
    cuisine_affinity = [c.lower() for c in (cuisine_affinity or [])]
    meals_per_day = max(1, int(meals_per_day or 1))
    total_picks = days * meals_per_day
    daily_target = (target_kcal or 0) * meals_per_day

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
    if len(base_pool) < total_picks:
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
    if len(deduped) >= total_picks:
        base_pool = deduped

    if cuisine_affinity:
        cuisine_pool = [c for c in base_pool if (c.get("cuisine") or "").lower() in cuisine_affinity]
        if len(cuisine_pool) >= total_picks:
            base_pool = cuisine_pool

    # A meal's primary (role="main") pick must be able to anchor a meal — a
    # side dish is menu-eligible but is an accompaniment, not a dinner on its
    # own (this is what let "Curried Rice"/"Deviled Eggs" become a day's only
    # dish before). Sides stay in the wider pool for _fill_daily_kcal's
    # top-ups, where pairing one with the chosen main is exactly the point.
    main_pool = [c for c in base_pool if is_meal_main_eligible(c)]
    accompaniment_pool = [c for c in base_pool if not is_meal_main_eligible(c)]
    if len(main_pool) < total_picks:
        infeasible_reasons.append(
            f"only {len(main_pool)} main-eligible dishes for {total_picks} meal "
            f"slots — classified sides can accompany a meal but can't anchor "
            f"one; search more recipes or widen the books/cuisines"
        )

    def _greedy(overlap_weight: float) -> tuple[list[MenuPick], list[dict], dict[str, int], set[str]]:
        pool = list(main_pool)
        picks: list[MenuPick] = []
        selected_meaningful: set[str] = set()
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

            s = (overlap_weight * overlap
                 + W_PANTRY_OVERLAP * pantry_s
                 + W_MACRO_FIT * macro_s
                 - W_SAME_BOOK_PENALTY * book_pen
                 - W_PROTEIN_REPEAT_PENALTY * protein_pen
                 - missing_protein_pen)

            joined = " ".join(ings).lower() + " " + c["title"].lower()
            for kw, target_n in constraints.items():
                if constraint_counts[kw] < target_n and kw.lower() in joined:
                    s += CONSTRAINT_NUDGE
            return s

        for slot in range(min(total_picks, len(pool))):
            pool.sort(key=score, reverse=True)
            best = pool.pop(0)

            m_ings = meaningful_ingredients(best["ingredients"])
            shared = sorted(m_ings & selected_meaningful)
            reason = "best pantry/macro fit" if not picks else f"shares {len(shared)} ingredients"
            picks.append(MenuPick(
                recipe_id=best["id"], title=best["title"], book=best["book"], path=best.get("path"),
                ingredients=best["ingredients"], shared_ingredients=shared,
                macro_fit=round(macro_fit(best.get("macros", {}), target_kcal, target_macros), 2),
                pick_reason=reason,
                day=slot // meals_per_day + 1, meal=slot % meals_per_day + 1,
                kcal=float(best.get("macros", {}).get("calories") or 0),
                protein_g=float(best.get("macros", {}).get("protein_g") or 0),
                carb_g=float(best.get("macros", {}).get("carb_g") or 0),
                fat_g=float(best.get("macros", {}).get("fat_g") or 0),
                fiber_g=float(best.get("macros", {}).get("fiber_g") or 0),
                servings=best.get("servings"),
            ))
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

    # Overlap floor: retry with the overlap weight doubled each attempt until
    # the plan-wide score clears MIN_OVERLAP_SCORE; keep the best attempt seen.
    best_attempt = None
    for mult in _OVERLAP_ESCALATION:
        attempt = _greedy(W_INGREDIENT_OVERLAP * mult)
        attempt_score = compute_overlap_score(attempt[0])
        if best_attempt is None or attempt_score > best_attempt[1]:
            best_attempt = (attempt, attempt_score)
        if attempt_score >= MIN_OVERLAP_SCORE:
            best_attempt = (attempt, attempt_score)
            break
    (picks, leftover_pool, constraint_counts, selected_meaningful), _ = best_attempt

    # Top-ups draw from unpicked mains AND the accompaniment pool (sides etc.)
    # — a side is exactly what should fill out a meal that's short on kcal
    # next to an already-chosen main.
    topup_pool = leftover_pool + accompaniment_pool

    picks, daily_totals = _fill_daily_kcal(
        picks, topup_pool, selected_meaningful,
        days=days, meals_per_day=meals_per_day, daily_target=daily_target,
        target_macros=target_macros, per_meal_kcal=target_kcal,
    )

    overlap_score = compute_overlap_score(picks)

    # Feasibility: every day must clear both the calorie band and every macro
    # (day_macro_status == "ok"). Days with no nutrition data ("unknown") are
    # not counted as failures here — they carry their own low-confidence flag.
    off_days = [d for d in daily_totals if d.get("macro_status") == "off"]
    for d in off_days:
        note = "; ".join(d.get("gap_notes") or []) or "misses target"
        infeasible_reasons.append(f"day {d['day']}: {note}")
    if len(picks) < total_picks:
        infeasible_reasons.append(
            f"could only fill {len(picks)} of {total_picks} meal slots"
        )
    feasible = not infeasible_reasons

    return {
        "picks": picks,
        "feasible": feasible,
        "infeasible_reasons": infeasible_reasons,
        "overlap_score": overlap_score,
        "overlap_floor_met": overlap_score >= MIN_OVERLAP_SCORE,
        "macro_fit_avg": round(sum(p.macro_fit for p in picks) / len(picks), 2) if picks else 0.0,
        "constraints_met": constraint_counts,
        "daily_totals": daily_totals,
    }


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
) -> tuple[list[MenuPick], list[dict]]:
    """Ensure each day's total kcal lands within DAILY_KCAL_TOLERANCE of the
    daily target by adding a second dish (role="top-up") to under-fed meals,
    max MAX_DISHES_PER_MEAL dishes per meal. Days with no kcal data at all
    are flagged "unknown" and left alone — no calorie math without calories.

    Returns (all picks sorted by day/meal, per-day totals with status flags).
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

            best = max(pool, key=top_up_score)
            best_kcal = float(best["macros"]["calories"])
            # Only add a dish that moves the day closer to target — never
            # overshoot past what leaving it under would cost.
            if abs((day_kcal + best_kcal) - daily_target) >= abs(day_kcal - daily_target):
                break
            picked_titles.add(norm_title(best["title"]))
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
    return all_picks, daily_totals
