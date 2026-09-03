"""plan_menu / swap_menu_item tools — the deterministic optimizer's LLM-facing API.

Plans are persisted to the `menus` table as drafts and referenced by menu_id
(task.md's actual tool signatures: swap_menu_item(day, replacement_id),
get_shopping_list(menu_id?)) rather than round-tripped through the LLM's
context as a raw JSON blob on every call — a mid-size local model echoing a
multi-KB plan dict back byte-for-byte on every swap is a real reliability
risk, not a theoretical one.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import time
from collections import Counter

from aboyeur.config import Config
from aboyeur.engines import optimizer
from aboyeur.engines.nutrition import per_meal_target
from aboyeur.tools.nutrition import estimate_nutrition
from aboyeur.tools.profile import get_profile
from aboyeur.tools.recipe import get_recipe


def _avg_target_kcal(targets: dict) -> float:
    return (sum(t["per_meal"]["kcal"] for t in targets.values()) / len(targets)) if targets else 600.0


def _avg_target_macros(targets: dict) -> dict:
    """Household-average per-meal protein/carb/fat targets — the macro-fit
    term scores against these, not just calories. With no household yet, the
    default 30/40/30 split of the fallback per-meal kcal, so plans always
    state macro targets."""
    if not targets:
        fallback = per_meal_target(_avg_target_kcal(targets), 1)
        return {k: fallback[k] for k in ("protein_g", "carb_g", "fat_g")}
    n = len(targets)
    return {
        key: sum(t["per_meal"][key] for t in targets.values()) / n
        for key in ("protein_g", "carb_g", "fat_g")
    }


def _ingredient_summary(ingredient_lists: list[list[str]]) -> dict:
    """Plan-wide ingredient stats: how many distinct (meaningful) ingredients
    the whole plan needs, and which ones multiple recipes share — the
    shopping-efficiency story the overlap score summarizes as one number."""
    counts: Counter[str] = Counter()
    for ings in ingredient_lists:
        counts.update(optimizer.meaningful_ingredients(ings))
    shared = sorted((i for i, n in counts.items() if n >= 2), key=lambda i: (-counts[i], i))
    return {
        "total_unique": len(counts),
        "shared_count": len(shared),
        "shared": [{"ingredient": i, "recipes": counts[i]} for i in shared],
    }


def _load_candidates(conn: sqlite3.Connection, config: Config, candidate_ids: list[int]) -> list[dict]:
    macros_by_id = estimate_nutrition(conn, config, candidate_ids)
    placeholders = ",".join("?" * len(candidate_ids))
    rows = conn.execute(
        f"SELECT id, title, book, path, cuisine, servings, ingredients_json FROM recipes "
        f"WHERE id IN ({placeholders})",
        candidate_ids,
    ).fetchall()
    out = []
    for row in rows:
        m = macros_by_id.get(row["id"], {})
        out.append({
            "id": row["id"], "title": row["title"], "book": row["book"], "path": row["path"],
            "cuisine": row["cuisine"], "servings": row["servings"],
            "ingredients": json.loads(row["ingredients_json"]),
            "macros": {
                "calories": m.get("kcal", 0),
                "protein_g": m.get("protein_g", 0),
                "carb_g": m.get("carb_g", 0),
                "fat_g": m.get("fat_g", 0),
            },
            "confidence": m.get("confidence", "low"),
        })
    return out


def _save_menu(conn: sqlite3.Connection, user_id: int, plan: dict) -> int:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cur = conn.execute(
        "INSERT INTO menus (user_id, week_of, days_json, status, created_at) "
        "VALUES (?, ?, ?, 'draft', ?)",
        (user_id, datetime.date.today().isoformat(), json.dumps(plan), now),
    )
    conn.commit()
    return cur.lastrowid


def _load_menu(
    conn: sqlite3.Connection, user_id: int, menu_id: int | None
) -> tuple[int, dict] | None:
    if menu_id is not None:
        row = conn.execute(
            "SELECT id, days_json FROM menus WHERE id = ? AND user_id = ?", (menu_id, user_id)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, days_json FROM menus WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return row["id"], json.loads(row["days_json"])


def _update_menu(conn: sqlite3.Connection, user_id: int, menu_id: int, plan: dict) -> None:
    conn.execute(
        "UPDATE menus SET days_json = ? WHERE id = ? AND user_id = ?",
        (json.dumps(plan), menu_id, user_id),
    )
    conn.commit()


def plan_menu(
    conn: sqlite3.Connection,
    config: Config,
    user_id: int,
    candidate_ids: list[int],
    days: int,
    meals_per_day: int = 1,
    constraints: dict[str, int] | None = None,
) -> dict:
    """Optimize `candidate_ids` (should be >= 2x `days * meals_per_day`) down to
    a `days * meals_per_day`-length plan maximizing ingredient/pantry overlap
    and macro fit. One call handles every meal for every day — the caller
    does NOT need to invoke this once per meal; meals_per_day > 1 is resolved
    entirely inside optimizer.select_menu's single pass (see its docstring —
    doing this per-meal via repeated LLM tool calls was the actual cause of a
    real 60-round, ~10-minute timeout on an 18-meal request).

    Persists the plan as a draft menu and returns it with a menu_id for later
    swap/shopping-list calls. See engines/optimizer.py for the scoring shape
    and task.md §7.
    """
    profile = get_profile(conn, user_id)
    pantry_rows = conn.execute("SELECT item FROM pantry WHERE user_id = ?", (user_id,)).fetchall()
    pantry = {r["item"] for r in pantry_rows}

    targets = profile["targets"]
    avg_kcal = _avg_target_kcal(targets)
    target_macros = _avg_target_macros(targets)
    meals_per_day = max(1, int(meals_per_day or 1))

    candidates = _load_candidates(conn, config, candidate_ids)
    low_confidence_ids = [c["id"] for c in candidates if c["confidence"] == "low"]
    result = optimizer.select_menu(
        candidates, days=days, pantry=pantry, target_kcal=avg_kcal,
        diet_modes=profile.get("diet_modes", []), constraints=constraints,
        cuisine_affinity=profile.get("cuisine_affinity", []),
        meals_per_day=meals_per_day, target_macros=target_macros,
    )

    days_out = [
        {
            "day": p.day, "meal": p.meal, "role": p.role,
            "recipe_id": p.recipe_id, "title": p.title, "book": p.book, "path": p.path,
            "kcal": p.kcal,
            "protein_g": p.protein_g, "carb_g": p.carb_g, "fat_g": p.fat_g,
            # Servings of the dish to eat (kcal/macros above already include
            # it). recipe_servings is the batch size as written, so "eat 2
            # servings of a serves-10 recipe" = cook 1/5 of the batch.
            "portion_multiplier": p.portion_multiplier,
            "recipe_servings": p.servings,
            "shared_ingredients": p.shared_ingredients, "macro_fit": p.macro_fit,
            "pick_reason": p.pick_reason,
        }
        for p in result["picks"]
    ]

    plan = {
        "days": days_out,
        "meals_per_day": meals_per_day,
        "overlap_score": result["overlap_score"],
        "overlap_floor_met": result["overlap_floor_met"],
        "macro_fit_avg": result["macro_fit_avg"],
        "constraints_met": result["constraints_met"],
        "daily_totals": result["daily_totals"],
        "daily_target_kcal": round(avg_kcal * meals_per_day),
        "daily_target_macros": (
            {k: round(v * meals_per_day, 1) for k, v in target_macros.items()}
            if target_macros else None
        ),
        "ingredient_summary": _ingredient_summary([p.ingredients for p in result["picks"]]),
        "targets": targets,
        # Recipes whose nutrition didn't resolve (no USDA key, or lookups
        # failed) — calorie math involving these days is unreliable and the
        # daily_totals status will say "unknown".
        "low_confidence_nutrition": low_confidence_ids,
        "micro_coverage": None,  # Phase 4
        "gaps": [],  # Phase 4
    }
    menu_id = _save_menu(conn, user_id, plan)
    return {"menu_id": menu_id, **plan}


def get_menu_recipes(conn: sqlite3.Connection, user_id: int, menu_id: int | None = None) -> dict:
    """Every recipe in a menu, pre-ordered by day then meal and pre-labeled
    with a day/meal heading — one markdown document the LLM echoes verbatim.

    Exists because STEP-2 delivery used to mean one get_recipe call per slot
    with the model mapping recipe_id -> day itself from a plan JSON several
    turns back; a mid-size local model observably mislabels that (day 3's
    recipes presented under a "Day 1" heading). Deterministic ordering and
    headings belong in code, not in the model.
    """
    loaded = _load_menu(conn, user_id, menu_id)
    if loaded is None:
        return {"error": f"no menu with id {menu_id}" if menu_id is not None else "no menu exists yet — call plan_menu first"}
    mid, plan = loaded

    slots = sorted(
        plan.get("days", []),
        key=lambda d: (d["day"], d.get("meal", 1), d.get("role", "main") != "main"),
    )
    if not slots:
        return {"error": f"menu {mid} has no meal slots"}
    multi_meal = (plan.get("meals_per_day") or 1) > 1

    parts = []
    for s in slots:
        # h1, so slot headings out-rank the recipes' own "## Ingredients" etc.
        heading = f"# Day {s['day']}"
        if multi_meal:
            heading += f", Meal {s.get('meal', 1)}"
        if s.get("role") == "top-up":
            heading += " (second dish)"
        heading += f": {s.get('title', '')}"

        lines = [heading]
        pm = s.get("portion_multiplier") or 1.0
        if abs(pm - 1.0) >= 0.005:
            note = f"_Portion: eat {pm} servings of this dish._"
            if s.get("recipe_servings"):
                note += f" _(Recipe as written makes {s['recipe_servings']} servings.)_"
            lines.append(note)

        r = get_recipe(conn, s["recipe_id"])
        if "error" in r:
            lines.append(f"(recipe unavailable: {r['error']})")
        else:
            lines.append(r["markdown"].strip())
        parts.append("\n\n".join(lines))

    return {"menu_id": mid, "slot_count": len(slots), "markdown": "\n\n---\n\n".join(parts)}


def swap_menu_item(
    conn: sqlite3.Connection,
    config: Config,
    user_id: int,
    day: int,
    replacement_id: int,
    menu_id: int | None = None,
    meal: int = 1,
) -> dict:
    """Swap one meal's recipe in a draft menu, re-score against the rest of
    the plan, and persist the update. menu_id defaults to the most recently
    created menu if omitted (the normal case: one active plan in flight).
    meal defaults to 1 — only needed when the plan has meals_per_day > 1.
    """
    loaded = _load_menu(conn, user_id, menu_id)
    if loaded is None:
        return {"error": f"no menu with id {menu_id}" if menu_id is not None else "no menu exists yet — call plan_menu first"}
    mid, plan = loaded

    days = list(plan["days"])
    # A meal can hold two dishes (main + top-up); the swap targets the main.
    idx = next(
        (i for i, d in enumerate(days)
         if d["day"] == day and d.get("meal", 1) == meal and d.get("role", "main") == "main"),
        None,
    )
    if idx is None:
        return {"error": f"no day {day} meal {meal} in menu {mid}"}

    ids = [d["recipe_id"] for d in days]
    ids[idx] = replacement_id

    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, book, path, servings, ingredients_json FROM recipes WHERE id IN ({placeholders})", ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    if replacement_id not in by_id:
        return {"error": f"no recipe with id {replacement_id}"}

    profile = get_profile(conn, user_id)
    avg_kcal = _avg_target_kcal(profile["targets"])
    macros_by_id = estimate_nutrition(conn, config, ids)

    ing_by_id = {rid: json.loads(by_id[rid]["ingredients_json"]) for rid in ids}
    meaningful_by_id = {rid: optimizer.meaningful_ingredients(ing_by_id[rid]) for rid in ids}

    target_macros = _avg_target_macros(profile["targets"])
    daily_target = plan.get("daily_target_kcal") or 0
    new_picks = []
    for i, rid in enumerate(ids):
        row = by_id[rid]
        others = (
            set().union(*(s for other_id, s in meaningful_by_id.items() if other_id != rid))
            if len(ids) > 1 else set()
        )
        shared = sorted(meaningful_by_id[rid] & others)
        m = macros_by_id.get(rid, {})
        kcal = float(m.get("kcal") or 0)
        fit = round(optimizer.macro_fit(
            {"calories": kcal, "protein_g": m.get("protein_g"),
             "carb_g": m.get("carb_g"), "fat_g": m.get("fat_g")},
            avg_kcal, target_macros,
        ), 2)
        new_picks.append(optimizer.MenuPick(
            recipe_id=rid, title=row["title"], book=row["book"], path=row["path"],
            ingredients=ing_by_id[rid], shared_ingredients=shared,
            macro_fit=fit,
            pick_reason="manual swap" if i == idx else days[i].get("pick_reason", ""),
            day=days[i]["day"], meal=days[i].get("meal", 1),
            role=days[i].get("role", "main"), kcal=kcal,
            protein_g=float(m.get("protein_g") or 0),
            carb_g=float(m.get("carb_g") or 0),
            fat_g=float(m.get("fat_g") or 0),
            servings=row["servings"],
        ))

    # Macros above are fresh per-serving values; re-apply portion scaling per
    # day so the swapped plan hits the daily target the same way plan_menu's
    # optimizer pass does.
    picks_by_day: dict[int, list[optimizer.MenuPick]] = {}
    for p in new_picks:
        picks_by_day.setdefault(p.day, []).append(p)
    for dps in picks_by_day.values():
        optimizer.scale_day_portions(dps, daily_target, avg_kcal, target_macros)

    new_days = [
        {
            "day": p.day, "meal": p.meal, "role": p.role,
            "recipe_id": p.recipe_id, "title": p.title, "book": p.book,
            "path": p.path, "kcal": p.kcal,
            "protein_g": p.protein_g, "carb_g": p.carb_g, "fat_g": p.fat_g,
            "portion_multiplier": p.portion_multiplier,
            "recipe_servings": p.servings,
            "shared_ingredients": p.shared_ingredients, "macro_fit": p.macro_fit,
            "pick_reason": p.pick_reason,
        }
        for p in new_picks
    ]

    daily_totals = []
    for dnum in sorted(picks_by_day):
        dps = picks_by_day[dnum]
        dk = sum(p.kcal for p in dps)
        if dk <= 0 or not daily_target:
            status = "unknown"
        elif dk < daily_target * (1 - optimizer.DAILY_KCAL_TOLERANCE):
            status = "under"
        elif dk > daily_target * (1 + optimizer.DAILY_KCAL_TOLERANCE):
            status = "over"
        else:
            status = "ok"
        daily_totals.append({
            "day": dnum, "kcal": round(dk),
            "protein_g": round(sum(p.protein_g for p in dps), 1),
            "carb_g": round(sum(p.carb_g for p in dps), 1),
            "fat_g": round(sum(p.fat_g for p in dps), 1),
            "target_kcal": daily_target, "status": status,
        })

    overlap_score = optimizer.compute_overlap_score(new_picks)
    new_plan = {
        **plan,
        "days": new_days,
        "overlap_score": overlap_score,
        "overlap_floor_met": overlap_score >= optimizer.MIN_OVERLAP_SCORE,
        "macro_fit_avg": round(sum(d["macro_fit"] for d in new_days) / len(new_days), 2) if new_days else 0.0,
        "daily_totals": daily_totals,
        "ingredient_summary": _ingredient_summary(list(ing_by_id.values())),
    }
    _update_menu(conn, user_id, mid, new_plan)
    return {"menu_id": mid, **new_plan}
