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
    "units": "metric",
}

# Guest mode: a /guest session plans against a temporary, never-persisted
# profile. Earlier this lived in a module-level global the Agent set/cleared
# around each turn — fine only because the backend serialized every turn
# behind one process-wide lock. With real concurrent users that global would
# leak one user's guest profile into another's turn, so every profile
# consumer now takes `guest` explicitly instead: passed a dict, operate on
# it in memory only; passed None, read/write the DB as user_id's own row.


def make_guest_profile() -> dict:
    """Fresh, empty guest profile — no household, no allergies, metric units."""
    return json.loads(json.dumps(DEFAULT_PROFILE))


def _load_raw(conn: sqlite3.Connection, user_id: int, guest: dict | None) -> dict:
    if guest is not None:
        return guest
    row = conn.execute("SELECT data_json FROM profile WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return dict(DEFAULT_PROFILE)
    return json.loads(row["data_json"])


def _save_raw(conn: sqlite3.Connection, user_id: int, profile: dict, guest: dict | None) -> None:
    if guest is not None:
        # In-memory only: keep the guest dict identity so the Agent's
        # reference stays live across tool calls within the turn.
        if profile is not guest:
            guest.clear()
            guest.update(profile)
        return
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        "INSERT INTO profile (user_id, data_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at",
        (user_id, json.dumps(profile), now),
    )
    conn.commit()


def get_profile(conn: sqlite3.Connection, user_id: int, guest: dict | None = None) -> dict:
    """Household, computed per-member BMR/TDEE targets, restrictions, preferences.

    When a macro strategy is set (update_profile's set_macro_strategy, used
    by the Macro Kit), each member also gets `strategy_daily_macros` — that
    strategy's daily protein/carb/fat grams. Additive: `targets`' existing
    per_meal / daily_macros keep the 30/40/30 split the menu path reads, and
    the field is absent entirely until a strategy is chosen.
    """
    profile = _load_raw(conn, user_id, guest)
    targets = nutrition.household_targets(profile)
    strategy = profile.get("macro_strategy")
    if strategy:
        for member in profile.get("household", []):
            entry = targets.get(member["id"])
            if entry is None:
                continue
            entry["strategy_daily_macros"] = nutrition.resolve_macro_targets(
                member, entry["daily_kcal"], strategy
            )
    return {**profile, "targets": targets}


def update_profile(
    conn: sqlite3.Connection, user_id: int, patch: dict, guest: dict | None = None
) -> dict:
    """Apply a structured patch. Supported keys:

    add_allergies, remove_allergies, add_dislikes, remove_dislikes,
    add_likes, remove_likes (all list[str]) — add/remove semantics, not replace.
    set_diet_modes, set_cuisine_affinity (list[str]) — replace.
    set_meals_per_day (int) — replace, household-wide default.
    set_macro_strategy ("balanced"|"endurance"|"cut") — replace, household-wide.
    add_member (dict with "id") — upsert by id, merged onto any existing
    member with that id (fields you omit are kept, not wiped) — so a
    single-field patch like {"id": "sam", "meals_per_day": 1} to put just
    one person on OMAD doesn't blow away their anthropometrics.
    remove_member (str id).
    """
    profile = _load_raw(conn, user_id, guest)

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
    if "set_macro_strategy" in patch:
        # Household-wide for now (Phase 1): a kit is built for one member,
        # but nothing yet asks two members to be on different strategies,
        # and a per-member field nobody sets is a field that rots.
        strategy = str(patch["set_macro_strategy"]).lower().strip()
        if strategy in nutrition.MACRO_STRATEGIES:
            profile["macro_strategy"] = strategy
    if "set_units" in patch:
        units = str(patch["set_units"]).lower().strip()
        if units in ("metric", "imperial"):
            profile["units"] = units
    if "add_member" in patch:
        # The model occasionally sends a list of members here despite the
        # schema saying object — upsert each rather than crashing mid-stream.
        raw = patch["add_member"]
        patch_members = raw if isinstance(raw, list) else [raw]
        for patch_member in patch_members:
            if not isinstance(patch_member, dict) or "id" not in patch_member:
                raise ValueError(
                    f"add_member entries must be objects with an 'id' field, got {patch_member!r}"
                )
            existing = next(
                (m for m in profile.get("household", []) if m["id"] == patch_member["id"]), None
            )
            member = {**existing, **patch_member} if existing else patch_member
            members = [m for m in profile.get("household", []) if m["id"] != member["id"]]
            members.append(member)
            profile["household"] = members
    if "remove_member" in patch:
        profile["household"] = [
            m for m in profile.get("household", []) if m["id"] != patch["remove_member"]
        ]

    _save_raw(conn, user_id, profile, guest)
    return get_profile(conn, user_id, guest)
