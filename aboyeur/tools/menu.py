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


def _avg_daily_kcal(targets: dict) -> float:
    """Household-average DAILY calorie need (not per-meal)."""
    if not targets:
        return 1800.0
    return sum(
        t.get("daily_kcal") or t["per_meal"]["kcal"] for t in targets.values()
    ) / len(targets)


def _avg_daily_macros(targets: dict) -> dict:
    """Household-average DAILY protein/carb/fat/fiber targets."""
    if not targets:
        d = per_meal_target(_avg_daily_kcal(targets), 1)
        return {k: d[k] for k in ("protein_g", "carb_g", "fat_g", "fiber_g")}
    n = len(targets)
    keys = ("protein_g", "carb_g", "fat_g", "fiber_g")
    out = {k: 0.0 for k in keys}
    for t in targets.values():
        dm = t.get("daily_macros") or per_meal_target(
            t.get("daily_kcal") or t["per_meal"]["kcal"], 1
        )
        for k in keys:
            out[k] += float(dm.get(k) or 0)
    return {k: v / n for k, v in out.items()}


def _menu_meal_targets(targets: dict, meals_per_day: int) -> tuple[float, dict]:
    """Per-meal kcal + macro targets for a menu with `meals_per_day` meals —
    derived from the DAILY need divided by the menu's meal count, NOT from a
    member's stored per_meal (which is baked at that member's own
    meals_per_day and would be wrong whenever the menu asks for a different
    number)."""
    m = max(1, int(meals_per_day or 1))
    per_meal_kcal = _avg_daily_kcal(targets) / m
    daily_macros = _avg_daily_macros(targets)
    per_meal_macros = {k: v / m for k, v in daily_macros.items()}
    return per_meal_kcal, per_meal_macros


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
        f"SELECT id, title, book, path, cuisine, servings, ingredients_json, "
        f"course, meal_complete FROM recipes WHERE id IN ({placeholders})",
        candidate_ids,
    ).fetchall()
    out = []
    for row in rows:
        m = macros_by_id.get(row["id"], {})
        out.append({
            "id": row["id"], "title": row["title"], "book": row["book"], "path": row["path"],
            "cuisine": row["cuisine"], "servings": row["servings"],
            "course": row["course"], "meal_complete": row["meal_complete"],
            "ingredients": json.loads(row["ingredients_json"]),
            "macros": {
                "calories": m.get("kcal", 0),
                "protein_g": m.get("protein_g", 0),
                "carb_g": m.get("carb_g", 0),
                "fat_g": m.get("fat_g", 0),
                "fiber_g": m.get("fiber_g", 0),
            },
            "confidence": m.get("confidence", "low"),
        })
    return out


def _save_menu(conn: sqlite3.Connection, client_id: int, plan: dict) -> int:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cur = conn.execute(
        "INSERT INTO menus (client_id, week_of, days_json, status, created_at) "
        "VALUES (?, ?, ?, 'draft', ?)",
        (client_id, datetime.date.today().isoformat(), json.dumps(plan), now),
    )
    conn.commit()
    return cur.lastrowid


def _load_menu(
    conn: sqlite3.Connection, client_id: int, menu_id: int | None
) -> tuple[int, dict] | None:
    if menu_id is not None:
        row = conn.execute(
            "SELECT id, days_json FROM menus WHERE id = ? AND client_id = ?", (menu_id, client_id)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, days_json FROM menus WHERE client_id = ? ORDER BY id DESC LIMIT 1",
            (client_id,),
        ).fetchone()
    if row is None:
        return None
    return row["id"], json.loads(row["days_json"])


def _update_menu(conn: sqlite3.Connection, client_id: int, menu_id: int, plan: dict) -> None:
    conn.execute(
        "UPDATE menus SET days_json = ? WHERE id = ? AND client_id = ?",
        (json.dumps(plan), menu_id, client_id),
    )
    conn.commit()


def _picks_to_days(picks: list[optimizer.MenuPick], cuisine_by_id: dict[int, str | None]) -> list[dict]:
    return [
        {
            "day": p.day, "meal": p.meal, "role": p.role,
            "recipe_id": p.recipe_id, "title": p.title, "book": p.book, "path": p.path,
            "cuisine": cuisine_by_id.get(p.recipe_id),
            "kcal": p.kcal,
            "protein_g": p.protein_g, "carb_g": p.carb_g, "fat_g": p.fat_g,
            "fiber_g": p.fiber_g,
            # Servings of the dish to eat (kcal/macros above already include
            # it). recipe_servings is the batch size as written, so "eat 2
            # servings of a serves-10 recipe" = cook 1/5 of the batch.
            "portion_multiplier": p.portion_multiplier,
            "recipe_servings": p.servings,
            "shared_ingredients": p.shared_ingredients, "macro_fit": p.macro_fit,
            "pick_reason": p.pick_reason,
        }
        for p in picks
    ]


