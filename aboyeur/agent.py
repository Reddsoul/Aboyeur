"""The LLM loop: system prompt, native tool calling, tool dispatch.

Allergy safety note: the allergy list is injected into search_recipes here,
by the dispatcher, from the profile — never taken from the LLM's tool-call
arguments (allergies isn't even in that tool's JSON schema, see
tools/schemas.py). The model can't forget to pass it because it's never
given the chance to pass it at all.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Iterator

from aboyeur.config import Config
from aboyeur.llm import StreamCollector, make_client, stream_chat
from aboyeur.prompts import build_system_prompt
from aboyeur.tools import (
    estimate_nutrition,
    get_menu_recipes,
    get_profile,
    get_recipe,
    get_shopping_list,
    manage_pantry,
    plan_menu,
    search_nutrition_reference,
    search_recipes,
    swap_menu_item,
    update_profile,
)
from aboyeur.tools.schemas import TOOL_SCHEMAS

logger = logging.getLogger("aboyeur")

# No cross-agent lock here on purpose. An earlier version of this wrapped
# every tool dispatch in a lock to protect a *shared* sqlite3.Connection
# from concurrent execute() calls (check_same_thread=False only disables the
# same-thread assertion, it doesn't make one connection object safe for
# concurrent use) — but that lock ended up wrapping plan_menu's real network
# calls (USDA lookups, LLM ingredient parsing) too, so one user's slow
# nutrition estimate blocked every other user's turn, including chats that
# never touch a recipe. The actual fix is upstream: each Agent gets its own
# sqlite3.Connection (see backend/main.py's _get_session_agent /
# _reset_session_agent and the CLI's cmd_chat) — WAL mode supports many
# concurrent connections to the same file safely, so nothing here needs to
# serialize at all once connections aren't shared across threads.

MAX_TOOL_ROUNDS = 60  # guard against a runaway tool-call loop. plan_menu now handles
# meals_per_day internally in one call (see tools/menu.py) instead of needing one
# LLM round trip per meal — that per-meal-round-trip design previously burned all
# 60 rounds on an 18-meal request with zero output. 60 is now a generous ceiling
# covering the realistic worst case (multiple search_recipes rounds, one plan_menu
# call, one get_recipe per day+meal slot) even if the model
# can't batch tool calls into a single round


def _tool_result_text(name: str, result: dict) -> str:
    """Serialize a tool result for the model's context.

    get_recipe's markdown is meant to be echoed back verbatim (see system
    prompt), but this is a mid-size local model, not a JSON-aware runtime —
    it reproduces tool content character-for-character rather than decoding
    escapes. json.dumps would hand it back "\\n" and "\\u00be" as literal
    text, which it then parrots straight into the user-facing answer. Plain
    text with real newlines/unicode sidesteps that; every other tool's
    result is small and structured enough that JSON is the better fit.
    """
    if name == "get_menu_recipes" and "markdown" in result:
        # Same verbatim-echo contract as get_recipe; headings are already in
        # the markdown, so hand it over as plain text.
        return result["markdown"]
    if name == "get_recipe" and "markdown" in result:
        header_lines = [f"# {result.get('title', '')} (id {result.get('id')})"]
        if result.get("path"):
            header_lines.append(f"path: {result['path']}")
        if result.get("servings"):
            header_lines.append(f"servings: {result['servings']}")
        if result.get("scale_multiplier"):
            header_lines.append(
                f"scale_multiplier: {result['scale_multiplier']} — {result.get('scale_note', '')}"
            )
        return "\n".join(header_lines) + "\n\n" + result["markdown"]
    return json.dumps(result, default=str)


def _dispatch(
    conn: sqlite3.Connection,
    config: Config,
    user_id: int,
    guest_profile: dict | None,
    name: str,
    args: dict,
) -> dict:
    if name == "search_recipes":
        profile = get_profile(conn, user_id, guest=guest_profile)
        pantry_rows = conn.execute(
            "SELECT item FROM pantry WHERE user_id = ?", (user_id,)
        ).fetchall()
        pantry = [r["item"] for r in pantry_rows]
        results = search_recipes(
            conn, args["query"],
            allergies=profile.get("allergies", []),
            cuisine=args.get("cuisine"), book=args.get("book"),
            include_ingredients=args.get("include_ingredients"),
            exclude_ingredients=args.get("exclude_ingredients"),
            pantry=pantry,
            limit=args.get("limit", 25),
        )
        return {"results": results}

    if name == "get_recipe":
        return get_recipe(conn, args["id"], scale_to_servings=args.get("scale_to_servings"))

    if name == "get_profile":
        return get_profile(conn, user_id, guest=guest_profile)

    if name == "update_profile":
        return update_profile(conn, user_id, args, guest=guest_profile)

    if name == "manage_pantry":
        return {"pantry": manage_pantry(conn, user_id, args["action"], args.get("items"))}

    if name == "estimate_nutrition":
        return estimate_nutrition(conn, config, args["recipe_ids"])

    if name == "plan_menu":
        return plan_menu(
            conn, config, user_id, args["candidate_ids"], args["days"],
            meals_per_day=args.get("meals_per_day", 1),
            constraints=args.get("constraints"),
        )

    if name == "swap_menu_item":
        return swap_menu_item(
            conn, config, user_id, args["day"], args["replacement_id"],
            menu_id=args.get("menu_id"), meal=args.get("meal", 1),
        )

    if name == "get_menu_recipes":
        return get_menu_recipes(conn, user_id, args.get("menu_id"))

    if name == "get_shopping_list":
        return get_shopping_list(conn, config, user_id, args.get("menu_id"))

    if name == "search_nutrition_reference":
        return {"results": search_nutrition_reference(conn, args["query"], limit=args.get("limit", 5))}

    return {"error": f"unknown tool {name!r}"}


class Agent:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: Config,
        user_id: int,
        history: list[dict] | None = None,
        guest_profile: dict | None = None,
    ):
        """user_id: whose profile/pantry/menus this agent's tool calls read
        and write. Never trust a caller-supplied value for this — it must
        come from an authenticated session (see backend/main.py's
        get_current_user), not a request body field.

        history: prior user/assistant turns to seed the conversation with
        (e.g. reconstructing an Agent per-request from an OpenAI-compat
        client that resends its own full history — see backend/main.py's
        oa_chat_completions). Only role/content pairs are needed; a turn's
        internal tool-call round trips aren't preserved across turns, only
        within the one currently being answered.

        guest_profile: when set, this in-memory dict replaces the stored
        household profile for every tool in this agent's turns (targets,
        allergen filter, system prompt), and update_profile writes into it
        instead of the DB — nothing a guest says is persisted. Held as
        plain instance state (not a module global) so concurrent agents for
        different users/sessions can never see each other's guest profile.
        """
        self.conn = conn
        self.config = config
        self.user_id = user_id
        self.guest_profile = guest_profile
        self.client = make_client(config)
        system = build_system_prompt(
            get_profile(conn, user_id, guest=guest_profile), guest=guest_profile is not None
        )
        self.messages: list[dict] = [{"role": "system", "content": system}]
        if history:
            self.messages.extend(history)

    def _refresh_system_prompt(self) -> None:
        self.messages[0] = {
            "role": "system",
            "content": build_system_prompt(
                get_profile(self.conn, self.user_id, guest=self.guest_profile),
                guest=self.guest_profile is not None,
            ),
        }

    def send(self, user_message: str, debug: bool = False) -> Iterator[tuple[str, object]]:
        """One user turn. Yields ("text", str) chunks of the final answer as
        they stream, and — only when debug=True — ("tool_call", dict) /
        ("tool_result", dict) events for each tool the model invokes along
        the way.
        """
        yield from self._send_turn(user_message, debug)

    def _send_turn(self, user_message: str, debug: bool) -> Iterator[tuple[str, object]]:
        self.messages.append({"role": "user", "content": user_message})

        for _ in range(MAX_TOOL_ROUNDS):
            collector = StreamCollector(stream_chat(self.client, self.config, self.messages, TOOL_SCHEMAS))
            for text_chunk in collector:
                yield ("text", text_chunk)

            if not collector.tool_calls:
                self.messages.append({"role": "assistant", "content": collector.content})
                return

            self.messages.append({
                "role": "assistant",
                "content": collector.content or None,
                "tool_calls": [
                    {
                        "id": tc["id"], "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                    for tc in collector.tool_calls
                ],
            })

            called_update_profile = False
            for tc in collector.tool_calls:
                try:
                    args = json.loads(tc["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                if debug:
                    yield ("tool_call", {"name": tc["name"], "args": args})
                # The local model sometimes emits tool calls that violate the
                # JSON schema (missing required args, wrong types). Feed the
                # failure back as a tool result so the model can correct and
                # retry, instead of letting the exception kill the whole
                # streaming response.
                try:
                    result = _dispatch(
                        self.conn, self.config, self.user_id, self.guest_profile,
                        tc["name"], args,
                    )
                except (KeyError, TypeError, ValueError) as e:
                    logger.warning(
                        "tool %s failed on args %r: %s: %s",
                        tc["name"], args, type(e).__name__, e,
                    )
                    result = {
                        "error": (
                            f"invalid arguments for {tc['name']}: {type(e).__name__}: {e}. "
                            "Re-check the tool's parameter schema and call it again "
                            "with all required arguments."
                        )
                    }
                if debug:
                    yield ("tool_result", {"name": tc["name"], "result": result})
                if tc["name"] == "update_profile":
                    called_update_profile = True
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": _tool_result_text(tc["name"], result),
                })

            if called_update_profile:
                self._refresh_system_prompt()

        yield ("text", "\n\n(stopped after too many tool-call rounds without a final answer)")
