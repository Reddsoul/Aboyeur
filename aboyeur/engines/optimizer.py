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
pool that's genuinely vegetable-forward. "No dessert as dinner" IS a hard
filter, since dropping desserts from a real recipe pool essentially never
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
    ingredients: list[str]
    shared_ingredients: list[str]
    macro_fit: float
    pick_reason: str


def is_dessert(title: str) -> bool:
    low = title.lower()
    return any(w in low for w in _DESSERT_TITLE_WORDS)


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


def macro_fit(macros: dict, target_kcal: float) -> float:
    """0..1, 1 = spot on target. 0.5 (neutral) if there's no target or no estimate."""
    kcal = macros.get("calories") or 0
    if not target_kcal or not kcal:
        return 0.5
    return max(0.0, 1.0 - abs(kcal - target_kcal) / target_kcal)


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
) -> dict:
    """Greedy selection: `days` picks, each maximizing overlap+pantry+macro fit
    against what's already chosen, minus same-book and protein-repeat penalties.

    candidates: [{id, title, book, ingredients: [...], macros: {"calories": ...}}]
    constraints: optional {keyword: min_count} the caller wants honored,
                 e.g. {"fish": 2} for "2 fish nights" — a scoring nudge, not a hard
                 requirement (never fails the run if under-satisfied).
    """
    diet_modes = diet_modes or []
    vegetarian = "vegetarian" in diet_modes or "vegan" in diet_modes
    constraints = constraints or {}

    pool = [c for c in candidates if not is_dessert(c["title"])]
    if len(pool) < days:
        pool = list(candidates)  # never starve the pool over a soft rule

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
        macro_s = macro_fit(c.get("macros", {}), target_kcal)
        book_pen = book_counts.get(c["book"], 0)
        mp = main_protein(ings)
        protein_pen = protein_counts.get(mp, 0) if mp else 0
        missing_protein_pen = 0.0 if vegetarian or has_protein(ings) else PROTEIN_MISSING_PENALTY

        s = (W_INGREDIENT_OVERLAP * overlap
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

    for _ in range(min(days, len(pool))):
        pool.sort(key=score, reverse=True)
        best = pool.pop(0)

        m_ings = meaningful_ingredients(best["ingredients"])
        shared = sorted(m_ings & selected_meaningful)
        reason = "best pantry/macro fit" if not picks else f"shares {len(shared)} ingredients"
        picks.append(MenuPick(
            recipe_id=best["id"], title=best["title"], book=best["book"],
            ingredients=best["ingredients"], shared_ingredients=shared,
            macro_fit=round(macro_fit(best.get("macros", {}), target_kcal), 2),
            pick_reason=reason,
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

    return {
        "picks": picks,
        "overlap_score": compute_overlap_score(picks),
        "macro_fit_avg": round(sum(p.macro_fit for p in picks) / len(picks), 2) if picks else 0.0,
        "constraints_met": constraint_counts,
    }
