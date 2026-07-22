"""get_profile / update_profile tools. Profile is a single JSON row in `profile`.

update_profile takes an explicit patch of add/remove actions rather than a
freeform merge — an allergy list is safety-critical and must never be
silently dropped by an unrelated field update.
"""

from __future__ import annotations

import json
import sqlite3
import time

from aboyeur.engines import nutrition

DEFAULT_PROFILE = {
    "household": [],
    "allergies": [],
    "dislikes": [],
    "likes": [],
    "cuisine_affinity": [],
    "diet_modes": [],
    "meals_per_day": 3,
}


def _load_raw(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT data_json FROM profile WHERE id = 1").fetchone()
    if row is None:
        return dict(DEFAULT_PROFILE)
    return json.loads(row["data_json"])


def _save_raw(conn: sqlite3.Connection, profile: dict) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        "INSERT INTO profile (id, data_json, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at",
        (json.dumps(profile), now),
    )
    conn.commit()


def get_profile(conn: sqlite3.Connection) -> dict:
    """Household, computed per-member BMR/TDEE targets, restrictions, preferences."""
    profile = _load_raw(conn)
    return {**profile, "targets": nutrition.household_targets(profile)}


def update_profile(conn: sqlite3.Connection, patch: dict) -> dict:
    """Apply a structured patch. Supported keys:

    add_allergies, remove_allergies, add_dislikes, remove_dislikes,
    add_likes, remove_likes (all list[str]) — add/remove semantics, not replace.
    set_diet_modes, set_cuisine_affinity (list[str]) — replace.
    set_meals_per_day (int) — replace.
    add_member (dict with "id") — upsert by id.
    remove_member (str id).
    """
    profile = _load_raw(conn)

    def _add(field: str, values: list[str]) -> None:
        s = set(profile.get(field, []))
        s.update(v.lower().strip() for v in values)
        profile[field] = sorted(s)

    def _remove(field: str, values: list[str]) -> None:
        s = set(profile.get(field, []))
        s.difference_update(v.lower().strip() for v in values)
        profile[field] = sorted(s)

    if "add_allergies" in patch:
        _add("allergies", patch["add_allergies"])
    if "remove_allergies" in patch:
        _remove("allergies", patch["remove_allergies"])
    if "add_dislikes" in patch:
        _add("dislikes", patch["add_dislikes"])
    if "remove_dislikes" in patch:
        _remove("dislikes", patch["remove_dislikes"])
    if "add_likes" in patch:
        _add("likes", patch["add_likes"])
    if "remove_likes" in patch:
        _remove("likes", patch["remove_likes"])
    if "set_diet_modes" in patch:
        profile["diet_modes"] = list(patch["set_diet_modes"])
    if "set_cuisine_affinity" in patch:
        profile["cuisine_affinity"] = list(patch["set_cuisine_affinity"])
    if "set_meals_per_day" in patch:
        profile["meals_per_day"] = int(patch["set_meals_per_day"])
    if "add_member" in patch:
        member = patch["add_member"]
        members = [m for m in profile.get("household", []) if m["id"] != member["id"]]
        members.append(member)
        profile["household"] = members
    if "remove_member" in patch:
        profile["household"] = [
            m for m in profile.get("household", []) if m["id"] != patch["remove_member"]
        ]

    _save_raw(conn, profile)
    return get_profile(conn)
