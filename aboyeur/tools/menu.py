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

from aboyeur.config import Config
from aboyeur.engines import optimizer
from aboyeur.tools.nutrition import estimate_nutrition
from aboyeur.tools.profile import get_profile


def _avg_target_kcal(targets: dict) -> float:
    return (sum(t["per_meal"]["kcal"] for t in targets.values()) / len(targets)) if targets else 600.0


def _load_candidates(conn: sqlite3.Connection, config: Config, candidate_ids: list[int]) -> list[dict]:
    macros_by_id = estimate_nutrition(conn, config, candidate_ids)
    placeholders = ",".join("?" * len(candidate_ids))
    rows = conn.execute(
        f"SELECT id, title, book, path, ingredients_json FROM recipes WHERE id IN ({placeholders})",
        candidate_ids,
    ).fetchall()
    out = []
    for row in rows:
        m = macros_by_id.get(row["id"], {})
        out.append({
            "id": row["id"], "title": row["title"], "book": row["book"], "path": row["path"],
            "ingredients": json.loads(row["ingredients_json"]),
            "macros": {"calories": m.get("kcal", 0)},
        })
    return out


def _save_menu(conn: sqlite3.Connection, plan: dict) -> int:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cur = conn.execute(
        "INSERT INTO menus (week_of, days_json, status, created_at) VALUES (?, ?, 'draft', ?)",
        (datetime.date.today().isoformat(), json.dumps(plan), now),
    )
    conn.commit()
    return cur.lastrowid


def _load_menu(conn: sqlite3.Connection, menu_id: int | None) -> tuple[int, dict] | None:
    if menu_id is not None:
        row = conn.execute("SELECT id, days_json FROM menus WHERE id = ?", (menu_id,)).fetchone()
    else:
        row = conn.execute("SELECT id, days_json FROM menus ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return row["id"], json.loads(row["days_json"])


def _update_menu(conn: sqlite3.Connection, menu_id: int, plan: dict) -> None:
    conn.execute("UPDATE menus SET days_json = ? WHERE id = ?", (json.dumps(plan), menu_id))
    conn.commit()


def plan_menu(
    conn: sqlite3.Connection,
    config: Config,
    candidate_ids: list[int],
    days: int,
    meals_per_day: int = 1,
    constraints: dict[str, int] | None = None,
) -> dict:
    """Optimize `candidate_ids` (should be >= 2x `days`) down to a `days`-length
    plan maximizing ingredient/pantry overlap and macro fit. Persists the plan
    as a draft menu and returns it with a menu_id for later swap/shopping-list
    calls. See engines/optimizer.py for the scoring shape and task.md §7.
    """
    profile = get_profile(conn)
    pantry_rows = conn.execute("SELECT item FROM pantry").fetchall()
    pantry = {r["item"] for r in pantry_rows}

    targets = profile["targets"]
    avg_kcal = _avg_target_kcal(targets)

    candidates = _load_candidates(conn, config, candidate_ids)
    result = optimizer.select_menu(
        candidates, days=days, pantry=pantry, target_kcal=avg_kcal,
        diet_modes=profile.get("diet_modes", []), constraints=constraints,
    )

    days_out = [
        {
            "day": i, "recipe_id": p.recipe_id, "title": p.title, "book": p.book, "path": p.path,
            "shared_ingredients": p.shared_ingredients, "macro_fit": p.macro_fit,
            "pick_reason": p.pick_reason,
        }
        for i, p in enumerate(result["picks"], start=1)
    ]

    plan = {
        "days": days_out,
        "overlap_score": result["overlap_score"],
        "macro_fit_avg": result["macro_fit_avg"],
        "constraints_met": result["constraints_met"],
        "targets": targets,
        "micro_coverage": None,  # Phase 4
        "gaps": [],  # Phase 4
    }
    menu_id = _save_menu(conn, plan)
    return {"menu_id": menu_id, **plan}


def swap_menu_item(
    conn: sqlite3.Connection,
    config: Config,
    day: int,
    replacement_id: int,
    menu_id: int | None = None,
) -> dict:
    """Swap one day's recipe in a draft menu, re-score against the rest of the
    plan, and persist the update. menu_id defaults to the most recently
    created menu if omitted (the normal case: one active plan in flight).
    """
    loaded = _load_menu(conn, menu_id)
    if loaded is None:
        return {"error": f"no menu with id {menu_id}" if menu_id is not None else "no menu exists yet — call plan_menu first"}
    mid, plan = loaded

    days = list(plan["days"])
    idx = next((i for i, d in enumerate(days) if d["day"] == day), None)
    if idx is None:
        return {"error": f"no day {day} in menu {mid}"}

    ids = [d["recipe_id"] for d in days]
    ids[idx] = replacement_id

    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, book, path, ingredients_json FROM recipes WHERE id IN ({placeholders})", ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    if replacement_id not in by_id:
        return {"error": f"no recipe with id {replacement_id}"}

    profile = get_profile(conn)
    avg_kcal = _avg_target_kcal(profile["targets"])
    macros_by_id = estimate_nutrition(conn, config, ids)

    ing_by_id = {rid: json.loads(by_id[rid]["ingredients_json"]) for rid in ids}
    meaningful_by_id = {rid: optimizer.meaningful_ingredients(ing_by_id[rid]) for rid in ids}

    new_days = []
    for i, rid in enumerate(ids):
        row = by_id[rid]
        others = (
            set().union(*(s for other_id, s in meaningful_by_id.items() if other_id != rid))
            if len(ids) > 1 else set()
        )
        shared = sorted(meaningful_by_id[rid] & others)
        kcal = macros_by_id.get(rid, {}).get("kcal", 0)
        fit = round(optimizer.macro_fit({"calories": kcal}, avg_kcal), 2)
        new_days.append({
            "day": i + 1, "recipe_id": rid, "title": row["title"], "book": row["book"],
            "path": row["path"],
            "shared_ingredients": shared, "macro_fit": fit,
            "pick_reason": "manual swap" if rid == replacement_id else days[i].get("pick_reason", ""),
        })

    picks_for_score = [
        optimizer.MenuPick(d["recipe_id"], d["title"], d["book"], d["path"], ing_by_id[d["recipe_id"]],
                            d["shared_ingredients"], d["macro_fit"], d["pick_reason"])
        for d in new_days
    ]

    new_plan = {
        **plan,
        "days": new_days,
        "overlap_score": optimizer.compute_overlap_score(picks_for_score),
        "macro_fit_avg": round(sum(d["macro_fit"] for d in new_days) / len(new_days), 2) if new_days else 0.0,
    }
    _update_menu(conn, mid, new_plan)
    return {"menu_id": mid, **new_plan}
