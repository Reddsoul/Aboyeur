"""Variety (profile 0-100): overlap floor, dish repetition, and how it
reaches the optimizer from the profile."""

from __future__ import annotations

import json
import sqlite3

import pytest

from aboyeur.config import Config
from aboyeur.engines import optimizer
from aboyeur.tools.profile import get_profile, update_profile


def _c(id_, title, ingredients, kcal=500, book=None):
    return {"id": id_, "title": title, "book": book or f"Book{id_}", "ingredients": ingredients,
            "macros": {"calories": kcal, "protein_g": 30, "carb_g": 50, "fat_g": 15}}


# Six dishes with nothing in common: unique picks can never share ingredients,
# so any overlap has to come from repeating a dish.
LONERS = [_c(i, f"Dish {i}", [f"a{i}", f"b{i}", f"c{i}", f"d{i}"]) for i in range(1, 7)]


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("v,floor", [(0, 80), (25, 65), (50, 50), (75, 25), (100, 0)])
def test_overlap_floor_for(v, floor):
    assert optimizer.overlap_floor_for(v) == floor


def test_clamp_and_defaults():
    assert optimizer.clamp_variety(None) == 50
    assert optimizer.clamp_variety("nope") == 50
    assert optimizer.clamp_variety(-5) == 0 and optimizer.clamp_variety(140) == 100
    assert optimizer.clamp_variety(49.6) == 50


@pytest.mark.parametrize("v,budget", [(0, 12), (25, 9), (50, 6), (75, 3), (100, 0)])
def test_repeat_budget_for(v, budget):
    assert optimizer.repeat_budget_for(v, total_picks=14, meals_per_day=2) == budget


def test_repeat_budget_single_day_is_zero():
    assert optimizer.repeat_budget_for(0, total_picks=2, meals_per_day=2) == 0


# ---------------------------------------------------------------------------
# select_menu behaviour
# ---------------------------------------------------------------------------

def test_variety_zero_repeats_day_one_every_day():
    r = optimizer.select_menu(LONERS, days=4, pantry=set(), target_kcal=500, meals_per_day=2, variety=0)
    picks = r["picks"]
    assert len(picks) == 8
    by_day = {}
    for p in picks:
        by_day.setdefault(p.day, []).append((p.meal, p.recipe_id))
    assert len(by_day) == 4
    day1 = sorted(by_day[1])
    assert all(sorted(v) == day1 for v in by_day.values())
    assert len({rid for _, rid in day1}) == 2  # two different dishes within the day
    assert r["repeats"] == 6 and r["variety"] == 0
    assert any("repeat of day 1" in p.pick_reason for p in picks)


def test_variety_100_never_repeats_and_reports_floor_zero():
    r = optimizer.select_menu(LONERS, days=3, pantry=set(), target_kcal=500, meals_per_day=2, variety=100)
    ids = [p.recipe_id for p in r["picks"]]
    assert len(ids) == len(set(ids)) == 6
    assert r["repeats"] == 0 and r["overlap_floor"] == 0 and r["overlap_floor_met"] is True


def test_variety_100_does_not_fail_a_pool_that_shares_nothing():
    # Under the old flat floor this pool was "floor unmet"; at 100 nothing
    # is asked of overlap.
    r = optimizer.select_menu(LONERS, days=3, pantry=set(), target_kcal=500, variety=100)
    assert r["overlap_floor_met"] is True


def test_default_variety_stops_at_the_first_repeat_that_clears_the_floor():
    # Every dish shares two meaningful ingredients and brings two of its own: six unique
    # picks score 42 (14 unique / 24 total), one repeat scores exactly 50.
    # Budget is 2; only 1 should be spent.
    half = [_c(i, f"Half {i}", ["chicken thigh", "ginger root", f"own{i}a", f"own{i}b"]) for i in range(1, 7)]
    r = optimizer.select_menu(half, days=3, pantry=set(), target_kcal=500, meals_per_day=2)
    assert r["variety"] == 50 and r["overlap_floor"] == 50
    assert r["overlap_score"] >= 50 and r["overlap_floor_met"] is True
    assert r["repeats"] == 1
    assert len({p.recipe_id for p in r["picks"]}) == 5


def test_budget_exhausted_is_reported_honestly():
    # Loners: 6 slots, budget 2 at variety 50 -> best reachable is 33.
    r = optimizer.select_menu(LONERS, days=3, pantry=set(), target_kcal=500, meals_per_day=2)
    assert r["repeats"] == 2
    assert r["overlap_score"] == 33 and r["overlap_floor_met"] is False


def test_sharing_pool_needs_no_repeats_at_default_variety():
    sharers = [_c(10 + i, f"Sharer {i}", ["chicken thigh", "ginger root", "scallion", f"x{i}"])
               for i in range(1, 7)]
    r = optimizer.select_menu(sharers, days=3, pantry=set(), target_kcal=500, meals_per_day=2)
    assert r["repeats"] == 0 and r["overlap_floor_met"] is True


