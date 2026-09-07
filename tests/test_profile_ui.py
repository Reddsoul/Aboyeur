"""Profile editor (backend/profile_ui.py): full-state save diffing + chat snapshot."""

from __future__ import annotations

import sqlite3

from aboyeur.tools.profile import get_profile
from backend import profile_ui

FULL_STATE = {
    "household": [
        {"id": "", "label": "Jose", "sex": "m", "age": "30", "height_cm": "178",
         "weight_kg": "80", "activity": "moderate", "daily_kcal": "", "meals_per_day": ""},
    ],
    "allergies": ["Peanut", "shellfish"],
    "dislikes": ["okra"],
    "likes": [],
    "cuisine_affinity": ["Indian"],
    "diet_modes": [],
    "meals_per_day": 3,
}


def test_save_profile_creates_members_and_lists(db_conn: sqlite3.Connection, client_id: int):
    saved = profile_ui.save_profile(db_conn, client_id, FULL_STATE)
    assert [m["id"] for m in saved["household"]] == ["jose"]  # id slugged from label
    member = saved["household"][0]
    assert member["age"] == 30 and member["height_cm"] == 178.0  # form strings coerced
    assert saved["allergies"] == ["peanut", "shellfish"]
    assert saved["targets"]["jose"]["source"] == "bmr"


def test_save_profile_goal_and_goal_weight_round_trip(db_conn: sqlite3.Connection, client_id: int):
    state = {**FULL_STATE, "household": [
        {**FULL_STATE["household"][0], "goal": "lose", "goal_weight_kg": "72"},
    ]}
    saved = profile_ui.save_profile(db_conn, client_id, state)
    member = saved["household"][0]
    assert member["goal"] == "lose"
    assert member["goal_weight_kg"] == 72.0
    t = saved["targets"]["jose"]
    # 80 -> 72 kg is >5 kg out: 0.75 kg/week -> 825/day deficit, computed not manual.
    assert t["goal_adjust_kcal"] == -round(7700 * 0.75 / 7)
    assert t["daily_kcal"] == t["maintenance_kcal"] + t["goal_adjust_kcal"]


def test_save_profile_removes_dropped_allergy(db_conn: sqlite3.Connection, client_id: int):
    profile_ui.save_profile(db_conn, client_id, FULL_STATE)
    second = {**FULL_STATE, "allergies": ["shellfish"]}
    saved = profile_ui.save_profile(db_conn, client_id, second)
    assert saved["allergies"] == ["shellfish"]


def test_save_profile_blanked_member_field_is_cleared(db_conn: sqlite3.Connection, client_id: int):
    profile_ui.save_profile(db_conn, client_id, FULL_STATE)
    # User blanks the weight in the form — merge semantics must NOT resurrect it.
    edited = {**FULL_STATE, "household": [
        {**FULL_STATE["household"][0], "id": "jose", "weight_kg": ""},
    ]}
    saved = profile_ui.save_profile(db_conn, client_id, edited)
    assert "weight_kg" not in saved["household"][0]
    assert saved["targets"]["jose"]["source"] == "fallback"  # no full anthro -> no BMR


def test_save_profile_removes_deleted_member(db_conn: sqlite3.Connection, client_id: int):
    profile_ui.save_profile(db_conn, client_id, FULL_STATE)
    saved = profile_ui.save_profile(db_conn, client_id, {**FULL_STATE, "household": []})
    assert saved["household"] == []


def test_save_profile_garbled_number_never_blocks_save(db_conn: sqlite3.Connection, client_id: int):
    state = {**FULL_STATE, "household": [
        {"label": "Sam", "age": "twelve", "meals_per_day": "1"},
    ]}
    saved = profile_ui.save_profile(db_conn, client_id, state)
    member = saved["household"][0]
    assert member["id"] == "sam"
    assert "age" not in member
    assert member["meals_per_day"] == 1


def test_profile_markdown_snapshot(db_conn: sqlite3.Connection, client_id: int):
    profile_ui.save_profile(db_conn, client_id, FULL_STATE)
    md = profile_ui.profile_markdown(get_profile(db_conn, client_id), "http://box:8000/profile/ui")
    assert "http://box:8000/profile/ui" in md
    assert "peanut, shellfish" in md
    assert "Jose" in md


def test_save_profile_persists_units(db_conn: sqlite3.Connection, client_id: int):
    saved = profile_ui.save_profile(db_conn, client_id, {**FULL_STATE, "units": "imperial"})
    assert saved["units"] == "imperial"
    saved = profile_ui.save_profile(db_conn, client_id, {**FULL_STATE, "units": "metric"})
    assert saved["units"] == "metric"
    # junk never sticks
    saved = profile_ui.save_profile(db_conn, client_id, {**FULL_STATE, "units": "stone"})
    assert saved["units"] == "metric"


def test_profile_markdown_imperial_display(db_conn: sqlite3.Connection, client_id: int):
    profile_ui.save_profile(db_conn, client_id, {**FULL_STATE, "units": "imperial"})
    md = profile_ui.profile_markdown(get_profile(db_conn, client_id), "http://x/profile/ui")
    assert "5'10\"" in md    # 178 cm
    assert "176lb" in md     # 80 kg
    assert "/guest" in md    # guest hint present
