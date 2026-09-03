"""The LLM's tool API (task.md §5). Plain callables — JSON schemas for
native tool-calling live in tools/schemas.py.
"""

from aboyeur.tools.menu import get_menu_recipes, plan_menu, swap_menu_item
from aboyeur.tools.nutrition import estimate_nutrition
from aboyeur.tools.nutrition_reference import search_nutrition_reference
from aboyeur.tools.pantry import manage_pantry
from aboyeur.tools.profile import get_profile, update_profile
from aboyeur.tools.recipe import get_recipe
from aboyeur.tools.search import search_recipes
from aboyeur.tools.shopping import get_shopping_list

__all__ = [
    "search_recipes",
    "get_recipe",
    "get_profile",
    "update_profile",
    "manage_pantry",
    "estimate_nutrition",
    "plan_menu",
    "swap_menu_item",
    "get_menu_recipes",
    "get_shopping_list",
    "search_nutrition_reference",
]
