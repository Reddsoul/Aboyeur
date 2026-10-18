"""get_profile / update_profile tools. Profile is a single JSON row in `profile`.

update_profile takes an explicit patch of add/remove actions rather than a
freeform merge — an allergy list is safety-critical and must never be
silently dropped by an unrelated field update.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from aboyeur.config import Config
from aboyeur.engines import nutrition
from aboyeur.crypto import decrypt_field, encrypt_field

# Opt-out default (feature-spec-2026-09-26.md #5 open decision): a new
# client starts with the common-kitchen set rather than zero tools, so
# equipment filtering doesn't exclude most of the corpus for every client
# until they fill in a checklist — they uncheck what they don't have,
# same "empty list = no restriction" shape diet_modes/allergies already use
# (except tools_available is never empty by default; see optimizer.
# equipment_blocked, which only filters when the field is actually set).
DEFAULT_TOOLS_AVAILABLE = ["stovetop", "oven", "pots_and_pans", "mixing_bowls"]

DEFAULT_PROFILE = {
    "household": [],
    "allergies": [],
    "dislikes": [],
    "likes": [],
    "cuisine_affinity": [],
    "diet_modes": [],
    "meals_per_day": 3,
    "units": "metric",
    # 0-100: how much a menu may repeat dishes / share ingredients. See
    # engines/optimizer.py overlap_floor_for. Every new client starts at 50.
    "variety": 50,
    # Kitchen equipment on hand — hard-excludes recipes that need something
    # missing (aboyeur/classify_equipment.py, engines/optimizer.equipment_blocked).
    "tools_available": list(DEFAULT_TOOLS_AVAILABLE),
    # Free-text practitioner notes about diet logic — separate from
    # clients.notes (practitioner-facing, not diet logic). Parsed lazily by
    # ensure_notes_parsed into meal_type_hints / meal_prep_requested; the
    # raw text is always what's shown back, the parse is a cache.
    "notes": "",
    "meal_type_hints": {},
    "meal_prep_requested": False,
}

# Guest mode: a /guest session plans against a temporary, never-persisted
# profile. Earlier this lived in a module-level global the Agent set/cleared
# around each turn — fine only because the backend serialized every turn
# behind one process-wide lock. With real concurrent users that global would
# leak one user's guest profile into another's turn, so every profile
# consumer now takes `guest` explicitly instead: passed a dict, operate on
# it in memory only; passed None, read/write the DB as client_id's own row.


def make_guest_profile() -> dict:
    """Fresh, empty guest profile — no household, no allergies, metric units."""
    return json.loads(json.dumps(DEFAULT_PROFILE))


def _load_raw(conn: sqlite3.Connection, client_id: int, guest: dict | None) -> dict:
    if guest is not None:
        return guest
    row = conn.execute("SELECT data_json FROM profile WHERE client_id = ?", (client_id,)).fetchone()
    if row is None:
        return dict(DEFAULT_PROFILE)
    # Encrypted at rest (health data) — see aboyeur/crypto.py encrypt_field.
    return json.loads(decrypt_field(row["data_json"]))


def _save_raw(conn: sqlite3.Connection, client_id: int, profile: dict, guest: dict | None) -> None:
    if guest is not None:
        # In-memory only: keep the guest dict identity so the Agent's
        # reference stays live across tool calls within the turn.
        if profile is not guest:
            guest.clear()
            guest.update(profile)
        return
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        "INSERT INTO profile (client_id, data_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(client_id) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at",
        (client_id, encrypt_field(json.dumps(profile)), now),
    )
    conn.commit()


def get_profile(conn: sqlite3.Connection, client_id: int, guest: dict | None = None) -> dict:
    """Household, computed per-member BMR/TDEE targets, restrictions, preferences.

    When a macro strategy is set (update_profile's set_macro_strategy, used
    by the Macro Kit), each member also gets `strategy_daily_macros` — that
    strategy's daily protein/carb/fat grams. Additive: `targets`' existing
    per_meal / daily_macros keep the 30/40/30 split the menu path reads, and
    the field is absent entirely until a strategy is chosen.
    """
    profile = _load_raw(conn, client_id, guest)
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
    conn: sqlite3.Connection, client_id: int, patch: dict, guest: dict | None = None
) -> dict:
    """Apply a structured patch. Supported keys:

    add_allergies, remove_allergies, add_dislikes, remove_dislikes,
    add_likes, remove_likes (all list[str]) — add/remove semantics, not replace.
    set_diet_modes, set_cuisine_affinity (list[str]) — replace.
    set_meals_per_day (int) — replace, household-wide default.
    set_variety (int 0-100) — replace. 0 = same dishes every day, 50 = default,
    100 = never repeat a dish, no pull toward shared ingredients.
    set_macro_strategy ("balanced"|"endurance"|"cut") — replace, household-wide.
    set_tools_available (list[str]) — replace. Kitchen equipment on hand,
    e.g. ["oven","stovetop","blender"]. Defaults to a common-kitchen set;
    hard-excludes recipes classified as needing something not on the list.
    add_tools_available, remove_tools_available (list[str]) — add/remove.
    set_notes (str) — replace. Free-text diet-logic notes ("eggs are
    breakfast for me", "wants meal-prep-friendly dinners"); re-parsed into
    meal_type_hints/meal_prep_requested the next time a menu is planned.
    add_member (dict with "id") — upsert by id, merged onto any existing
    member with that id (fields you omit are kept, not wiped) — so a
    single-field patch like {"id": "sam", "meals_per_day": 1} to put just
    one person on OMAD doesn't blow away their anthropometrics.
    remove_member (str id).
    set_member_target_override (dict: id, kcal, protein_g, carb_g, fat_g,
    fiber_g?) — manually correct one member's daily calorie/macro target
    when the computed BMR/TDEE number is wrong for them; wins over the
    computed target everywhere it's read (menu planning, the profile
    screen) until cleared. Global to that member, not per-menu.
    clear_member_target_override (str member id) — drop the override and
    go back to the computed BMR/TDEE target.
    """
    profile = _load_raw(conn, client_id, guest)

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
    if "set_variety" in patch:
        profile["variety"] = max(0, min(100, int(round(float(patch["set_variety"])))))
    if "set_macro_strategy" in patch:
        # Household-wide for now (Phase 1): a kit is built for one member,
        # but nothing yet asks two members to be on different strategies,
        # and a per-member field nobody sets is a field that rots.
        strategy = str(patch["set_macro_strategy"]).lower().strip()
        if strategy in nutrition.MACRO_STRATEGIES:
            profile["macro_strategy"] = strategy
    if "set_tools_available" in patch:
        profile["tools_available"] = sorted({
            v.lower().strip() for v in patch["set_tools_available"] if v and v.strip()
        })
    if "add_tools_available" in patch:
        _add("tools_available", patch["add_tools_available"])
    if "remove_tools_available" in patch:
        _remove("tools_available", patch["remove_tools_available"])
    if "set_notes" in patch:
        profile["notes"] = str(patch["set_notes"])
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
    if "set_member_target_override" in patch:
        raw = patch["set_member_target_override"]
        if not isinstance(raw, dict) or "id" not in raw:
            raise ValueError(
                f"set_member_target_override must be an object with an 'id' field, got {raw!r}"
            )
        members = profile.get("household", [])
        if not any(m["id"] == raw["id"] for m in members):
            raise ValueError(f"no household member with id {raw['id']!r}")
        # A partial statement ("she needs 140g protein") keeps the member's
        # current value for every macro not given — otherwise a missing
        # carb_g/fat_g would validate as 0. kcal alone ("give her 1950 kcal")
        # scales the current protein/carb/fat to it, same split.
        macro_keys = ("protein_g", "carb_g", "fat_g", "fiber_g")
        given = {k for k in macro_keys if raw.get(k) is not None}
        if len(given) < len(macro_keys):
            current = nutrition.household_targets(profile).get(raw["id"], {}).get("daily_macros", {})
            raw = {**raw}
            scale = 1.0
            if not given & {"protein_g", "carb_g", "fat_g"} and raw.get("kcal") and current.get("kcal"):
                scale = float(raw["kcal"]) / float(current["kcal"])
            for k in macro_keys:
                if raw.get(k) is None and current.get(k) is not None:
                    raw[k] = round(current[k] * (scale if k != "fiber_g" else 1.0), 1)
        validated = nutrition.validated_recipe_macros(raw)
        if validated is None:
            raise ValueError(
                "target override rejected — kcal must roughly agree with "
                "4*protein + 4*carb + 9*fat, and every value must be >= 0"
            )
        for m in members:
            if m["id"] == raw["id"]:
                m["target_override"] = validated
    if "clear_member_target_override" in patch:
        for m in profile.get("household", []):
            if m["id"] == patch["clear_member_target_override"]:
                m.pop("target_override", None)

    _save_raw(conn, client_id, profile, guest)
    return get_profile(conn, client_id, guest)


def ensure_notes_parsed(
    conn: sqlite3.Connection, config: Config, client_id: int, guest: dict | None = None
) -> dict:
    """{meal_type_hints, meal_prep_requested} for this client, lazily
    (re)computed by an LLM pass over profile.notes and cached against the
    exact notes text — same "parse once, cache it" shape as
    engines/nutrition.parse_ingredients — so plan_menu doesn't re-run the
    pass on every call, only when the notes actually changed.

    Cached in the profile row itself (guest profiles re-parse every call,
    same as the rest of a guest session — nothing about a guest persists).
    """
    raw = _load_raw(conn, client_id, guest)
    notes = (raw.get("notes") or "").strip()
    digest = hashlib.sha256(notes.encode()).hexdigest() if notes else ""
    if raw.get("_notes_parsed_hash") != digest:
        from aboyeur.engines.notes import parse_client_notes
        parsed = parse_client_notes(config, notes)
        raw["meal_type_hints"] = parsed["meal_type_hints"]
        raw["meal_prep_requested"] = parsed["meal_prep_requested"]
        raw["_notes_parsed_hash"] = digest
        _save_raw(conn, client_id, raw, guest)
    return {
        "meal_type_hints": raw.get("meal_type_hints") or {},
        "meal_prep_requested": bool(raw.get("meal_prep_requested")),
    }
