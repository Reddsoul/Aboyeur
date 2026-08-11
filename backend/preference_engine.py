"""User-profile inference + interaction logging + freeform recipe correction.

Phase 1: infer_preferences / update_profile / get_prompt_injection.
Phase 7: parse_correction -> data/user_recipes.jsonl.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from backend import llm

DATA_DIR = Path(os.getenv("DB_PATH", "data/aboyeur.db")).expanduser().resolve().parent
PROFILE_PATH = DATA_DIR / "user_profile.json"
LOG_PATH = DATA_DIR / "interaction_log.jsonl"
USER_RECIPES_PATH = DATA_DIR / "user_recipes.jsonl"

_LIST_KEYS = ("likes", "dislikes", "allergies", "cuisine_affinity")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Profile I/O
# ---------------------------------------------------------------------------

def load_profile() -> dict:
    try:
        return json.loads(PROFILE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"household": [], "preferences": {k: [] for k in _LIST_KEYS},
                "plan_days": 7, "meals_per_day": 1}


def save_profile(profile: dict):
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(profile, indent=2))


# ---------------------------------------------------------------------------
# Interaction log (jsonl)
# ---------------------------------------------------------------------------

def log_interaction(entry: dict):
    """Append one turn to interaction_log.jsonl. File must already exist (Phase 1.2)."""
    entry.setdefault("ts", _now())
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _tail_log(last_n: int) -> list[dict]:
    try:
        lines = LOG_PATH.read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in lines[-last_n:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------------------
# Phase 1 — preference inference
# ---------------------------------------------------------------------------

def get_prompt_injection() -> str:
    """Compact household + preference summary injected into every /chat system prompt."""
    p = load_profile()
    adults = [m for m in p.get("household", []) if m["id"].startswith("adult")]
    kids = [m for m in p.get("household", []) if m["id"].startswith("kid")]

    def cal(m):
        return m.get("daily_kcal") or 2000

    parts = []
    if adults:
        parts.append(f"{len(adults)}A(" + ", ".join(
            f"{m.get('activity', 'moderate')},{cal(m)}cal" for m in adults) + ")")
    if kids:
        parts.append(f"{len(kids)}K(" + ", ".join(
            f"{m.get('activity', 'moderate')},{cal(m)}cal" for m in kids) + ")")
    household = ", ".join(parts) if parts else "unknown"

    prefs = p.get("preferences", {})
    likes = ", ".join(prefs.get("likes", [])) or "none"
    dislikes = ", ".join(prefs.get("dislikes", [])) or "none"
    allergies = ", ".join(prefs.get("allergies", [])) or "none"
    fusion = prefs.get("fusion_tolerance", "medium")

    return (f"Household: {household}. Likes: [{likes}]. Skips: [{dislikes}]. "
            f"Allergies: [{allergies}]. Fusion: {fusion}.")


def infer_preferences(last_n: int = 10) -> dict:
    """LLM reads last N log lines, returns a JSON diff of preference changes."""
    events = _tail_log(last_n)
    if not events:
        return {}
    log_text = "\n".join(json.dumps(e) for e in events)
    profile = load_profile()
    messages = [
        {"role": "system", "content":
            "You infer food preferences from interaction logs. Output ONLY a JSON object "
            "with any of these keys, omitting keys with no change: "
            '{"likes":[...new items...],"dislikes":[...],"allergies":[...],'
            '"cuisine_affinity":[...],"fusion_tolerance":"low|medium|high"}. '
            "Lists are ADDITIVE (items to add). Lowercase short ingredient/cuisine names."},
        {"role": "user", "content":
            f"Current preferences: {json.dumps(profile.get('preferences', {}))}\n\n"
            f"Recent interactions:\n{log_text}\n\nJSON diff:"},
    ]
    return llm.chat_json(messages)


def update_profile(diff: dict) -> dict:
    """Merge an inference diff into user_profile.json. Lists union, scalars overwrite."""
    if not diff:
        return load_profile()
    profile = load_profile()
    prefs = profile.setdefault("preferences", {})
    for key in _LIST_KEYS:
        if key in diff and isinstance(diff[key], list):
            existing = {x.lower() for x in prefs.get(key, [])}
            merged = list(prefs.get(key, []))
            for item in diff[key]:
                if isinstance(item, str) and item.lower() not in existing:
                    merged.append(item.lower())
                    existing.add(item.lower())
            prefs[key] = merged
    if isinstance(diff.get("fusion_tolerance"), str):
        prefs["fusion_tolerance"] = diff["fusion_tolerance"]
    save_profile(profile)
    return profile


# ---------------------------------------------------------------------------
# Phase 7 — freeform recipe correction
# ---------------------------------------------------------------------------

def parse_correction(raw: str) -> dict:
    """Convert freeform correction text to structured recipe JSON, persist it.

    LLM structured output with a regex fallback. Writes one line to user_recipes.jsonl.
    """
    messages = [
        {"role": "system", "content":
            "Convert the user's note into JSON with this exact shape and output ONLY JSON: "
            '{"title": "...", "ingredients": [{"qty": "", "unit": "", "name": ""}], '
            '"steps": ["..."], "notes": "..."}.'},
        {"role": "user", "content": raw},
    ]
    parsed = llm.chat_json(messages)

    if not parsed or "ingredients" not in parsed:
        parsed = _regex_fallback(raw)

    record = {
        "ts": _now(),
        "source": "user_correction",
        "raw": raw,
        "title": parsed.get("title") or "user correction",
        "ingredients": parsed.get("ingredients", []),
        "steps": parsed.get("steps", []),
        "notes": parsed.get("notes", ""),
    }
    USER_RECIPES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with USER_RECIPES_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return record


_ING_LINE = re.compile(r"(\d[\d/\.\s]*)\s*(\w+)?\s+(?:of\s+)?([a-zA-Z][a-zA-Z\s\-]+)")


def _regex_fallback(raw: str) -> dict:
    """Best-effort ingredient extraction when the LLM returns invalid JSON."""
    ingredients = []
    for m in _ING_LINE.finditer(raw):
        qty, unit, name = m.group(1).strip(), (m.group(2) or "").strip(), m.group(3).strip()
        ingredients.append({"qty": qty, "unit": unit, "name": name.lower()})
    return {"title": "user correction", "ingredients": ingredients, "steps": [], "notes": raw}
