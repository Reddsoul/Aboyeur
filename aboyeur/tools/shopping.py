"""get_shopping_list tool — for a planned menu or for a Macro Kit."""

from __future__ import annotations

import json
import sqlite3

from aboyeur.config import Config
from aboyeur.engines import shopping as shopping_engine
from aboyeur.engines.nutrition import parse_ingredients
from aboyeur.tools.kit import load_kit
from aboyeur.tools.profile import get_profile

# Columns that rotate through the protein slot: any of them can be the
# bowl's protein, so a week's meals are split across all of them.
_PROTEIN_ROLES = ("protein", "dairy_protein")


def get_shopping_list(
    conn: sqlite3.Connection,
    config: Config,
    client_id: int,
    menu_id: int | None = None,
    kit_id: int | None = None,
) -> dict:
    """Consolidated grocery list for a menu or a Macro Kit, by store section.

    Both ids default to the user's most recent of that kind, and every
    lookup is scoped to client_id — never another account's row. With no id at
    all, the most recent menu wins, falling back to the most recent kit.
    An id passed as menu_id that turns out to be a kit id resolves as a kit
    (a mid-size model mixes the two up, and erroring on a row the user does
    own would be a worse answer than just handling it).

    Menu path: raw ingredient lines are canonicalized via parse_ingredients
    (cached in ingredient_parse, usually already warm from nutrition
    estimation) so "potatoes"/"irish potato"/"boiled potato" aggregate into
    one entry with summed grams instead of one line item each.

    Pantry items are marked have=True, not removed — the LLM should still
    show what's needed vs. already on hand.
    """
    if kit_id is not None:
        loaded = load_kit(conn, client_id, kit_id)
        if loaded is None:
            return {"error": f"no kit with id {kit_id}"}
        return _kit_shopping_list(conn, client_id, *loaded)

    if menu_id is not None:
        row = conn.execute(
            "SELECT days_json FROM menus WHERE id = ? AND client_id = ?", (menu_id, client_id)
        ).fetchone()
        if row is None:
            loaded = load_kit(conn, client_id, menu_id)
            if loaded is not None:
                return _kit_shopping_list(conn, client_id, *loaded)
            return {"error": f"no menu or kit with id {menu_id}"}
    else:
        row = conn.execute(
            "SELECT days_json FROM menus WHERE client_id = ? ORDER BY id DESC LIMIT 1", (client_id,)
        ).fetchone()
        if row is None:
            loaded = load_kit(conn, client_id, None)
            if loaded is not None:
                return _kit_shopping_list(conn, client_id, *loaded)
            return {"error": "no menu or kit exists yet — call plan_menu or build_macro_kit first"}

    plan = json.loads(row["days_json"])
    ids = [d["recipe_id"] for d in plan.get("days", [])]
    if not ids:
        return {"sections": {}, "total_items": 0, "to_buy": 0}

    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT ingredients_json FROM recipes WHERE id IN ({placeholders})", ids,
    ).fetchall()
    picks = [{"ingredients": json.loads(r["ingredients_json"])} for r in rows]

    pantry = _pantry(conn, client_id)
    profile = get_profile(conn, client_id)
    dislikes = profile.get("dislikes", [])

    raw_lines = sorted({
        ing.lower().strip() for p in picks for ing in p["ingredients"] if ing.strip()
    })
    parsed = parse_ingredients(conn, config, raw_lines)
    canonical = {
        raw: {"food": item["food"], "grams": item["grams"]}
        for raw, item in zip(raw_lines, parsed)
        if item.get("food")
    }

    return shopping_engine.build_shopping_list(picks, pantry, dislikes, canonical=canonical)


def _pantry(conn: sqlite3.Connection, client_id: int) -> set[str]:
    rows = conn.execute("SELECT item FROM pantry WHERE client_id = ?", (client_id,)).fetchall()
    return {r["item"] for r in rows}


def _kit_shopping_list(conn: sqlite3.Connection, client_id: int, kit_id: int, kit: dict) -> dict:
    """Grams to buy per kit component over the kit's day horizon.

    Even rotation: a column's per-meal grams are split across its items, so
    3 proteins over 21 meals is 7 meals of each. Two columns don't come from
    the assembly rule's per-meal grams —

      * dairy_protein rotates through the protein slot but isn't
        interchangeable with it gram-for-gram (170 g of yogurt is not 170 g
        of chicken), so it takes its own serving size for its share of the
        meals, and the protein column's share shrinks accordingly.
      * sauce is used every meal at its own serving size — the assembly rule
        calls it free, which is true of the macros, not of the shopping.

    Fruit is bought a serving a day when the solver left it out of the bowl
    (its usual state) rather than not bought at all.
    """
    columns = kit.get("columns") or {}
    per_meal = (kit.get("assembly_rule") or {}).get("per_meal_grams") or {}
    meals_per_day = max(1, int(kit.get("meals_per_day") or 1))
    days = max(1, int(kit.get("days") or 1))
    total_meals = meals_per_day * days

    plan: list[tuple[dict, float, float]] = []  # (item, grams, meals covered)

    protein_items = [
        (role, item) for role in _PROTEIN_ROLES for item in (columns.get(role) or [])
    ]
    if protein_items:
        share = total_meals / len(protein_items)
        for role, item in protein_items:
            grams_per_meal = (
                float(per_meal.get("protein") or 0) if role == "protein"
                else float(item.get("serving_g") or 0)
            )
            plan.append((item, grams_per_meal * share, share))

    for role in ("carb_base", "veg_volume", "fat", "fruit"):
        items = columns.get(role) or []
        if not items:
            continue
        share = total_meals / len(items)
        grams_per_meal = float(per_meal.get(role) or 0)
        if role == "fruit" and grams_per_meal <= 0:
            # Not in the bowl, still on the list: one serving a day, split
            # across the fruit column.
            for item in items:
                plan.append((item, float(item.get("serving_g") or 0) * days / len(items),
                             days / len(items)))
            continue
        for item in items:
            plan.append((item, grams_per_meal * share, share))

    sauces = columns.get("sauce") or []
    if sauces:
        share = total_meals / len(sauces)
        for item in sauces:
            plan.append((item, float(item.get("serving_g") or 0) * share, share))

    pantry = _pantry(conn, client_id)
    dislikes = [d.lower().strip() for d in (get_profile(conn, client_id).get("dislikes") or [])]

    sections: dict[str, list] = {s: [] for s in shopping_engine.SECTIONS}
    total = 0
    for item, grams, meals in plan:
        if grams <= 0:
            continue
        total += 1
        name = item["name"]
        have = any(p.lower().strip() in name or name in p.lower().strip() for p in pantry)
        flagged = any(d in name or name in d for d in dislikes)
        sections.setdefault(shopping_engine.categorize_ingredient(name), []).append({
            "name": name,
            "approx_grams": round(grams),
            "meals_covered": round(meals, 1),
            "have": have,
            "flag": "sub needed" if flagged else None,
        })

    for items in sections.values():
        items.sort(key=lambda it: (-it["approx_grams"], it["name"]))
    sections = {k: v for k, v in sections.items() if v}
    to_buy = sum(1 for s in sections.values() for it in s if not it["have"])
    return {
        "source": "kit",
        "kit_id": kit_id,
        "days": days,
        "meals_per_day": meals_per_day,
        "sections": sections,
        "total_items": total,
        "to_buy": to_buy,
        "note": (
            f"Quantities cover {days} days x {meals_per_day} meals, rotating evenly "
            "through each column. Weights are raw / as-purchased."
        ),
    }
