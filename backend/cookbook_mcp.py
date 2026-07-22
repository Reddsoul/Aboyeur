"""fastmcp tool definitions for Odysseus integration (Phase 6).

Mounted on the FastAPI app at /mcp (SSE endpoint: /mcp/sse). Tools reuse the same
engines as the HTTP API; each opens its own DB session. No imports from main.py
(avoids a circular import at mount time).
"""
import logging

from fastmcp import FastMCP

from backend import macro_engine, menu_engine, preference_engine, rag
from backend.models import Recipe, SessionLocal

logger = logging.getLogger("aboyeur.mcp")

mcp = FastMCP("aboyeur")


@mcp.tool()
def search_recipes(query: str, filters: dict = {}) -> list:
    """Search cookbook library by semantic query. Returns top recipe matches."""
    db = SessionLocal()
    try:
        hits = rag.search(query, n=10, filters=filters or None)
        out = []
        for h in hits:
            r = db.get(Recipe, h["id"])
            out.append({
                "id": h["id"], "score": h["score"],
                "title": r.title if r else h["metadata"].get("title", ""),
                "technique": r.technique if r else "",
                "ingredients": (r.ingredients if r else []) or [],
            })
        return out
    finally:
        db.close()


@mcp.tool()
def plan_menu(days: int, constraints: str = "") -> dict:
    """Generate a meal plan optimized for ingredient overlap and household nutrition targets."""
    db = SessionLocal()
    try:
        profile = preference_engine.load_profile()
        query = f"weeknight dinners. {constraints}".strip()
        result = menu_engine.generate_menu(db, query, days or profile.get("plan_days", 7), profile)
        result["fusion_concepts"] = []
        try:
            if result["curated_recipes"]:
                from backend import fusion_engine
                top = [{"ingredients": c["ingredients"], "technique": c["technique"],
                        "cuisine": c["cuisine"]} for c in result["curated_recipes"][:5]]
                lib = {i for (ings,) in db.query(Recipe.ingredients).limit(1000)
                       for i in (ings or [])}
                result["fusion_concepts"] = fusion_engine.generate_fusion_concepts(
                    top, n=2, library_ingredients=lib,
                    fusion_tolerance=profile.get("preferences", {}).get("fusion_tolerance", "medium"))
        except Exception as e:
            logger.warning("fusion in MCP plan_menu failed: %s", e)
        return result
    finally:
        db.close()


@mcp.tool()
def scale_recipe(recipe_id: str, servings_per_person: dict = {}) -> dict:
    """Scale a recipe to exact serving counts / calorie targets per household member."""
    db = SessionLocal()
    try:
        r = db.get(Recipe, recipe_id)
        if not r:
            return {"error": "recipe not found"}
        profile = preference_engine.load_profile()
        targets = macro_engine.household_targets(profile)
        base = {"calories": r.calories, "protein_g": r.protein_g,
                "carb_g": r.carb_g, "fat_g": r.fat_g}
        portions = {}
        for mid, t in targets.items():
            target_kcal = t["per_meal"]["kcal"]
            if mid in (servings_per_person or {}):
                target_kcal = target_kcal * float(servings_per_person[mid])
            portions[mid] = macro_engine.scale_recipe(base, target_kcal)
        return {"recipe_id": r.id, "title": r.title, "portions": portions}
    finally:
        db.close()


@mcp.tool()
def update_preferences(feedback: str) -> dict:
    """Log feedback and update household food preferences."""
    preference_engine.log_interaction({"shown": [], "chosen": [], "skipped": [],
                                       "feedback_raw": feedback})
    diff = preference_engine.infer_preferences(last_n=5)
    profile = preference_engine.update_profile(diff)
    return {"diff": diff, "preferences": profile.get("preferences", {})}


@mcp.tool()
def get_shopping_list() -> dict:
    """Get pantry-aware, section-grouped shopping list for the current menu."""
    from backend import main as _main  # current draft/approved menu lives here
    db = SessionLocal()
    try:
        menu = _main._current_menu(db)
        if not menu:
            return {"error": "no menu — plan one first"}
        profile = preference_engine.load_profile()
        pantry = menu_engine._pantry_set(db)
        dislikes = [d.lower() for d in profile.get("preferences", {}).get("dislikes", [])]
        selection = [{"ingredients": c["ingredients"], "technique": c.get("technique", "")}
                     for c in menu.get("curated_recipes", [])]
        return menu_engine.build_shopping_list(selection, pantry, dislikes)
    finally:
        db.close()


def get_asgi_app():
    """Return the MCP ASGI app, tolerant of fastmcp version differences.

    task.md mounts via `mcp.get_asgi_app()`; fastmcp 2.x exposes `sse_app()` /
    `http_app()`. Try them in order.
    """
    for attr, kwargs in (("get_asgi_app", {}), ("sse_app", {}), ("http_app", {"transport": "sse"})):
        fn = getattr(mcp, attr, None)
        if callable(fn):
            try:
                return fn(**kwargs)
            except TypeError:
                return fn()
    raise RuntimeError("no compatible fastmcp ASGI factory found")
