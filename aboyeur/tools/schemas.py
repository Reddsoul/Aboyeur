"""OpenAI-style JSON schemas for the LLM's tools, task.md §5.

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
                "yourself. Returns at most 25 compact results (id, title, book, cuisine, course, "
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
                    "for_menu": {
                        "type": "boolean",
                        "description": (
                            "Set true when building a candidate pool for plan_menu — drops "
                            "recipes classified as dessert, drink, or condiment/sauce so the "
                            "pool is real meals. Leave false/unset for general recipe search."
                        ),
                    },
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
                "diet modes. Each member's per_meal target already accounts for their own "
                "meals_per_day if they have a personal override (falls back to the household "
                "default otherwise) — don't assume every member eats on the same schedule. "
                "Always check this before planning a menu — it drives targets and the hard "
                "allergy filter."
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
                "an allergy — do this immediately, don't wait to be asked; same for any stated "
                "personal fact (height, weight, age, sex, diet) via add_member. Store metric: "
                "convert feet/inches to height_cm and pounds to weight_kg before saving. "
                "Add/remove fields take lists and merge (they don't replace the existing "
                "list); set_* fields replace outright. set_meals_per_day is the household-wide "
                "default — if only ONE person is changing their eating pattern (e.g. going "
                "OMAD), do NOT use set_meals_per_day, use add_member with that person's id and "
                "meals_per_day instead so the rest of the household isn't affected."
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
                    "set_meals_per_day": {
                        "type": "integer",
                        "description": (
                            "Household-wide default meals/day. Only use this when the change "
                            "applies to everyone; for one person use add_member instead."
                        ),
                    },
                    "set_macro_strategy": {
                        "type": "string", "enum": ["balanced", "endurance", "cut"],
                        "description": (
                            "How to split calories into protein/carb/fat. Set this in the "
                            "same turn the user states a sport or a body-composition goal: "
                            "'endurance' for running, cycling, rowing, swimming or race "
                            "training; 'cut' when they state a fat-loss goal; 'balanced' "
                            "otherwise (including general lifting). Used by build_macro_kit; "
                            "it does not change existing menu targets."
                        ),
                    },
                    "add_member": {
                        "type": "object",
                        "description": (
                            "Household member: {id, label, sex, age, height_cm, weight_kg, "
                            "activity, daily_kcal, meals_per_day, goal, goal_weight_kg}. "
                            "goal is one of 'lose'/'maintain'/'gain' — set it whenever the "
                            "user states a weight goal; goal_weight_kg is their target "
                            "weight (convert lbs to kg). The calorie deficit/surplus is "
                            "computed from goal + goal_weight_kg automatically — never set "
                            "daily_kcal to hand-build a deficit. id required; every other "
                            "field is optional and merges onto that member's existing record "
                            "(omitted fields are kept as-is, not cleared) — so you can patch "
                            "just meals_per_day for one person, e.g. {'id': 'sam', "
                            "'meals_per_day': 1} to put only Sam on OMAD, without needing to "
                            "resend their anthropometrics. Omit meals_per_day to fall back to "
                            "the household default."
                        ),
                    },
                    "remove_member": {"type": "string", "description": "Member id to remove."},
                    "set_units": {
                        "type": "string", "enum": ["metric", "imperial"],
                        "description": "Display unit preference. Storage is always metric.",
                    },
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
                "Turn a candidate pool of recipe ids into an optimized menu — ONE call plans "
                "every meal for every day (days x meals_per_day total picks); it does NOT need "
                "to be called once per meal, that internal looping already happens for you. "
                "Meets the client's calorie AND protein/carb/fat targets first, then maximizes "
                "shared ingredients (fewer grocery items); never repeats a recipe within a day; "
                "keeps desserts, drinks and bare condiments/sauces out of the picks entirely; "
                "hard-filters to the household's cuisine_affinity when set. "
                "Pass at least 2x as many candidate_ids as (days x meals_per_day) — call "
                "search_recipes a few times first to build that pool (batch multiple "
                "search_recipes calls into one turn when you can, e.g. one per cuisine, instead "
                "of one turn per call — every extra turn is a slow round-trip to a local model). "
                "Enforces a minimum ingredient-overlap floor (25/100) and tops up under-fed "
                "days with a second dish per meal (role='top-up') so each day's total kcal "
                "lands within 15% of the household's daily target — a meal entry can therefore "
                "have two dishes; present both. Returns picks grouped by day and meal plus "
                "daily_totals (per-day kcal/protein/carb/fat vs target — each day carries a kcal "
                "status of under/ok/over/unknown AND a macro_status of ok/off/unknown with "
                "gap_notes), an overlap score, and macro fit — mention daily totals, each day's "
                "macro_status, and any 'unknown' days (missing nutrition data) when presenting "
                "the plan. Days the candidate pool can't bring on target are re-planned "
                "automatically from the whole library (favoring dishes that share ingredients "
                "with the rest of the plan), so every requested day always comes back; a day "
                "that is still off after that is delivered flagged (macro_status 'off', "
                "gap_notes, repair_notes) — present it, don't hide it. Only when nothing "
                "eligible exists to fill a slot does it return {'error': 'cannot_build', "
                "'reasons': [...], 'suggest': 'macro_kit'} and persist nothing. "
                "The result carries summary_markdown: the finished "
                "day-by-day proposal table, rendered in code with the correct day numbers — "
                "show it verbatim, never rebuild it from the numbers. "
                "TO EXTEND a plan already shown ('the rest of the week', 'the remaining 2 "
                "days', 'add 3 more days'): pass extend_menu_id = that plan's menu_id and set "
                "days to the number of ADDITIONAL days. The new days are numbered after the "
                "existing ones (a 5-day menu + 2 becomes days 1-7), use no dish already in "
                "the plan, and are saved to the same menu_id — do NOT start a fresh plan for "
                "this; it would restart at day 1 and be unrelated to what was shown."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_ids": {"type": "array", "items": {"type": "integer"}},
                    "days": {"type": "integer", "description": "Number of days to plan, e.g. 7."},
                    "meals_per_day": {
                        "type": "integer",
                        "description": (
                            "How many meals per day to plan for, e.g. 3 for breakfast/lunch/"
                            "dinner. Default 1. Handled in one pass — do not call plan_menu "
                            "multiple times to cover this."
                        ),
                    },
                    "constraints": {
                        "type": "object",
                        "description": (
                            "Optional keyword->minimum-count nudges, e.g. {'fish': 2} for "
                            "'2 fish nights'. Best-effort, not a hard guarantee."
                        ),
                    },
                    "extend_menu_id": {
                        "type": "integer",
                        "description": (
                            "Continue an existing draft menu instead of starting a new one: "
                            "the menu_id of the plan already shown. `days` then means how "
                            "many days to ADD (numbered after the plan's last day), reusing "
                            "that menu's meals_per_day and targets. Omit for a new plan."
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
            "name": "build_macro_kit",
            "description": (
                "Build a Macro Kit: a mix-and-match GROCERY TEMPLATE — a short list of "
                "whole foods per column (protein, carb base, volume vegetables, fat, "
                "sauce) plus a per-meal gram rule, so the user assembles a bowl and hits "
                "their macros without following a recipe or counting. "
                "USE THIS when the user wants foods/groceries they can combine or mix and "
                "match, 'a combo of groceries to hit my macros', 'what should I buy to hit "
                "my protein', a training/cutting food list, or meal-prep components. "
                "DO NOT use plan_menu for that — plan_menu picks specific cookbook recipes "
                "for specific days, which is a different request. Conversely, if they want "
                "actual dishes/recipes for the week, use search_recipes + plan_menu, not "
                "this. Do not call search_recipes first; a kit does not come from the "
                "recipe library. ONE call returns everything: the columns with per-serving "
                "macros, the assembly rule (grams per column per meal), example bowls with "
                "their macros, and the daily total vs target with an ok/under/over status. "
                "All weights are RAW / as-purchased (dry rice and oats, raw chicken). "
                "Call get_profile first so weight, diet and allergies are current, and "
                "update_profile in the same turn if the user just stated a sport, goal or "
                "bodyweight. Afterwards offer get_shopping_list with the returned kit_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "meals_per_day": {
                        "type": "integer",
                        "description": (
                            "Meals the grams are divided across, e.g. 3. Omit to use the "
                            "member's own meals_per_day, else the household default."
                        ),
                    },
                    "days": {
                        "type": "integer",
                        "description": (
                            "How many days of groceries the shopping list should cover. "
                            "Default 7. Does not change the assembly rule — a kit is one "
                            "template, not a day-by-day plan."
                        ),
                    },
                    "emphasis": {
                        "type": "string", "enum": ["balanced", "endurance", "cut"],
                        "description": (
                            "Macro strategy. 'endurance' = carb-forward with protein set "
                            "per kg bodyweight (running, cycling, rowing, marathon "
                            "training); 'cut' = high protein, lower calorie, high-volume "
                            "foods (fat loss); 'balanced' otherwise. Omit to use the "
                            "profile's macro_strategy."
                        ),
                    },
                    "kit_size": {
                        "type": "object",
                        "description": (
                            "Optional per-column item counts, e.g. {'protein': 4, "
                            "'carb_base': 2}. Keys are the column names. Defaults are "
                            "sensible — only pass this if the user asks for more or fewer "
                            "options."
                        ),
                    },
                    "include_foods": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Foods the user specifically asked for, e.g. ['oats', 'salmon'].",
                    },
                    "exclude_foods": {
                        "type": "array", "items": {"type": "string"},
                        "description": (
                            "Foods to leave out. Not for allergies — those come from the "
                            "profile automatically and are already excluded before you see "
                            "the kit."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "swap_menu_item",
            "description": (
                "Replace one meal's recipe in a menu with a different recipe id, and get the "
                "re-scored plan back. menu_id is the id plan_menu (or a prior swap_menu_item) "
                "returned — omit it to act on the most recently planned menu."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "menu_id": {"type": "integer", "description": "Omit to use the most recent menu."},
                    "day": {"type": "integer", "description": "Which day (1-indexed) to replace."},
                    "meal": {
                        "type": "integer",
                        "description": (
                            "Which meal within that day (1-indexed), only needed when the plan "
                            "has meals_per_day > 1. Default 1."
                        ),
                    },
                    "replacement_id": {"type": "integer"},
                },
                "required": ["day", "replacement_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_menu_recipes",
            "description": (
                "Fetch EVERY recipe in a planned menu as one markdown document, already "
                "in day-then-meal order with a heading per slot (day, meal, dish title, "
                "portion note). Use this for STEP 2 delivery after the user confirms a "
                "plan — ONE call replaces calling get_recipe once per slot, and the "
                "ordering/labeling is done for you. Show the returned markdown verbatim; "
                "do NOT reorder, relabel, or paraphrase it. Omit menu_id to use the most "
                "recently planned menu."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "menu_id": {"type": "integer", "description": "Omit to use the most recent menu."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_shopping_list",
            "description": (
                "Build the consolidated grocery list for a menu, grouped by store section "
                "(produce, protein, dairy, pantry, spice, frozen). Ingredient variants are "
                "already merged into one canonical item with total approx_grams and a "
                "used_in_recipes count (e.g. 'potatoes' + 'boiled potato' -> one 'potato' "
                "entry) — present items as given, don't re-merge or re-split them. Pantry "
                "items already on hand are marked have=true rather than removed, so you can "
                "show what's needed vs. what's already there. Omit menu_id to use the most "
                "recently planned menu. This also builds the grocery list for a Macro "
                "Kit — pass the kit_id build_macro_kit returned (a kit list gives grams "
                "to buy per food over the kit's day horizon, with meals_covered instead "
                "of used_in_recipes). With no id at all it uses the most recent menu, "
                "falling back to the most recent kit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "menu_id": {"type": "integer", "description": "Omit to use the most recent menu."},
                    "kit_id": {
                        "type": "integer",
                        "description": "A Macro Kit id from build_macro_kit. Omit for a menu.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_nutrition_reference",
            "description": (
                "Search vintage nutrition-science/diet-theory books (calorie tables, "
                "vitamin/mineral facts, dietetics, food chemistry — public-domain texts, "
                "roughly 1800s-1920s) for background explanation. This is NOT the recipe "
                "library — use search_recipes for dishes. Returns short excerpts with the "
                "source book title. These are historical texts, not current medical "
                "guidance: some of what they claim is outdated or since disproven (e.g. "
                "period calorie-counting fads, discredited theories) — attribute the "
                "source book by name and flag anything that reads as dated or medical "
                "advice, don't present it as settled modern nutrition science."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords, e.g. 'vitamin C scurvy' or 'protein requirement'."},
                    "limit": {"type": "integer", "description": "Max excerpts, capped at 10. Default 5."},
                },
                "required": ["query"],
            },
        },
    },
]

TOOL_NAMES = [t["function"]["name"] for t in TOOL_SCHEMAS]
