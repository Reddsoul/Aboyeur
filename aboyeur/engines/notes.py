"""Parse a client's free-text profile notes into structured meal-planning
hints (feature-spec-2026-09-26.md #2).

Same shape as engines.nutrition.parse_ingredients: one LLM pass over a small
piece of text, tolerant JSON extraction, a heuristic-free empty result on
any failure (never raises — a notes parse that can't run is not a reason to
fail plan_menu). Caching lives in profile.py's ensure_notes_parsed, not
here — this module is the pure "text in, structure out" step.
"""

from __future__ import annotations

import json
import re

from aboyeur.config import Config

_SYSTEM_PROMPT = (
    "Read a nutrition client's free-text notes to their practitioner. Extract two things:\n"
    '  meal_type_hints: {"food or dish name": ["breakfast"|"lunch"|"dinner", ...]} '
    "for anything the client says should (or should only) be eaten at a specific "
    'meal, e.g. "steak is breakfast for me" -> {"steak": ["breakfast"]}. Only '
    "include foods the client actually names — don't invent examples.\n"
    "  meal_prep_requested: true if the client wants batch-cook / leftover-"
    "friendly meals (meal prep, cook once eat all week, make-ahead), else false.\n"
    'Reply with ONLY JSON: {"meal_type_hints": {...}, "meal_prep_requested": bool}. '
    'No prose, no markdown fence. Empty/irrelevant notes -> '
    '{"meal_type_hints": {}, "meal_prep_requested": false}.'
)

_EMPTY = {"meal_type_hints": {}, "meal_prep_requested": False}


def parse_client_notes(config: Config, notes: str) -> dict:
    """{meal_type_hints, meal_prep_requested} from free-text notes. Returns
    the empty result for blank notes or on any LLM/parse failure."""
    notes = (notes or "").strip()
    if not notes:
        return dict(_EMPTY)
    try:
        from openai import OpenAI

        client = OpenAI(
            base_url=config.llm_base_url, api_key=config.llm_api_key or "unused",
            timeout=30, max_retries=1,
        )
        resp = client.chat.completions.create(
            model=config.llm_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": notes},
            ],
            temperature=0,
        )
        content = (resp.choices[0].message.content or "").strip()
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
        m = re.search(r"\{.*\}", content, re.DOTALL)
        parsed = json.loads(m.group(0) if m else content)
    except Exception:
        return dict(_EMPTY)

    raw_hints = parsed.get("meal_type_hints")
    hints: dict[str, list[str]] = {}
    if isinstance(raw_hints, dict):
        for key, meal_types in raw_hints.items():
            if not isinstance(key, str) or not key.strip() or not meal_types:
                continue
            types = meal_types if isinstance(meal_types, list) else [meal_types]
            cleaned = sorted({str(t).strip().lower() for t in types if str(t).strip()})
            if cleaned:
                hints[key.strip().lower()] = cleaned

    return {
        "meal_type_hints": hints,
        "meal_prep_requested": bool(parsed.get("meal_prep_requested")),
    }
