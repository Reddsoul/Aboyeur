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


# Self-repair: how many times plan_menu widens the pool and re-plans the
# days that missed target before delivering its best attempt. Each round
# pulls this many extra corpus recipes, ranked by ingredient overlap with
# what's already in the plan (cached-nutrition recipes first — estimating
# nutrition for an uncached one costs LLM + USDA round-trips).
REPAIR_ROUNDS = (60, 150, 300)
REPAIR_UNCACHED_CAP = (10, 20, 30)


def _widen_pool(
    conn: sqlite3.Connection,
    config: Config,
    profile: dict,
    pantry: set[str],
    exclude_ids: set[int],
    exclude_titles: set[str],
    seed_ingredients: set[str],
    want: int,
    uncached_cap: int,
) -> list[dict]:
    """Pull `want` more menu-eligible recipes from the whole corpus, ranked
    by how many ingredients they share with `seed_ingredients` (the dishes
    already in the plan) and the pantry — so a repaired day reuses the
    shopping list rather than adding to it. Allergies are hard-excluded here
    exactly as search_recipes does; cuisine_affinity is applied when it
    leaves enough to choose from. Recipes with cached nutrition come first
    (free); at most `uncached_cap` uncached ones are admitted per round
    because each costs a live estimate."""
    from aboyeur.engines import allergens
    allergies = allergens.expand_all([a for a in profile.get("allergies", []) if a and a.strip()])
    affinity = {c.lower() for c in (profile.get("cuisine_affinity") or [])}
    rows = conn.execute(
        """SELECT r.id, r.title, r.cuisine, r.course, r.meal_complete, r.ingredients_json,
                  (n.recipe_id IS NOT NULL) AS has_nutrition
           FROM recipes r LEFT JOIN nutrition n ON n.recipe_id = r.id
           WHERE r.course IN ('main', 'breakfast', 'unknown')"""
    ).fetchall()

    scored: list[tuple[float, bool, int]] = []
    seen_titles = set(exclude_titles)
    for r in rows:
        if r["id"] in exclude_ids:
            continue
        nt = optimizer.norm_title(r["title"])
        if nt in seen_titles:
            continue
        ingredients = json.loads(r["ingredients_json"])
        if optimizer.is_menu_ineligible(r["title"], ingredients, r["course"]):
            continue
        blob = " ".join(ingredients).lower() + " " + r["title"].lower()
        if allergies and any(a in blob for a in allergies):
            continue
        m_ings = optimizer.meaningful_ingredients(ingredients)
        overlap = len(m_ings & seed_ingredients)
        pantry_hits = sum(1 for i in m_ings if any(p in i or i in p for p in pantry)) if pantry else 0
        score = 2.0 * overlap + 1.0 * pantry_hits
        if affinity and (r["cuisine"] or "").lower() in affinity:
            score += 1.5
        if r["meal_complete"]:
            score += 0.5
        if r["course"] == "unknown":
            score -= 0.5
        seen_titles.add(nt)
        scored.append((score, bool(r["has_nutrition"]), r["id"]))

    scored.sort(key=lambda t: (-t[0], not t[1], t[2]))
    picked: list[int] = []
    uncached = 0
    for _, cached, rid in scored:
        if len(picked) >= want:
            break
        if not cached:
            if uncached >= uncached_cap:
                continue
            uncached += 1
        picked.append(rid)
    if not picked:
        return []
    # _load_candidates returns DB order; keep the overlap ranking.
    by_id = {c["id"]: c for c in _load_candidates(conn, config, picked)}
    return [by_id[rid] for rid in picked if rid in by_id]


def _merge_results(kept: dict, kept_days: set[int], sub: dict) -> dict:
    """Combine the kept days of one select_menu result with the re-planned
    days of another into one result-shaped dict."""
    picks = [p for p in kept["picks"] if p.day in kept_days] + list(sub["picks"])
    picks.sort(key=lambda p: (p.day, p.meal, p.role != "main"))
    totals = [t for t in kept["daily_totals"] if t["day"] in kept_days] + list(sub["daily_totals"])
    totals.sort(key=lambda t: t["day"])
    constraints = dict(kept.get("constraints_met") or {})
    for kw, n in (sub.get("constraints_met") or {}).items():
        constraints[kw] = constraints.get(kw, 0) + n
    overlap = optimizer.compute_overlap_score(picks)
    off = [t for t in totals if t.get("macro_status") == "off"]
    reasons = [f"day {t['day']}: " + ("; ".join(t.get("gap_notes") or []) or "misses target") for t in off]
    reasons += [r for r in sub.get("infeasible_reasons", []) if not r.startswith("day ")]
    return {
        "picks": picks,
        "feasible": not reasons,
        "infeasible_reasons": reasons,
        "overlap_score": overlap,
        "overlap_floor_met": overlap >= optimizer.MIN_OVERLAP_SCORE,
        "macro_fit_avg": round(sum(p.macro_fit for p in picks) / len(picks), 2) if picks else 0.0,
        "constraints_met": constraints,
        "daily_totals": totals,
    }


