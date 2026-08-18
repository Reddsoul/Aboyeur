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


def _scripted_agent(db_conn, config, monkeypatch, script):
    _FakeCollector.script = list(script)
    monkeypatch.setattr(agent_mod, "StreamCollector", _FakeCollector)
    monkeypatch.setattr(agent_mod, "stream_chat", lambda *a, **k: iter(()))
    monkeypatch.setattr(agent_mod, "make_client", lambda config: None)
    return Agent(db_conn, config)


def test_missing_required_arg_becomes_error_tool_result(
    db_conn: sqlite3.Connection, config: Config, monkeypatch
):
    agent = _scripted_agent(db_conn, config, monkeypatch, [
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
    db_conn: sqlite3.Connection, config: Config, monkeypatch
):
    agent = _scripted_agent(db_conn, config, monkeypatch, [
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
