"""Allergen category expansion.

A user says a category ("shellfish", "tree nuts"); ingredient lists say the
specific thing ("shrimp", "octopus", "almond"). A literal substring match on
the category word alone catches almost nothing — no recipe's ingredients_json
contains the literal word "shellfish", so filtering on it as typed is close
to a no-op despite looking like it works (found this the hard way: a live
"I'm allergic to shellfish" test let shrimp tempura and multiple octopus
dishes straight through the filter).

This list covers the common named categories a real person says out loud —
the standard "Big 9"-style allergen groups — not a full ontology. Anything
not in here (a specific ingredient name like "peanut" or "egg") already
works fine as a literal substring match without expansion.
"""

from __future__ import annotations

ALLERGEN_ALIASES: dict[str, list[str]] = {
    "shellfish": [
        "shrimp", "prawn", "crab", "lobster", "crayfish", "crawfish",
        "clam", "mussel", "oyster", "scallop", "squid", "octopus", "snail", "escargot",
    ],
    "crustacean": ["shrimp", "prawn", "crab", "lobster", "crayfish", "crawfish"],
    "mollusk": ["clam", "mussel", "oyster", "scallop", "squid", "octopus", "snail"],
    "tree nut": [
        "almond", "walnut", "cashew", "pecan", "pistachio", "hazelnut",
        "macadamia", "brazil nut", "pine nut", "chestnut",
    ],
    "tree nuts": [
        "almond", "walnut", "cashew", "pecan", "pistachio", "hazelnut",
        "macadamia", "brazil nut", "pine nut", "chestnut",
    ],
    "nuts": [
        "almond", "walnut", "cashew", "pecan", "pistachio", "hazelnut",
        "macadamia", "brazil nut", "pine nut", "chestnut", "peanut",
    ],
    "dairy": ["milk", "cheese", "butter", "cream", "yogurt", "yoghurt", "ghee", "whey", "casein", "buttermilk"],
    "milk": ["milk", "cheese", "butter", "cream", "yogurt", "yoghurt", "ghee", "whey", "casein", "buttermilk"],
    "gluten": ["flour", "bread", "pasta", "wheat", "barley", "rye", "noodle", "breadcrumb", "panko", "couscous", "semolina"],
    "wheat": ["flour", "bread", "pasta", "wheat", "noodle", "breadcrumb", "panko", "couscous", "semolina"],
    "soy": ["soy sauce", "tofu", "edamame", "miso", "soybean", "tempeh"],
    "fish": ["salmon", "tuna", "cod", "anchovy", "halibut", "trout", "sardine", "mackerel", "bass", "tilapia", "herring"],
}


def expand(term: str) -> list[str]:
    """The literal term plus any known category expansion, all lowercased."""
    key = term.lower().strip()
    return [key] + ALLERGEN_ALIASES.get(key, [])


def expand_all(terms: list[str]) -> list[str]:
    out: list[str] = []
    for t in terms:
        out.extend(expand(t))
    return out