def _ingredients_for(conn: sqlite3.Connection, recipe_ids: list[int]) -> dict[int, list[str]]:
    ids = sorted(set(recipe_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, ingredients_json FROM recipes WHERE id IN ({placeholders})", ids
    ).fetchall()
    return {r["id"]: json.loads(r["ingredients_json"]) for r in rows}


def _fmt_g(v) -> str:
    try:
        return f"{round(float(v))} g"
    except (TypeError, ValueError):
        return "?"


def render_menu_summary(plan: dict) -> str:
    """The STEP-1 proposal table, built in code from the plan dict.

    The model used to transcribe this table by hand from plan_menu's JSON —
    day numbers, titles, multipliers and totals — and a mid-size local model
    observably gets that wrong (a 7-day plan summarized as days 3-7, day
    numbers that match nothing real). Same fix get_menu_recipes already
    applied to STEP 2: deterministic markdown the model echoes verbatim, with
    the day numbering guaranteed by code. Tolerates older persisted plans
    that lack cuisine/fiber/gap fields.
    """
    days = sorted(
        plan.get("days", []),
        key=lambda d: (d["day"], d.get("meal", 1), d.get("role", "main") != "main"),
    )
    meals_per_day = max(1, int(plan.get("meals_per_day") or 1))
    multi_meal = meals_per_day > 1
    day_numbers = sorted({d["day"] for d in days})
    lines: list[str] = []

    span = ""
    if day_numbers:
        span = (
            f"day {day_numbers[0]}" if len(day_numbers) == 1
            else f"days {day_numbers[0]}-{day_numbers[-1]}"
        )
    head = f"## Menu #{plan.get('menu_id', '?')}"
    if span:
        head += f" — {span}, {meals_per_day} meal{'s' if multi_meal else ''}/day"
    lines.append(head)

    target_kcal = plan.get("daily_target_kcal")
    tm = plan.get("daily_target_macros") or {}
    if target_kcal:
        tline = f"**Daily target:** {round(target_kcal)} kcal"
        if tm:
            tline += (
                f" · protein {_fmt_g(tm.get('protein_g'))} · carbs {_fmt_g(tm.get('carb_g'))}"
                f" · fat {_fmt_g(tm.get('fat_g'))}"
            )
            if tm.get("fiber_g") is not None:
                tline += f" · fiber {_fmt_g(tm.get('fiber_g'))}"
        lines.append(tline)

    cols = ["Day"] + (["Meal"] if multi_meal else []) + ["Dish", "Cuisine", "Portion", "Macro fit"]
    lines.append("")
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("|" + "|".join("---" for _ in cols) + "|")
    for d in days:
        dish = f"{d.get('title', '')} (id {d.get('recipe_id')})"
        if d.get("role") == "top-up":
            dish = "+ " + dish + " — second dish"
        pm = d.get("portion_multiplier") or 1.0
        if abs(pm - 1.0) >= 0.005:
            portion = f"{pm} servings"
            if d.get("recipe_servings"):
                portion += f" (recipe makes {d['recipe_servings']})"
        else:
            portion = "1 serving"
        fit = d.get("macro_fit")
        row = [str(d["day"])] + ([str(d.get("meal", 1))] if multi_meal else []) + [
            dish, d.get("cuisine") or "—", portion,
            f"{fit:.2f}" if isinstance(fit, (int, float)) else "—",
        ]
        lines.append("| " + " | ".join(row) + " |")

    totals = sorted(plan.get("daily_totals", []), key=lambda t: t["day"])
    if totals:
        lines.append("")
        lines.append("**Daily totals vs target**")
        lines.append("")
        lines.append("| Day | kcal | Protein | Carbs | Fat | Fiber | Status |")
        lines.append("|---|---|---|---|---|---|---|")
        for t in totals:
            tk = t.get("target_kcal") or target_kcal
            kcal = f"{t.get('kcal', '?')} / {round(tk)}" if tk else str(t.get("kcal", "?"))

            def _vs(key: str) -> str:
                have = t.get(key)
                want = tm.get(key)
                if have is None:
                    return "—"
                return f"{round(have)} / {round(want)} g" if want is not None else f"{round(have)} g"

            ms = t.get("macro_status") or t.get("status") or "unknown"
            status = ms
            notes = t.get("gap_notes") or []
            if ms == "off" and notes:
                status = "off — " + "; ".join(notes)
            elif ms == "unknown":
                status = "unknown (nutrition data missing)"
            lines.append(
                f"| {t['day']} | {kcal} | {_vs('protein_g')} | {_vs('carb_g')} | "
                f"{_vs('fat_g')} | {_vs('fiber_g')} | {status} |"
            )

    lines.append("")
    score_bits = []
    if plan.get("overlap_score") is not None:
        floor = plan.get("overlap_floor_met")
        score_bits.append(
            f"Ingredient overlap {plan['overlap_score']}/100"
            + ("" if floor is None else (" (floor met)" if floor else " (below the 25 floor)"))
        )
    if plan.get("macro_fit_avg") is not None:
        score_bits.append(f"macro fit avg {plan['macro_fit_avg']}")
    if score_bits:
        lines.append("**Scores:** " + " · ".join(score_bits))

    summ = plan.get("ingredient_summary") or {}
    if summ:
        shared = summ.get("shared") or []
        shared_txt = ", ".join(
            f"{e['ingredient']} ({e['recipes']})" for e in shared[:12]
        ) or "none"
        if len(shared) > 12:
            shared_txt += f", +{len(shared) - 12} more"
        lines.append(
            f"**Ingredients:** {summ.get('total_unique', '?')} distinct; "
            f"shared across recipes: {shared_txt}"
        )

    low = plan.get("low_confidence_nutrition") or []
    if low:
        titles = {d["recipe_id"]: d.get("title") for d in days}
        picked_low = [rid for rid in low if rid in titles]
        if picked_low:
            lines.append(
                "**Nutrition uncertain for:** "
                + ", ".join(f"{titles[rid]} (id {rid})" for rid in picked_low)
                + " — totals involving these are estimates at best."
            )
    return "\n".join(lines)


def plan_menu(
    conn: sqlite3.Connection,
    config: Config,
    client_id: int,
    candidate_ids: list[int],
    days: int,
    meals_per_day: int = 1,
    constraints: dict[str, int] | None = None,
    extend_menu_id: int | None = None,
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

    extend_menu_id: continue an existing draft instead of starting a new one.
    `days` then means *additional* days: they are numbered from that menu's
    last day + 1, planned against the same targets and meals_per_day the
    menu already has, deduplicated against every dish already in it, merged
    into its `days`, and saved back to the SAME menu_id (plan-wide totals,
    overlap, macro fit and the ingredient summary are recomputed over the
    whole merged plan). "Give me the remaining 2 days" is this call — before
    it existed the only option was a disconnected fresh plan whose day
    numbers restarted at 1. If the extra days can't be built the call returns
    cannot_build exactly like a fresh plan does and the existing menu is left
    untouched — never a silently shorter extension.
    """
    profile = get_profile(conn, client_id)
    pantry_rows = conn.execute("SELECT item FROM pantry WHERE client_id = ?", (client_id,)).fetchall()
    pantry = {r["item"] for r in pantry_rows}

    targets = profile["targets"]

    existing: tuple[int, dict] | None = None
    start_day = 1
    exclude_ids: set[int] = set()
    exclude_titles: set[str] = set()
    if extend_menu_id is not None:
        existing = _load_menu(conn, client_id, extend_menu_id)
        if existing is None:
            return {"error": f"no menu with id {extend_menu_id} to extend — call plan_menu without extend_menu_id to start a new one"}
        existing_plan = existing[1]
        existing_days = existing_plan.get("days", [])
        # The menu's own meal count wins: a 3-meal plan can't grow 1-meal days.
        meals_per_day = max(1, int(existing_plan.get("meals_per_day") or 1))
        start_day = max((d["day"] for d in existing_days), default=0) + 1
        exclude_ids = {d["recipe_id"] for d in existing_days}
        exclude_titles = {optimizer.norm_title(d.get("title", "")) for d in existing_days}

    meals_per_day = max(1, int(meals_per_day or 1))
    avg_kcal, target_macros = _menu_meal_targets(targets, meals_per_day)
    if existing is not None:
        # Grade the new days against the targets the existing days were
        # graded against, so one plan isn't half old-target, half new.
        stored_kcal = existing[1].get("daily_target_kcal")
        stored_macros = existing[1].get("daily_target_macros")
        if stored_kcal:
            avg_kcal = float(stored_kcal) / meals_per_day
        if stored_macros:
            target_macros = {k: float(v) / meals_per_day for k, v in stored_macros.items()}

    candidates = [
        c for c in _load_candidates(conn, config, candidate_ids)
        if c["id"] not in exclude_ids and optimizer.norm_title(c["title"]) not in exclude_titles
    ]
    low_confidence_ids = [c["id"] for c in candidates if c["confidence"] == "low"]
    result = optimizer.select_menu(
        candidates, days=days, pantry=pantry, target_kcal=avg_kcal,
        diet_modes=profile.get("diet_modes", []), constraints=constraints,
        cuisine_affinity=profile.get("cuisine_affinity", []),
        meals_per_day=meals_per_day, target_macros=target_macros,
        start_day=start_day,
    )

    # A plan that can't hit the client's calorie + macro targets from this
    # recipe pool is not delivered as a draft — the agent tells the user what
    # couldn't be met and offers a Macro Kit or looser constraints instead.
    # (See prompts.py "cannot_build" handling.) Same for an extension: the
    # existing days stay exactly as they were, nothing partial is appended.
    if not result.get("feasible", True):
        out = {
            "error": "cannot_build",
            "reasons": result.get("infeasible_reasons", []),
            "daily_totals": result.get("daily_totals", []),
            "daily_target_kcal": round(avg_kcal * meals_per_day),
            "daily_target_macros": (
                {k: round(v * meals_per_day, 1) for k, v in target_macros.items()}
                if target_macros else None
            ),
            "suggest": "macro_kit",
        }
        if existing is not None:
            out["menu_id"] = existing[0]
            out["existing_days_kept"] = start_day - 1
        return out

    cuisine_by_id = {c["id"]: c.get("cuisine") for c in candidates}
    days_out = _picks_to_days(result["picks"], cuisine_by_id)

    daily_target_kcal = round(avg_kcal * meals_per_day)
    daily_target_macros = (
        {k: round(v * meals_per_day, 1) for k, v in target_macros.items()}
        if target_macros else None
    )

    if existing is not None:
        # Append: the new days join the existing ones under the same menu_id,
        # and every plan-wide field that depends on the full day set is
        # recomputed over all of them — never just the new slice.
        mid, existing_plan = existing
        all_days = sorted(
            list(existing_plan.get("days", [])) + days_out,
            key=lambda d: (d["day"], d.get("meal", 1), d.get("role", "main") != "main"),
        )
        all_totals = sorted(
            list(existing_plan.get("daily_totals", [])) + list(result["daily_totals"]),
            key=lambda t: t["day"],
        )
        existing_ings = _ingredients_for(conn, [d["recipe_id"] for d in existing_plan.get("days", [])])
        all_ingredient_lists = [
            existing_ings.get(d["recipe_id"], []) for d in existing_plan.get("days", [])
        ] + [p.ingredients for p in result["picks"]]
        overlap_score = optimizer.compute_overlap_score([
            optimizer.MenuPick(
                recipe_id=0, title="", book="", path="", ingredients=ings,
                shared_ingredients=[], macro_fit=0.0, pick_reason="",
            )
            for ings in all_ingredient_lists
        ])
        merged_constraints = dict(existing_plan.get("constraints_met") or {})
        for kw, n in (result["constraints_met"] or {}).items():
            merged_constraints[kw] = merged_constraints.get(kw, 0) + n
        low_conf = sorted(
            set(existing_plan.get("low_confidence_nutrition") or []) | set(low_confidence_ids)
        )
        plan = {
            **existing_plan,
            "days": all_days,
            "meals_per_day": meals_per_day,
            "overlap_score": overlap_score,
            "overlap_floor_met": overlap_score >= optimizer.MIN_OVERLAP_SCORE,
            "macro_fit_avg": (
                round(sum(d["macro_fit"] for d in all_days) / len(all_days), 2) if all_days else 0.0
            ),
            "constraints_met": merged_constraints,
            "daily_totals": all_totals,
            "daily_target_kcal": daily_target_kcal,
            "daily_target_macros": daily_target_macros,
            "ingredient_summary": _ingredient_summary(all_ingredient_lists),
            "targets": targets,
            "low_confidence_nutrition": low_conf,
        }
        plan["summary_markdown"] = render_menu_summary({"menu_id": mid, **plan})
        _update_menu(conn, client_id, mid, plan)
        return {
            "menu_id": mid,
            "appended_days": [start_day + i for i in range(days)],
            **plan,
        }

    plan = {
        "days": days_out,
        "meals_per_day": meals_per_day,
        "overlap_score": result["overlap_score"],
        "overlap_floor_met": result["overlap_floor_met"],
        "macro_fit_avg": result["macro_fit_avg"],
        "constraints_met": result["constraints_met"],
        "daily_totals": result["daily_totals"],
        "daily_target_kcal": daily_target_kcal,
        "daily_target_macros": daily_target_macros,
        "ingredient_summary": _ingredient_summary([p.ingredients for p in result["picks"]]),
        "targets": targets,
        # Recipes whose nutrition didn't resolve (no USDA key, or lookups
        # failed) — calorie math involving these days is unreliable and the
        # daily_totals status will say "unknown".
        "low_confidence_nutrition": low_confidence_ids,
        "micro_coverage": None,  # Phase 4
        "gaps": [],  # Phase 4
    }
    menu_id = _save_menu(conn, client_id, plan)
    # The day-by-day table is rendered here, in code, and stored with the
    # plan — the model echoes it, it never transcribes the JSON into a table
    # itself (see render_menu_summary).
    plan["summary_markdown"] = render_menu_summary({"menu_id": menu_id, **plan})
    _update_menu(conn, client_id, menu_id, plan)
    return {"menu_id": menu_id, **plan}


def get_menu_recipes(conn: sqlite3.Connection, client_id: int, menu_id: int | None = None) -> dict:
    """Every recipe in a menu, pre-ordered by day then meal and pre-labeled
    with a day/meal heading — one markdown document the LLM echoes verbatim.

    Exists because STEP-2 delivery used to mean one get_recipe call per slot
    with the model mapping recipe_id -> day itself from a plan JSON several
    turns back; a mid-size local model observably mislabels that (day 3's
    recipes presented under a "Day 1" heading). Deterministic ordering and
    headings belong in code, not in the model.
    """
    loaded = _load_menu(conn, client_id, menu_id)
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
    client_id: int,
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
    loaded = _load_menu(conn, client_id, menu_id)
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
        f"SELECT id, title, book, path, cuisine, servings, ingredients_json FROM recipes WHERE id IN ({placeholders})", ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    if replacement_id not in by_id:
        return {"error": f"no recipe with id {replacement_id}"}

    profile = get_profile(conn, client_id)
    macros_by_id = estimate_nutrition(conn, config, ids)

    ing_by_id = {rid: json.loads(by_id[rid]["ingredients_json"]) for rid in ids}
    meaningful_by_id = {rid: optimizer.meaningful_ingredients(ing_by_id[rid]) for rid in ids}

    meals_per_day = max(1, int(plan.get("meals_per_day") or 1))
    avg_kcal, target_macros = _menu_meal_targets(profile["targets"], meals_per_day)
    daily_target = plan.get("daily_target_kcal") or round(avg_kcal * meals_per_day)
    daily_macros = optimizer.daily_macros_from_per_meal(target_macros, meals_per_day)
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
            fiber_g=float(m.get("fiber_g") or 0),
            servings=row["servings"],
        ))

    # Macros above are fresh per-serving values; re-apply portion scaling per
    # day so the swapped plan hits the daily target the same way plan_menu's
    # optimizer pass does.
    picks_by_day: dict[int, list[optimizer.MenuPick]] = {}
    for p in new_picks:
        picks_by_day.setdefault(p.day, []).append(p)
    for dps in picks_by_day.values():
        optimizer.solve_day_portions(dps, daily_target, avg_kcal, target_macros)

    new_days = _picks_to_days(new_picks, {rid: by_id[rid]["cuisine"] for rid in ids})

    daily_totals = []
    for dnum in sorted(picks_by_day):
        dps = picks_by_day[dnum]
        totals = {
            "kcal": sum(p.kcal for p in dps),
            "protein_g": sum(p.protein_g for p in dps),
            "carb_g": sum(p.carb_g for p in dps),
            "fat_g": sum(p.fat_g for p in dps),
            "fiber_g": sum(p.fiber_g for p in dps),
        }
        status, macro_status, gap_notes = optimizer.day_macro_status(
            totals, daily_target, daily_macros
        )
        daily_totals.append({
            "day": dnum, "kcal": round(totals["kcal"]),
            "protein_g": round(totals["protein_g"], 1),
            "carb_g": round(totals["carb_g"], 1),
            "fat_g": round(totals["fat_g"], 1),
            "fiber_g": round(totals["fiber_g"], 1),
            "target_kcal": daily_target, "status": status,
            "macro_status": macro_status, "gap_notes": gap_notes,
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
    new_plan["summary_markdown"] = render_menu_summary({"menu_id": mid, **new_plan})
    _update_menu(conn, client_id, mid, new_plan)
    return {"menu_id": mid, **new_plan}
