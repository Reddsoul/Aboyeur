"""Ingredient-overlap menu optimizer + macro-aware portioning + shopping list.

Greedy selection: first pick = best combined score; each subsequent pick maximizes
ingredient overlap (and shared technique) with what's already chosen. Buy garlic once,
use it five times.
"""
from typing import Optional

from sqlalchemy.orm import Session

from backend import macro_engine
from backend.models import Recipe, Book, PantryItem
from backend.utils import categorize_ingredient

# First-pick scoring weights (task.md "Ingredient Overlap Optimizer").
W_PANTRY = 0.5
W_PREFERENCE = 0.3
W_VARIETY = 0.2

TECHNIQUE_OVERLAP_BONUS = 5  # Phase 3.1: shared technique = shared prep/equipment.


# ---------------------------------------------------------------------------
# Candidate pool
# ---------------------------------------------------------------------------

def _recipe_dict(r: Recipe, book_cuisine: dict, score: float = 0.0) -> dict:
    return {
        "id": r.id,
        "book_id": r.book_id,
        "title": r.title,
        "ingredients": [i.lower() for i in (r.ingredients or [])],
        "technique": r.technique or "",
        "cuisine": book_cuisine.get(r.book_id, []),
        "macros": {
            "calories": r.calories or 0,
            "protein_g": r.protein_g or 0,
            "carb_g": r.carb_g or 0,
            "fat_g": r.fat_g or 0,
        },
        "semantic_score": score,
    }


def candidate_pool(db: Session, query: str, pool_size: int = 60,
                   filters: Optional[dict] = None) -> list[dict]:
    """Build a candidate list of recipe dicts ranked by semantic similarity.

    Uses ChromaDB for ranking, then hydrates authoritative ingredient/macro data
    from SQLite. Falls back to plain SQLite ordering if Chroma is unavailable.
    """
    book_cuisine = {b.id: (b.cuisine or []) for b in db.query(Book).all()}
    ranked_ids: list[str] = []
    scores: dict[str, float] = {}

    try:
        from backend import rag
        hits = rag.search(query or "dinner", n=pool_size, filters=filters)
        for h in hits:
            ranked_ids.append(h["id"])
            scores[h["id"]] = h.get("score", 0.0)
    except Exception:
        ranked_ids = []

    if ranked_ids:
        rows = {r.id: r for r in db.query(Recipe).filter(Recipe.id.in_(ranked_ids)).all()}
        out = [_recipe_dict(rows[i], book_cuisine, scores.get(i, 0.0))
               for i in ranked_ids if i in rows]
        if out:
            return out

    # Fallback: no vector store — just take recipes from SQLite.
    rows = db.query(Recipe).limit(pool_size).all()
    return [_recipe_dict(r, book_cuisine) for r in rows]


# ---------------------------------------------------------------------------
# Greedy overlap selection
# ---------------------------------------------------------------------------

def _pantry_set(db: Session) -> set:
    return {p.name.lower().strip() for p in db.query(PantryItem).all()}


def _pantry_overlap(ingredients: list[str], pantry: set) -> float:
    if not ingredients:
        return 0.0
    hit = sum(1 for ing in ingredients if any(p in ing or ing in p for p in pantry))
    return hit / len(ingredients)


def _ingredient_overlap(ingredients: list[str], selected_ings: set) -> int:
    return sum(1 for ing in ingredients if ing in selected_ings)


def select_recipes(candidates: list[dict], pantry: set, days: int) -> list[dict]:
    """Greedy: best first pick, then maximize overlap with the running selection."""
    if not candidates:
        return []
    pool = list(candidates)
    selected: list[dict] = []
    selected_ings: set = set()
    used_cuisines: list = []

    # First pick — combined score.
    def first_score(c):
        pantry_s = _pantry_overlap(c["ingredients"], pantry)
        pref_s = c.get("semantic_score", 0.0)
        variety_s = 1.0  # nothing picked yet
        return W_PANTRY * pantry_s + W_PREFERENCE * pref_s + W_VARIETY * variety_s

    pool.sort(key=first_score, reverse=True)
    first = pool.pop(0)
    selected.append({**first, "shared_ingredients": [], "pick_reason": "best combined score"})
    selected_ings.update(first["ingredients"])
    used_cuisines.extend(first["cuisine"])

    # Subsequent picks — maximize overlap + technique bonus, mild variety penalty.
    selected_techniques = {first["technique"]} if first["technique"] else set()
    while len(selected) < days and pool:
        def overlap_score(c):
            ov = _ingredient_overlap(c["ingredients"], selected_ings)
            tech_bonus = (TECHNIQUE_OVERLAP_BONUS / 5.0) if c["technique"] in selected_techniques and c["technique"] else 0
            variety_pen = 0.5 if any(cu in used_cuisines for cu in c["cuisine"]) else 0
            return ov + tech_bonus - variety_pen + 0.3 * c.get("semantic_score", 0.0)

        pool.sort(key=overlap_score, reverse=True)
        nxt = pool.pop(0)
        shared = sorted(set(nxt["ingredients"]) & selected_ings)
        selected.append({**nxt, "shared_ingredients": shared,
                         "pick_reason": f"shares {len(shared)} ingredients"})
        selected_ings.update(nxt["ingredients"])
        used_cuisines.extend(nxt["cuisine"])
        if nxt["technique"]:
            selected_techniques.add(nxt["technique"])

    return selected


