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


def test_save_profile_creates_members_and_lists(db_conn: sqlite3.Connection):
    saved = profile_ui.save_profile(db_conn, FULL_STATE)
    assert [m["id"] for m in saved["household"]] == ["jose"]  # id slugged from label
    member = saved["household"][0]
    assert member["age"] == 30 and member["height_cm"] == 178.0  # form strings coerced
    assert saved["allergies"] == ["peanut", "shellfish"]
    assert saved["targets"]["jose"]["source"] == "bmr"


def test_save_profile_removes_dropped_allergy(db_conn: sqlite3.Connection):
    profile_ui.save_profile(db_conn, FULL_STATE)
    second = {**FULL_STATE, "allergies": ["shellfish"]}
    saved = profile_ui.save_profile(db_conn, second)
    assert saved["allergies"] == ["shellfish"]


def test_save_profile_blanked_member_field_is_cleared(db_conn: sqlite3.Connection):
    profile_ui.save_profile(db_conn, FULL_STATE)
    # User blanks the weight in the form — merge semantics must NOT resurrect it.
    edited = {**FULL_STATE, "household": [
        {**FULL_STATE["household"][0], "id": "jose", "weight_kg": ""},
    ]}
    saved = profile_ui.save_profile(db_conn, edited)
    assert "weight_kg" not in saved["household"][0]
    assert saved["targets"]["jose"]["source"] == "fallback"  # no full anthro -> no BMR


def test_save_profile_removes_deleted_member(db_conn: sqlite3.Connection):
    profile_ui.save_profile(db_conn, FULL_STATE)
    saved = profile_ui.save_profile(db_conn, {**FULL_STATE, "household": []})
    assert saved["household"] == []


def test_save_profile_garbled_number_never_blocks_save(db_conn: sqlite3.Connection):
    state = {**FULL_STATE, "household": [
        {"label": "Sam", "age": "twelve", "meals_per_day": "1"},
    ]}
    saved = profile_ui.save_profile(db_conn, state)
    member = saved["household"][0]
    assert member["id"] == "sam"
    assert "age" not in member
    assert member["meals_per_day"] == 1


def test_profile_markdown_snapshot(db_conn: sqlite3.Connection):
    profile_ui.save_profile(db_conn, FULL_STATE)
    md = profile_ui.profile_markdown(get_profile(db_conn), "http://box:8000/profile/ui")
    assert "http://box:8000/profile/ui" in md
    assert "peanut, shellfish" in md
    assert "Jose" in md
