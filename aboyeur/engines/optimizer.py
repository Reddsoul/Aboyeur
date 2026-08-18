"""Greedy menu optimizer — maximizes ingredient/pantry overlap and macro fit.

Concept ported from the POC's pick_week()/backend.menu_engine.select_recipes()
(task.md §2 salvage list), rewritten clean against task.md §7's scoring shape:

    Score(candidate | selected) = w1*ingredient_overlap (meaningful ingredients
                                     only, UNIVERSAL_STAPLES excluded)
                                 + w2*pantry_overlap
                                 + w3*macro_fit
                                 - w4*same_book_penalty
                                 - w5*protein_repeat_penalty

Micronutrient gap-fill (task.md's w4 term in the spec numbering) is Phase 4
scope — no micro data exists until then, so it's simply not a term here
rather than a fake zero-weighted placeholder.

"Protein present (unless vegetarian mode)" is enforced as a heavy per-pick
penalty rather than a hard filter: a hard filter risks starving a candidate
pool that's genuinely vegetable-forward. "No dessert/drink as a meal" IS a
hard filter, since dropping those from a real recipe pool essentially never
empties it.
"""

from __future__ import annotations

from dataclasses import dataclass

W_INGREDIENT_OVERLAP = 1.0
W_PANTRY_OVERLAP = 2.0
W_MACRO_FIT = 3.0
W_SAME_BOOK_PENALTY = 2.0
W_PROTEIN_REPEAT_PENALTY = 1.5
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
MAX_DISHES_PER_MEAL = 2

# Ingredients that appear in nearly every recipe — not meaningful for overlap
# scoring. Using these to drive selection causes same-book/same-staples
# clustering instead of genuinely shared ingredients.
UNIVERSAL_STAPLES = {
    "salt", "pepper", "butter", "flour", "water", "oil", "olive oil", "vegetable oil",
    "sugar", "vinegar", "onion", "garlic", "eggs", "egg", "milk", "cream", "stock", "broth",
    "rice", "bread", "bread crumbs", "breadcrumbs", "bay leaf", "parsley", "thyme",
    "lemon juice", "lemon",
}

_DESSERT_TITLE_WORDS = {
    "pie", "cake", "pudding", "tart", "tarte", "waffle", "cookie", "biscuit",
    "pastry", "custard", "ice cream", "sorbet", "mousse", "brownie", "muffin",
    "scone", "donut", "charlotte", "compote", "strudel", "torte", "flan",
    "praline", "macaroon", "meringue",
}

