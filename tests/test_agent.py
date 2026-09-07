"""Agent tool-dispatch guard.

The local model sometimes emits tool calls that violate the JSON schema
(missing required args, wrong types). Those must come back to the model as
an error tool result it can correct — not escape agent.send and abort the
streaming HTTP response (seen in logs as KeyError 'query' and TypeError in
update_profile, surfaced to the user as an internal error).
"""

from __future__ import annotations

import sqlite3

import aboyeur.agent as agent_mod
from aboyeur.agent import Agent
from aboyeur.config import Config


class _FakeCollector:
    """Scripted stand-in for StreamCollector: each entry is (content, tool_calls)."""

    script: list[tuple[str, list[dict]]] = []

    def __init__(self, _stream):
        self.content, self.tool_calls = type(self).script.pop(0)

    def __iter__(self):
        if self.content:
            yield self.content


def _scripted_agent(db_conn, config, client_id, monkeypatch, script):
    _FakeCollector.script = list(script)
    monkeypatch.setattr(agent_mod, "StreamCollector", _FakeCollector)
    monkeypatch.setattr(agent_mod, "stream_chat", lambda *a, **k: iter(()))
    monkeypatch.setattr(agent_mod, "make_client", lambda config: None)
    return Agent(db_conn, config, client_id)


def test_missing_required_arg_becomes_error_tool_result(
    db_conn: sqlite3.Connection, config: Config, client_id: int, monkeypatch
):
    agent = _scripted_agent(db_conn, config, client_id, monkeypatch, [
        ("", [{"id": "t1", "name": "search_recipes", "arguments": "{}"}]),
        ("here are some ideas", []),
    ])
    out = list(agent.send("find me dinner"))
    text = "".join(payload for kind, payload in out if kind == "text")
    assert "here are some ideas" in text

    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "invalid arguments" in tool_msg["content"]
    assert "search_recipes" in tool_msg["content"]


def test_wrong_arg_type_becomes_error_tool_result(
    db_conn: sqlite3.Connection, config: Config, client_id: int, monkeypatch
):
    agent = _scripted_agent(db_conn, config, client_id, monkeypatch, [
        ("", [{
            "id": "t1", "name": "update_profile",
            "arguments": '{"add_member": "jose"}',
        }]),
        ("profile noted", []),
    ])
    out = list(agent.send("add jose to the household"))
    text = "".join(payload for kind, payload in out if kind == "text")
    assert "profile noted" in text

    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "invalid arguments" in tool_msg["content"]


def test_guest_profile_overrides_and_never_persists(
    db_conn: sqlite3.Connection, config: Config, client_id: int, monkeypatch,
):
    """A guest session's update_profile writes stay in the guest dict; the
    stored household profile is untouched, and the override is cleared once
    the turn ends."""
    from aboyeur.tools import profile as profile_tools
    from aboyeur.tools.profile import get_profile, update_profile

    update_profile(db_conn, client_id, {"add_allergies": ["peanut"]})  # the household's real profile

    guest = profile_tools.make_guest_profile()
    agent = _scripted_agent(db_conn, config, client_id, monkeypatch, [
        ("", [{
            "id": "t1", "name": "update_profile",
            "arguments": '{"add_allergies": ["shellfish"], "add_member": '
                         '{"id": "guest", "sex": "f", "height_cm": 152.4, "weight_kg": 54.4}}',
        }]),
        ("noted", []),
    ])
    agent.guest_profile = guest
    agent.messages[0] = {"role": "system", "content": "guest test"}

    list(agent.send("im a 5 foot woman, 120 lbs, allergic to shellfish"))

    assert guest["allergies"] == ["shellfish"]
    assert [m["id"] for m in guest["household"]] == ["guest"]
    stored = get_profile(db_conn, client_id)
    assert stored["allergies"] == ["peanut"]  # untouched
    assert stored["household"] == []
    # No module-global to leak: guest_profile lives on this Agent instance
    # only, so a fresh Agent for the same user sees the real stored profile,
    # never the guest's.
    fresh = Agent(db_conn, config, client_id)
    assert fresh.guest_profile is None
    assert get_profile(db_conn, client_id, guest=fresh.guest_profile)["allergies"] == ["peanut"]


def test_guest_system_prompt_flags_guest_mode(db_conn: sqlite3.Connection, config: Config, monkeypatch, client_id: int):
    from aboyeur.tools.profile import make_guest_profile
    monkeypatch.setattr(agent_mod, "make_client", lambda config: None)
    agent = Agent(db_conn, config, client_id, guest_profile=make_guest_profile())
    assert "GUEST MODE ACTIVE" in agent.messages[0]["content"]
