"""Grocery-section categorization + shopping-list aggregation.

Ported from the POC's backend/utils.py (categorize_ingredient) and
backend/menu_engine.py (build_shopping_list), task.md §2 salvage list.
"""

from __future__ import annotations

import re

from aboyeur.engines.nutrition import fold_plural

_SECTION_KEYWORDS = {
    "protein": ["chicken", "beef", "pork", "lamb", "fish", "shrimp", "prawn", "tofu",
                "egg", "salmon", "tuna", "turkey", "bacon", "sausage", "duck", "crab",
                "scallop", "clam", "mussel", "anchovy", "ground", "mince", "steak",
                "tempeh", "seitan", "ham", "oyster"],
    "produce": ["garlic", "onion", "ginger", "scallion", "tomato", "potato", "carrot",
                "pepper", "chili", "chile", "cilantro", "parsley", "basil", "mint",
                "lettuce", "spinach", "cabbage", "cucumber", "lemon", "lime", "apple",
                "mushroom", "eggplant", "zucchini", "broccoli", "celery", "leek",
                "shallot", "lemongrass", "bok choy", "greens", "herb", "lime leaf",
                "coriander", "cucumbers", "bean sprout", "radish", "cauliflower",
                "squash", "corn", "avocado", "kale", "fennel", "asparagus"],
    "dairy": ["milk", "butter", "cream", "cheese", "yogurt", "yoghurt", "parmesan",
              "mozzarella", "ricotta", "mascarpone", "creme fraiche", "ghee", "feta"],
    "frozen": ["frozen", "ice cream", "peas frozen"],
    "spice": ["salt", "sugar", "pepper", "cumin", "coriander seed", "turmeric",
              "paprika", "cinnamon", "clove", "cardamom", "nutmeg", "star anise",
              "peppercorn", "sichuan pepper", "chili powder", "chile powder", "curry powder",
              "garam masala", "five spice", "bay leaf", "oregano", "thyme", "fennel seed",
              "mustard seed", "fenugreek", "saffron", "vanilla", "msg", "spice"],
    "pantry": ["oil", "vinegar", "soy sauce", "soy", "rice", "flour", "noodle",
               "pasta", "stock", "broth", "sauce", "paste", "doubanjiang", "miso",
               "sesame", "cornstarch", "starch", "wine", "sake", "mirin", "honey",
               "syrup", "bean", "lentil", "chickpea", "coconut milk", "tamarind",
               "fish sauce", "oyster sauce", "tomato paste", "canned", "tin", "dried",
               "nut", "almond", "peanut", "cashew", "tahini", "yeast", "baking",
               "chocolate", "cocoa", "gelatin", "tortilla", "bread", "panko", "breadcrumb"],
}

# Check spice/protein/dairy/produce before pantry (pantry is the catch-all bucket).
_SECTION_ORDER = ["spice", "protein", "dairy", "produce", "frozen", "pantry"]

_SECTIONS = ["produce", "protein", "dairy", "pantry", "spice", "frozen"]
# Public alias: the Macro Kit shopping list groups by the same sections in
# the same order, but aggregates components (already canonical names) rather
# than raw recipe lines, so it can't reuse build_shopping_list itself.
SECTIONS = _SECTIONS

# Word-boundary matching, not raw substring: "egg" must not match inside
# "eggplant", "corn" must not match inside "cornstarch".
_SECTION_PATTERNS = {
    section: [re.compile(r"\b" + re.escape(kw) + r"\b") for kw in kws]
    for section, kws in _SECTION_KEYWORDS.items()
}


def categorize_ingredient(name: str) -> str:
    """Map an ingredient name to a grocery section. Defaults to 'pantry'."""
    if not name:
        return "pantry"
    low = name.lower()
    for section in _SECTION_ORDER:
        for pat in _SECTION_PATTERNS[section]:
            if pat.search(low):
                return section
    return "pantry"


def build_shopping_list(
    picks: list[dict],
    pantry: set[str],
    dislikes: list[str] | None = None,
    canonical: dict[str, dict] | None = None,
) -> dict:
    """Aggregate ingredients across a menu's recipes, grouped by grocery section.

    picks: [{"ingredients": [...]}, ...] — any dict with an "ingredients" list.
    canonical: optional {raw_line_lowercased: {"food": str, "grams": float}}
    from nutrition.parse_ingredients — when given, raw lines aggregate under
    their canonical food ("irish potato", "boiled potato", "potatoes" all
    become one "potato" entry with summed grams) instead of one list item per
    distinct raw string. Without it, plural folding alone still merges
    "potato"/"potatoes". Pantry items are marked have=True; disliked
    ingredients are flagged for a sub.
    """
    dislikes = [d.lower().strip() for d in (dislikes or []) if d.strip()]
    pantry = {p.lower().strip() for p in pantry}
    canonical = canonical or {}

    agg: dict[str, dict] = {}
    for r in picks:
        recipe_foods: set[str] = set()  # count each recipe once per food
        for ing in r["ingredients"]:
            raw = ing.lower().strip()
            parsed = canonical.get(raw)
            food = fold_plural((parsed or {}).get("food") or raw)
            if not food:
                continue
            entry = agg.setdefault(food, {
                "used_in": 0, "grams": 0.0, "grams_known": False,
                "variants": set(), "section": categorize_ingredient(food),
            })
            if raw != food:
                entry["variants"].add(raw)
            grams = (parsed or {}).get("grams") or 0.0
            if grams > 0:
                entry["grams"] += float(grams)
                entry["grams_known"] = True
            if food not in recipe_foods:
                entry["used_in"] += 1
                recipe_foods.add(food)

    sections: dict[str, list] = {s: [] for s in _SECTIONS}
    for food, entry in sorted(agg.items(), key=lambda kv: (-kv[1]["used_in"], kv[0])):
        have = any(p in food or food in p for p in pantry)
        flagged = any(d in food or food in d for d in dislikes)
        item = {
            "name": food,
            "approx_grams": round(entry["grams"]) if entry["grams_known"] else None,
            "used_in_recipes": entry["used_in"],
            "have": have,
            "flag": "sub needed" if flagged else None,
        }
        if entry["variants"]:
            item["merged_from"] = sorted(entry["variants"])
        sections.setdefault(entry["section"], []).append(item)

    sections = {k: v for k, v in sections.items() if v}
    to_buy = sum(1 for s in sections.values() for it in s if not it["have"])
    return {"sections": sections, "total_items": len(agg), "to_buy": to_buy}