def _failed_days(
    result: dict, wanted_days: list[int], meals_per_day: int, include_unknown: bool = False,
) -> list[int]:
    """Days that missed target, have too few dishes, or are missing entirely.

    include_unknown: also count days with no nutrition data. Only sensible
    when the pool actually contains dishes WITH data (otherwise every day is
    unknown and re-planning can't change that) — a known-macro day beats an
    unknown one, so the repair pass swaps unknowns out when it can."""
    by_day: dict[int, int] = {}
    for p in result["picks"]:
        if p.role == "main":
            by_day[p.day] = by_day.get(p.day, 0) + 1
    status = {t["day"]: t.get("macro_status") for t in result["daily_totals"]}
    bad = {"off", "unknown"} if include_unknown else {"off"}
    return [
        d for d in wanted_days
        if by_day.get(d, 0) < meals_per_day or status.get(d) in bad
    ]


def _repair_failed_days(
    conn: sqlite3.Connection,
    config: Config,
    profile: dict,
    pantry: set[str],
    result: dict,
    candidates: list[dict],
    wanted_days: list[int],
    *,
    meals_per_day: int,
    avg_kcal: float,
    target_macros: dict | None,
    constraints: dict | None,
    exclude_ids: set[int],
    exclude_titles: set[str],
    seed_ingredients: set[str],
) -> tuple[dict, list[dict], list[str]]:
    """When the first pass leaves days off target, keep the days that landed
    and re-plan only the failed ones from a progressively wider corpus pool,
    scoring every replacement for overlap with the ingredients the plan
    already uses. Returns (best result, all candidates seen, notes).

    This replaces the old "refuse the whole plan" behavior: a 7-day request
    now comes back as 7 days, with any day that still couldn't be brought
    fully on target after the last round flagged in its daily_totals
    (macro_status "off" + gap_notes) and in `repair_notes` — never a plan
    missing days, never a table hidden behind a refusal."""
    notes: list[str] = []
    all_candidates = list(candidates)
    seen_ids = set(exclude_ids) | {c["id"] for c in candidates}
    seen_titles = set(exclude_titles) | {optimizer.norm_title(c["title"]) for c in candidates}
    best = result

    def _has_known() -> bool:
        return any(c.get("confidence") != "low" for c in all_candidates)

    for round_no, (want, uncached_cap) in enumerate(zip(REPAIR_ROUNDS, REPAIR_UNCACHED_CAP), 1):
        failed = _failed_days(best, wanted_days, meals_per_day, include_unknown=_has_known())
        if not failed:
            break
        kept_days = set(wanted_days) - set(failed)
        kept_picks = [p for p in best["picks"] if p.day in kept_days]
        seeds = set(seed_ingredients)
        for p in kept_picks:
            seeds |= optimizer.meaningful_ingredients(p.ingredients)
        kept_ids = {p.recipe_id for p in kept_picks}
        kept_titles = {optimizer.norm_title(p.title) for p in kept_picks}

        extra = _widen_pool(
            conn, config, profile, pantry,
            exclude_ids=seen_ids | kept_ids, exclude_titles=seen_titles | kept_titles,
            seed_ingredients=seeds, want=want, uncached_cap=uncached_cap,
        )
        for c in extra:
            seen_ids.add(c["id"])
            seen_titles.add(optimizer.norm_title(c["title"]))
        all_candidates.extend(extra)

        pool = [
            c for c in all_candidates
            if c["id"] not in kept_ids and optimizer.norm_title(c["title"]) not in kept_titles
        ]
        sub = optimizer.select_menu(
            pool, days=len(failed), pantry=pantry, target_kcal=avg_kcal,
            diet_modes=profile.get("diet_modes", []), constraints=constraints,
            cuisine_affinity=profile.get("cuisine_affinity", []),
            meals_per_day=meals_per_day, target_macros=target_macros,
            day_numbers=failed, seed_ingredients=seeds,
        )
        merged = _merge_results(best, kept_days, sub)
        known = _has_known()
        merged_failed = _failed_days(merged, wanted_days, meals_per_day, include_unknown=known)
        notes.append(
            f"round {round_no}: re-planned day(s) {', '.join(map(str, failed))} from "
            f"{len(extra)} added recipes; {len(merged_failed)} still off"
        )
        # Keep whichever attempt leaves fewer failed days (ties: the newer,
        # which had the wider, overlap-ranked pool).
        if len(merged_failed) <= len(_failed_days(best, wanted_days, meals_per_day, include_unknown=known)):
            best = merged
        if not merged_failed:
            break

    still = _failed_days(best, wanted_days, meals_per_day, include_unknown=_has_known())
    if still and notes:
        notes.append(
            f"day(s) {', '.join(map(str, still))} are the closest the cookbook library "
            f"gets after {len(notes)} repair round(s) — see gap_notes (or missing nutrition "
            f"data); a swap or a Macro Kit can close the gap"
        )
    return best, all_candidates, notes


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

    off_days = [t["day"] for t in totals if t.get("macro_status") == "off"]
    if off_days:
        lines.append(
            f"**Not fully on target:** day{'s' if len(off_days) > 1 else ''} "
            f"{', '.join(map(str, off_days))} — closest the library gets (see Status); "
            "swap the day or use a Macro Kit to close the gap."
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

    Self-repair: days the first pass leaves off target are re-planned from a
    progressively wider corpus pool (REPAIR_ROUNDS), keeping the days that
    landed and ranking replacements by shared ingredients with them. The
    plan always comes back with every requested day; a day that still
    misses after the last round is delivered flagged (daily_totals
    macro_status "off" + gap_notes, repair_notes), not hidden. cannot_build
    is reserved for slots that can't be filled at all.

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
    # Ingredients already committed (an extended menu's existing days): new
    # picks are scored for overlap with them from slot one.
    existing_ings: dict[int, list[str]] = {}
    seed_ingredients: set[str] = set()
    if existing is not None:
        existing_ings = _ingredients_for(conn, [d["recipe_id"] for d in existing[1].get("days", [])])
        for ings in existing_ings.values():
            seed_ingredients |= optimizer.meaningful_ingredients(ings)

    wanted_days = list(range(start_day, start_day + int(days)))
    result = optimizer.select_menu(
        candidates, days=days, pantry=pantry, target_kcal=avg_kcal,
        diet_modes=profile.get("diet_modes", []), constraints=constraints,
        cuisine_affinity=profile.get("cuisine_affinity", []),
        meals_per_day=meals_per_day, target_macros=target_macros,
        start_day=start_day, seed_ingredients=seed_ingredients,
    )

    # Days that missed target are re-planned from a wider pool (the model's
    # hand-built candidate pool is routinely too thin — two searches for a
    # 14-meal week), keeping the days that landed and favoring dishes that
    # share ingredients with them. See _repair_failed_days.
    # (Returns untouched when every day landed.)
    result, candidates, repair_notes = _repair_failed_days(
        conn, config, profile, pantry, result, candidates, wanted_days,
        meals_per_day=meals_per_day, avg_kcal=avg_kcal, target_macros=target_macros,
        constraints=constraints, exclude_ids=exclude_ids, exclude_titles=exclude_titles,
        seed_ingredients=seed_ingredients,
    )
    low_confidence_ids = [c["id"] for c in candidates if c["confidence"] == "low"]

    # Only a plan with EMPTY slots is refused now — the library genuinely has
    # nothing eligible left even after widening. Days that are merely off
    # target are delivered flagged (daily_totals macro_status/gap_notes +
    # repair_notes) so the user sees the whole week and can swap, not a
    # refusal that hides every meal. For an extension the existing days
    # stay exactly as they were, nothing partial is appended.
    filled_days = {p.day for p in result["picks"] if p.role == "main"}
    if any(d not in filled_days for d in wanted_days):
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
            "repair_notes": repair_notes,
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
        # What the self-repair pass did, and which days (if any) are still
        # short — surfaced in the summary so an approximate day is never
        # presented as if it landed.
        "repair_notes": repair_notes,
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
