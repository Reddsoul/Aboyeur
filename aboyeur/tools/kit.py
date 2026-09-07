"""build_macro_kit — the Macro Kit's LLM-facing tool.

A kit is a mix-and-match grocery template: a few foods per column plus a
per-meal gram rule, so any bowl assembled from it lands on the member's
macros. That is a different deliverable from plan_menu's "these recipes on
these days", and it stays a different tool, a different table, and a
different engine on purpose — see engines/kit.py.

Persistence mirrors tools/menu.py exactly, including the user_id filter on
BOTH the by-id and the default-to-most-recent branch. The unscoped
"most recent row" query is precisely the shape that leaked menus across
accounts twice before (see HANDOFF); load_kit is public and shared rather
than copy-pasted into tools/shopping.py, because a second copy of that
query is how the leak came back the second time.
"""

from __future__ import annotations

import json
import sqlite3
import time

from aboyeur.config import Config
from aboyeur.engines import kit as kit_engine
from aboyeur.engines.nutrition import daily_kcal_for_member, resolve_macro_targets
from aboyeur.tools.profile import get_profile

DEFAULT_MEALS_PER_DAY = 3
DEFAULT_DAYS = 7


def _save_kit(conn: sqlite3.Connection, user_id: int, kit: dict) -> int:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cur = conn.execute(
        "INSERT INTO kits (user_id, data_json, created_at) VALUES (?, ?, ?)",
        (user_id, json.dumps(kit), now),
    )
    conn.commit()
    return cur.lastrowid


def load_kit(
    conn: sqlite3.Connection, user_id: int, kit_id: int | None
) -> tuple[int, dict] | None:
    """(kit_id, kit) for this user, or None. kit_id=None -> their latest.

    Both branches filter on user_id. Omitting it on the "latest" branch is
    the exact cross-tenant leak this codebase has already shipped twice.
    """
    if kit_id is not None:
        row = conn.execute(
            "SELECT id, data_json FROM kits WHERE id = ? AND user_id = ?", (kit_id, user_id)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, data_json FROM kits WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return row["id"], json.loads(row["data_json"])


def _primary_member(profile: dict) -> dict:
    """The member a kit is built for.

    A kit is one person's portion rule, not a household average — averaging
    two people's macro targets produces a bowl that fits neither. With more
    than one member the first is used and the kit says so in its notes.
    """
    household = profile.get("household") or []
    return household[0] if household else {}


def build_macro_kit(
    conn: sqlite3.Connection,
    config: Config,
    user_id: int,
    meals_per_day: int | None = None,
    days: int = DEFAULT_DAYS,
    emphasis: str | None = None,
    kit_size: dict | None = None,
    include_foods: list[str] | None = None,
    exclude_foods: list[str] | None = None,
    guest: dict | None = None,
) -> dict:
    """Build and persist a Macro Kit for this user's primary member.

    meals_per_day / emphasis default from the profile (the member's own
    meals_per_day, else the household default; the profile's macro_strategy,
    else "balanced"). days is the shopping-list horizon only — a kit is one
    template, not a rotation, so it doesn't change the assembly rule.
    """
    profile = get_profile(conn, user_id, guest=guest)
    member = _primary_member(profile)

    meals = int(meals_per_day or member.get("meals_per_day")
                or profile.get("meals_per_day") or DEFAULT_MEALS_PER_DAY)
    meals = max(1, meals)
    strategy = (emphasis or profile.get("macro_strategy") or "balanced").strip().lower()

    daily_kcal = daily_kcal_for_member(member)
    targets = resolve_macro_targets(member, daily_kcal, strategy)

    # Dislikes are a soft preference, but for a kit they're a hard drop: a
    # column has 3-5 items, so leaving a disliked food in it means a third
    # of the roster is one the member won't cook.
    excludes = list(exclude_foods or []) + list(profile.get("dislikes") or [])

    kit = kit_engine.build_kit(
        conn, config,
        targets=targets,
        meals_per_day=meals,
        days=max(1, int(days or DEFAULT_DAYS)),
        emphasis=targets["strategy"],
        kit_size=kit_size,
        diet_modes=profile.get("diet_modes", []),
        allergies=profile.get("allergies", []),
        exclude_foods=excludes,
        include_foods=include_foods,
    )

    label = member.get("label") or member.get("id")
    if label:
        kit["for_member"] = label
    if len(profile.get("household") or []) > 1:
        kit["notes"].append(
            f"Built for {label or 'the first household member'} — a kit is one "
            "person's portion rule. Ask for a kit per person if others need one."
        )
    if not member:
        kit["notes"].append(
            "No household member on file, so the calorie target is a generic "
            "default. Add height/weight/age/sex for a real one."
        )

    kit_id = _save_kit(conn, user_id, kit)
    return {"kit_id": kit_id, **kit}