_DRINK_TITLE_WORDS = {
    "smoothie", "cocktail", "mocktail", "juice", "milkshake",
    "sangria", "margarita", "daiquiri", "mojito", "lemonade",
    "latte", "cappuccino", "espresso", "iced tea", "bubble tea",
    "spritzer", "frappe", "hot toddy", "eggnog", "julep",
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


def is_dessert(title: str) -> bool:
    low = title.lower()
    return any(w in low for w in _DESSERT_TITLE_WORDS)


def is_drink(title: str) -> bool:
    low = title.lower()
    return any(w in low for w in _DRINK_TITLE_WORDS)


def has_protein(ingredients: list[str]) -> bool:
    joined = " ".join(i.lower() for i in ingredients)
    return any(p in joined for p in _PROTEIN_WORDS)


def main_protein(ingredients: list[str]) -> str | None:
    joined = " ".join(i.lower() for i in ingredients)
    for p in _MAIN_PROTEINS:
        if p in joined:
            return p
    return None


def meaningful_ingredients(ingredients: list[str]) -> set[str]:
    return {i.lower() for i in ingredients if i.lower() not in UNIVERSAL_STAPLES}


def macro_fit(macros: dict, target_kcal: float, target_macros: dict | None = None) -> float:
    """0..1, 1 = spot on target. 0.5 (neutral) if there's no target or no estimate.

    When target_macros ({protein_g, carb_g, fat_g}) is given and the candidate
    has macro data, the fit blends calorie closeness (60%) with macro closeness
    (40%) — macros are a keystone feature, not a tiebreaker.
    """
    kcal = macros.get("calories") or 0
    if not target_kcal or not kcal:
        return 0.5
    fit = max(0.0, 1.0 - abs(kcal - target_kcal) / target_kcal)
    if target_macros:
        macro_fits = []
        for key in ("protein_g", "carb_g", "fat_g"):
            t = target_macros.get(key) or 0
            v = macros.get(key)
            if t and v is not None:
                macro_fits.append(max(0.0, 1.0 - abs(float(v) - t) / t))
        if macro_fits:
            fit = 0.6 * fit + 0.4 * (sum(macro_fits) / len(macro_fits))
    return fit


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

    base_pool = [c for c in candidates if not is_dessert(c["title"]) and not is_drink(c["title"])]
    if len(base_pool) < total_picks:
        base_pool = list(candidates)  # never starve the pool over a soft rule

    if cuisine_affinity:
        cuisine_pool = [c for c in base_pool if (c.get("cuisine") or "").lower() in cuisine_affinity]
        if len(cuisine_pool) >= total_picks:
            base_pool = cuisine_pool

    def _greedy(overlap_weight: float) -> tuple[list[MenuPick], list[dict], dict[str, int], set[str]]:
        pool = list(base_pool)
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

    picks, daily_totals = _fill_daily_kcal(
        picks, leftover_pool, selected_meaningful,
        days=days, meals_per_day=meals_per_day, daily_target=daily_target,
        target_macros=target_macros, per_meal_kcal=target_kcal,
    )

    overlap_score = compute_overlap_score(picks)
    return {
        "picks": picks,
        "overlap_score": overlap_score,
        "overlap_floor_met": overlap_score >= MIN_OVERLAP_SCORE,
        "macro_fit_avg": round(sum(p.macro_fit for p in picks) / len(picks), 2) if picks else 0.0,
        "constraints_met": constraint_counts,
        "daily_totals": daily_totals,
    }


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

    pool = [
        c for c in pool
        if not is_dessert(c["title"]) and not is_drink(c["title"])
        and float(c.get("macros", {}).get("calories") or 0) > 0
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
            if not open_meals:
                break
            target_meal = min(open_meals, key=lambda m: (dish_counts[m], meal_kcal[m]))

            def top_up_score(c: dict) -> float:
                kcal = float(c["macros"]["calories"])
                closeness = max(0.0, 1.0 - abs(gap - kcal) / gap)
                overlap = len(meaningful_ingredients(c["ingredients"]) & selected_meaningful)
                return W_MACRO_FIT * closeness + W_INGREDIENT_OVERLAP * overlap

            best = max(pool, key=top_up_score)
            best_kcal = float(best["macros"]["calories"])
            # Only add a dish that moves the day closer to target — never
            # overshoot past what leaving it under would cost.
            if abs((day_kcal + best_kcal) - daily_target) >= abs(day_kcal - daily_target):
                break
            pool.remove(best)
            m_ings = meaningful_ingredients(best["ingredients"])
            shared = sorted(m_ings & selected_meaningful)
            day_picks.append(MenuPick(
                recipe_id=best["id"], title=best["title"], book=best["book"], path=best.get("path"),
                ingredients=best["ingredients"], shared_ingredients=shared,
                macro_fit=round(macro_fit(best.get("macros", {}), per_meal_kcal, target_macros), 2),
                pick_reason=f"top-up: day {day} was {round(gap)} kcal under target",
                day=day, meal=target_meal, role="top-up", kcal=best_kcal,
            ))
            selected_meaningful |= m_ings
            day_kcal += best_kcal

    all_picks = sorted(
        (p for dps in by_day.values() for p in dps),
        key=lambda p: (p.day, p.meal, p.role != "main"),
    )
    daily_totals = []
    for day in sorted(by_day):
        day_kcal = sum(p.kcal for p in by_day[day])
        if day_kcal <= 0 or not daily_target:
            status = "unknown"
        elif day_kcal < daily_target * (1 - DAILY_KCAL_TOLERANCE):
            status = "under"
        elif day_kcal > daily_target * (1 + DAILY_KCAL_TOLERANCE):
            status = "over"
        else:
            status = "ok"
        daily_totals.append({
            "day": day, "kcal": round(day_kcal),
            "target_kcal": round(daily_target), "status": status,
        })
    return all_picks, daily_totals