def compute_overlap_score(selected: list[dict]) -> int:
    """0–100. Higher = more ingredient (and technique) sharing across the menu."""
    if len(selected) < 2:
        return 0
    all_slots = sum(len(r["ingredients"]) for r in selected)
    unique = len({ing for r in selected for ing in r["ingredients"]})
    if all_slots == 0:
        return 0
    base = (1.0 - unique / all_slots) * 100.0

    # Technique-sharing bonus: +5 per recipe that reuses an earlier technique.
    seen_tech, bonus = set(), 0
    for r in selected:
        t = r["technique"]
        if t and t in seen_tech:
            bonus += TECHNIQUE_OVERLAP_BONUS
        if t:
            seen_tech.add(t)

    return max(0, min(100, round(base + bonus)))


# ---------------------------------------------------------------------------
# Portioning
# ---------------------------------------------------------------------------

def _disliked(name: str, dislikes: list[str]) -> bool:
    low = name.lower()
    return any(d and (d in low or low in d) for d in dislikes)


def portion_recipe(recipe: dict, targets: dict) -> dict:
    """Scale one recipe to each household member's per-meal kcal target.

    Returns per_person breakdown + a household total. Flags ±15% misses.
    """
    base = recipe["macros"]
    per_person = {}
    total_kcal = 0.0
    for mid, t in targets.items():
        target_kcal = t["per_meal"]["kcal"]
        scaled = macro_engine.scale_recipe(base, target_kcal)
        flag = macro_engine.flag_off_target(scaled["calories"], target_kcal)
        per_person[mid] = {**scaled, "label": t["label"],
                           "target_kcal": target_kcal, "flag": flag}
        total_kcal += scaled["calories"]
    return {"per_person": per_person, "household_kcal": round(total_kcal, 1)}


# ---------------------------------------------------------------------------
# Shopping list — section-grouped, pantry-subtracted, sub flags
# ---------------------------------------------------------------------------

def build_shopping_list(selected: list[dict], pantry: set, dislikes: list[str]) -> dict:
    """Aggregate ingredients across the menu, grouped by grocery section.

    Pantry items are marked have=True; disliked ingredients get [sub needed].
    """
    agg: dict[str, dict] = {}
    for r in selected:
        for ing in r["ingredients"]:
            entry = agg.setdefault(ing, {"name": ing, "used_in": 0,
                                         "section": categorize_ingredient(ing)})
            entry["used_in"] += 1

    sections: dict[str, list] = {s: [] for s in
                                 ["produce", "protein", "dairy", "pantry", "spice", "frozen"]}
    for ing, entry in sorted(agg.items(), key=lambda kv: (-kv[1]["used_in"], kv[0])):
        have = any(p in ing or ing in p for p in pantry)
        item = {
            "name": ing,
            "used_in_recipes": entry["used_in"],
            "have": have,
            "flag": "[sub needed]" if _disliked(ing, dislikes) else "",
        }
        sections.setdefault(entry["section"], []).append(item)

    # Drop empty sections for a clean payload.
    sections = {k: v for k, v in sections.items() if v}
    to_buy = sum(1 for s in sections.values() for it in s if not it["have"])
    return {"sections": sections, "total_items": len(agg), "to_buy": to_buy}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def generate_menu(db: Session, query: str, days: int, profile: dict,
                  filters: Optional[dict] = None) -> dict:
    """Full menu plan. Returns curated_recipes, overlap_score, shopping_list, targets.

    (fusion_concepts are merged in by the caller via fusion_engine.)
    """
    targets = macro_engine.household_targets(profile)
    pantry = _pantry_set(db)
    dislikes = [d.lower() for d in profile.get("preferences", {}).get("dislikes", [])]

    candidates = candidate_pool(db, query, pool_size=max(40, days * 8), filters=filters)
    selected = select_recipes(candidates, pantry, days)

    curated = []
    for i, r in enumerate(selected):
        portions = portion_recipe(r, targets)
        curated.append({
            "label": "[Curated Recipe]",
            "day": i + 1,
            "recipe_id": r["id"],
            "title": r["title"],
            "book_id": r["book_id"],
            "cuisine": r["cuisine"],
            "technique": r["technique"],
            "ingredients": r["ingredients"],
            "shared_ingredients": r.get("shared_ingredients", []),
            "base_macros": r["macros"],
            "portions": portions,
            "pick_reason": r.get("pick_reason", ""),
        })

    return {
        "curated_recipes": curated,
        "overlap_score": compute_overlap_score(selected),
        "shopping_list": build_shopping_list(selected, pantry, dislikes),
        "targets": targets,
    }
