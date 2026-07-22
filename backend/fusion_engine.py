"""LLM-generated fusion concepts that bridge cuisines / fill nutritional gaps.

Pipeline: extract overlap -> LLM flavor-profile summary -> LLM concept generation ->
Python validates every ingredient (in_library + specialty_source flags) and estimates
nutrition with the macro engine. RAG recipes are real; these are clearly labeled concepts.
"""
import json
from collections import Counter
from typing import Optional

from backend import llm, macro_engine

# Common global staples — present in any kitchen, never "specialty".
_STAPLES = {
    "salt", "sugar", "water", "oil", "olive oil", "vegetable oil", "pepper",
    "black pepper", "garlic", "onion", "egg", "eggs", "butter", "flour", "rice",
    "soy sauce", "vinegar", "ginger", "scallion", "stock", "broth",
}


def _overlap_summary(top_recipes: list[dict]) -> dict:
    ing_counter = Counter()
    tech_counter = Counter()
    cuisines = set()
    for r in top_recipes:
        for ing in r.get("ingredients", []):
            ing_counter[ing.lower()] += 1
        if r.get("technique"):
            tech_counter[r["technique"]] += 1
        for c in (r.get("cuisine") or []):
            cuisines.add(c)
    shared = [ing for ing, c in ing_counter.items() if c > 1]
    return {
        "shared_ingredients": shared or [i for i, _ in ing_counter.most_common(8)],
        "techniques": [t for t, _ in tech_counter.most_common()],
        "cuisines": sorted(cuisines),
    }


def _validate_ingredient(name: str, library: set) -> dict:
    low = name.lower().strip()
    in_lib = low in library or any(low in l or l in low for l in library)
    return {
        "name": low,
        "in_library": bool(in_lib),
        "specialty_source": (not in_lib) and (low not in _STAPLES),
    }


def generate_fusion_concepts(top_recipes: list[dict], n: int = 2,
                             library_ingredients: Optional[set] = None,
                             fusion_tolerance: str = "medium") -> list[dict]:
    """Return up to `n` validated, clearly-labeled fusion concepts.

    Each: {label, title, rationale, ingredients[{name,in_library,specialty_source}],
           steps, flavor_profile, nutrition_estimate}.
    """
    if not top_recipes:
        return []
    library = {i.lower() for i in (library_ingredients or set())}
    overlap = _overlap_summary(top_recipes)

    # 1) Flavor-profile summary (structured).
    flavor = llm.chat_json([
        {"role": "system", "content":
            "Summarize a flavor profile as JSON only: "
            '{"dominant":[...],"aromatics":[...],"texture":[...],"notes":"..."}.'},
        {"role": "user", "content":
            f"Shared ingredients: {overlap['shared_ingredients']}\n"
            f"Techniques: {overlap['techniques']}\nCuisines: {overlap['cuisines']}"},
    ])

    # 2) Concept generation.
    concepts_raw = llm.chat_json([
        {"role": "system", "content":
            f"You invent fusion dinner concepts (tolerance={fusion_tolerance}). "
            f"Bridge the cuisines or fill nutritional gaps using mostly the shared "
            f"ingredients. Output ONLY JSON: {{\"concepts\":[{{\"title\":\"\",\"rationale\":\"\","
            f"\"ingredients\":[\"name\"],\"steps\":[\"\"],\"flavor_profile\":\"\"}}]}}. "
            f"Produce exactly {n} concepts."},
        {"role": "user", "content":
            f"Flavor profile: {json.dumps(flavor)}\n"
            f"Shared ingredients: {overlap['shared_ingredients']}\n"
            f"Techniques: {overlap['techniques']}\nCuisines: {overlap['cuisines']}"},
    ])

    concepts = concepts_raw.get("concepts", []) if isinstance(concepts_raw, dict) else []
    out = []
    for c in concepts[:n]:
        raw_ings = c.get("ingredients", []) or []
        validated = [_validate_ingredient(str(i), library) for i in raw_ings]
        nutrition = macro_engine.estimate_recipe_macros(
            [v["name"] for v in validated], servings=4)
        out.append({
            "label": "[Fusion Concept]",
            "title": c.get("title", "Untitled Fusion"),
            "rationale": c.get("rationale", ""),
            "ingredients": validated,
            "steps": c.get("steps", []),
            "flavor_profile": c.get("flavor_profile", flavor),
            "nutrition_estimate": nutrition,
        })
    return out
