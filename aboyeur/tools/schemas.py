"""OpenAI-style JSON schemas for the 9 tools, task.md §5.

Kept separate from the tool implementations: the schema is what the LLM
sees (names, descriptions, parameter shapes) and is tuned for a mid-size
local model, while the Python functions in the other tools/ modules are
what actually runs. Descriptions are deliberately explicit about the
allergy/verbatim/no-repeat rules — a mid-size model won't infer them.
"""

from __future__ import annotations

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_recipes",
            "description": (
                "Search the recipe library by keyword. Full-text search over title, "
                "ingredients, tags, cuisine, book. Allergens listed in the user's profile "
                "are already hard-excluded from results — you never need to filter them "
                "yourself. Returns at most 25 compact results (id, title, book, cuisine, "
                "first 6 ingredients, pantry-match flag). Call this multiple times with "
                "different queries to build a larger candidate pool for plan_menu."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords, e.g. 'chicken curry' or 'chocolate cake'."},
                    "cuisine": {"type": "string", "description": "Optional exact cuisine filter, e.g. 'Indian'."},
                    "book": {"type": "string", "description": "Optional exact source cookbook filter."},
                    "include_ingredients": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Only return recipes containing ALL of these ingredients.",
                    },
                    "exclude_ingredients": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Soft exclusion, e.g. a disliked ingredient (not allergies — those are automatic).",
                    },
                    "limit": {"type": "integer", "description": "Max results, capped at 25. Default 25."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recipe",
            "description": (
                "Fetch one recipe's full, verbatim markdown (ingredients with quantities, "
                "instructions, notes) by id. Never paraphrase or rewrite what this returns — "
                "show it as-is. If scale_to_servings is given, you also get a scale "
                "multiplier (quantities are NOT auto-rewritten yet; mention the multiplier "
                "to the user instead of doing the math yourself)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Recipe id from a prior search_recipes/plan_menu result."},
                    "scale_to_servings": {"type": "integer", "description": "Optional target serving count."},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_profile",
            "description": (
                "Get the household: members, computed per-member daily/per-meal calorie and "
                "macro targets (BMR/TDEE-based), allergies, dislikes, likes, cuisine affinity, "
                "diet modes. Always check this before planning a menu — it drives targets and "
                "the hard allergy filter."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_profile",
            "description": (
                "Update the household profile. Use add_allergies whenever the user mentions "
                "an allergy — do this immediately, don't wait to be asked. Add/remove fields "
                "take lists and merge (they don't replace the existing list); set_* fields "
                "replace outright."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "add_allergies": {"type": "array", "items": {"type": "string"}},
                    "remove_allergies": {"type": "array", "items": {"type": "string"}},
                    "add_dislikes": {"type": "array", "items": {"type": "string"}},
                    "remove_dislikes": {"type": "array", "items": {"type": "string"}},
                    "add_likes": {"type": "array", "items": {"type": "string"}},
                    "remove_likes": {"type": "array", "items": {"type": "string"}},
                    "set_diet_modes": {
                        "type": "array", "items": {"type": "string"},
                        "description": "e.g. ['vegetarian'], ['keto']. Replaces the existing list.",
                    },
                    "set_cuisine_affinity": {"type": "array", "items": {"type": "string"}},
                    "set_meals_per_day": {"type": "integer"},
                    "add_member": {
                        "type": "object",
                        "description": (
                            "Household member: {id, label, sex, age, height_cm, weight_kg, "
                            "activity, daily_kcal}. id required; anthropometrics optional "
                            "(daily_kcal is the fallback target if they're omitted)."
                        ),
                    },
                    "remove_member": {"type": "string", "description": "Member id to remove."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_pantry",
            "description": "List, add to, remove from, or clear the pantry (what's already on hand).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "remove", "clear"]},
                    "items": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Ingredient names, for add/remove.",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estimate_nutrition",
            "description": (
                "Get per-serving calories and macros for one or more recipes (USDA-backed, "
                "cached). Each result includes a confidence flag — 'low' means the estimate "
                "is unreliable (say so if you report it); 'medium' means it resolved via USDA."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "recipe_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["recipe_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_menu",
            "description": (
                "Turn a candidate pool of recipe ids into an optimized day-by-day menu. "
                "Maximizes shared ingredients (fewer grocery items) and fit against the "
                "household's calorie targets; never repeats a recipe; keeps desserts out of "
                "the dinner slots. Pass at least 2x as many candidate_ids as days — call "
                "search_recipes a few times first to build that pool. Returns per-day picks "
                "with an overlap score and macro fit you should mention when presenting the plan."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_ids": {"type": "array", "items": {"type": "integer"}},
                    "days": {"type": "integer", "description": "Number of dinners to plan, e.g. 7."},
                    "meals_per_day": {"type": "integer", "description": "Default 1 (dinner-only planning)."},
                    "constraints": {
                        "type": "object",
                        "description": (
                            "Optional keyword->minimum-count nudges, e.g. {'fish': 2} for "
                            "'2 fish nights'. Best-effort, not a hard guarantee."
                        ),
                    },
                },
                "required": ["candidate_ids", "days"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "swap_menu_item",
            "description": (
                "Replace one day's recipe in a menu with a different recipe id, and get the "
                "re-scored plan back. menu_id is the id plan_menu (or a prior swap_menu_item) "
                "returned — omit it to act on the most recently planned menu."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "menu_id": {"type": "integer", "description": "Omit to use the most recent menu."},
                    "day": {"type": "integer", "description": "Which day (1-indexed) to replace."},
                    "replacement_id": {"type": "integer"},
                },
                "required": ["day", "replacement_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_shopping_list",
            "description": (
                "Build the consolidated grocery list for a menu, grouped by store section "
                "(produce, protein, dairy, pantry, spice, frozen). Pantry items already on "
                "hand are marked have=true rather than removed, so you can show what's needed "
                "vs. what's already there. Omit menu_id to use the most recently planned menu."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "menu_id": {"type": "integer", "description": "Omit to use the most recent menu."},
                },
            },
        },
    },
]

TOOL_NAMES = [t["function"]["name"] for t in TOOL_SCHEMAS]