def test_repeats_never_put_the_same_dish_twice_in_one_day():
    for v in (0, 10, 25, 40):
        r = optimizer.select_menu(LONERS, days=5, pantry=set(), target_kcal=500, meals_per_day=2, variety=v)
        for day in {p.day for p in r["picks"]}:
            mains = [p.recipe_id for p in r["picks"] if p.day == day and p.role == "main"]
            assert len(mains) == len(set(mains)), (v, day, mains)


def test_low_variety_fills_slots_a_thin_pool_cannot():
    # Three dishes, six slots: at variety 100 that's infeasible; at 25 repeats
    # fill the plan and the pool is "enough".
    three = LONERS[:3]
    strict = optimizer.select_menu(three, days=3, pantry=set(), target_kcal=500, meals_per_day=2, variety=100)
    assert len(strict["picks"]) == 3 and strict["feasible"] is False
    loose = optimizer.select_menu(three, days=3, pantry=set(), target_kcal=500, meals_per_day=2, variety=25)
    assert len(loose["picks"]) == 6
    assert not any("usable dishes" in reason for reason in loose["infeasible_reasons"])


# ---------------------------------------------------------------------------
# Profile plumbing
# ---------------------------------------------------------------------------

def test_new_client_starts_at_50_and_set_variety_clamps(db_conn: sqlite3.Connection, client_id: int):
    assert get_profile(db_conn, client_id)["variety"] == 50
    update_profile(db_conn, client_id, {"set_variety": 130})
    assert get_profile(db_conn, client_id)["variety"] == 100
    update_profile(db_conn, client_id, {"set_variety": "12.4"})
    assert get_profile(db_conn, client_id)["variety"] == 12


def test_profile_form_save_carries_variety(db_conn: sqlite3.Connection, client_id: int):
    from backend.profile_ui import save_profile
    saved = save_profile(db_conn, client_id, {"household": [], "meals_per_day": 2, "variety": 20})
    assert saved["variety"] == 20
    # Omitted -> untouched, not reset.
    saved = save_profile(db_conn, client_id, {"household": [], "meals_per_day": 2})
    assert saved["variety"] == 20


def test_plan_menu_reads_variety_from_the_profile(db_conn: sqlite3.Connection, config: Config, client_id: int):
    from aboyeur.tools.menu import plan_menu
    ids = [r["id"] for r in db_conn.execute("SELECT id FROM recipes")]
    update_profile(db_conn, client_id, {"set_variety": 0})
    plan = plan_menu(db_conn, config, client_id, candidate_ids=ids, days=3, meals_per_day=2)
    assert "error" not in plan, plan
    assert plan["variety"] == 0 and plan["overlap_floor"] == 80
    mains = {d["day"]: sorted(d["recipe_id"] for d in plan["days"] if d["role"] == "main")
             for d in plan["days"]}
    assert len(set(map(tuple, mains.values()))) == 1  # same mains every day
    assert "variety 0/100" in plan["summary_markdown"]
    assert "repeat a dish by design" in plan["summary_markdown"]


def test_shopping_list_counts_a_repeated_dish_per_slot(db_conn: sqlite3.Connection, config: Config, client_id: int):
    from aboyeur.tools.shopping import get_shopping_list
    rid = db_conn.execute("SELECT id FROM recipes ORDER BY id LIMIT 1").fetchone()["id"]
    slot = {"day": 1, "meal": 1, "role": "main", "recipe_id": rid, "title": "T"}
    for n_slots in (1, 3):
        plan = {"meals_per_day": 1, "days": [{**slot, "day": d + 1} for d in range(n_slots)]}
        cur = db_conn.execute(
            "INSERT INTO menus (client_id, week_of, days_json, status, created_at) "
            "VALUES (?, '2026-09-21', ?, 'draft', 'now')", (client_id, json.dumps(plan)),
        )
        db_conn.commit()
        result = get_shopping_list(db_conn, config, client_id, menu_id=cur.lastrowid)
        items = [it for sec in result["sections"].values() for it in sec]
        assert items
        if n_slots == 1:
            once = {it["name"]: it["used_in_recipes"] for it in items}
        else:
            assert all(it["used_in_recipes"] == 3 * once[it["name"]] for it in items)


def test_get_menu_recipes_prints_a_repeated_dish_once(db_conn: sqlite3.Connection, config: Config, client_id: int):
    from aboyeur.tools.menu import get_menu_recipes
    rid = db_conn.execute("SELECT id FROM recipes ORDER BY id LIMIT 1").fetchone()["id"]
    plan = {"meals_per_day": 1, "days": [
        {"day": d, "meal": 1, "role": "main", "recipe_id": rid, "title": "Same"} for d in (1, 2, 3)
    ]}
    cur = db_conn.execute(
        "INSERT INTO menus (client_id, week_of, days_json, status, created_at) "
        "VALUES (?, '2026-09-21', ?, 'draft', 'now')", (client_id, json.dumps(plan)),
    )
    db_conn.commit()
    md = get_menu_recipes(db_conn, client_id, cur.lastrowid)["markdown"]
    assert md.count("## Ingredients") == 1
    assert md.count("Same recipe as Day 1") == 2
