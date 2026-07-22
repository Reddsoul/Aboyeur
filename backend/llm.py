"""Thin wrapper over the OpenAI-compatible LiteLLM endpoint (DGX Spark via Tailscale).

ALL LLM calls in the project go through this module. No other file constructs an
OpenAI client. Base URL + default model read from env at import time.
"""
import json
import os
from typing import Iterator, Optional

from openai import OpenAI

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://100.70.6.1:4000/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "fast")

# api_key is required by the SDK but unused (tailnet-gated, no auth).
client = OpenAI(base_url=LLM_BASE_URL, api_key="none")


class ToolCallError(RuntimeError):
    """Raised when a tool_call is expected but the model returned none."""


def chat(messages: list[dict], model: Optional[str] = None, temperature: float = 0.7) -> str:
    """Non-streaming completion. Returns the assistant content string."""
    resp = client.chat.completions.create(
        model=model or LLM_MODEL,
        messages=messages,
        temperature=temperature,
    )
    return resp.choices[0].message.content or ""


def stream_chat(messages: list[dict], model: Optional[str] = None,
                temperature: float = 0.7) -> Iterator[str]:
    """Yield content delta strings. Used by the POST /chat SSE endpoint."""
    stream = client.chat.completions.create(
        model=model or LLM_MODEL,
        messages=messages,
        temperature=temperature,
        stream=True,
    )
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            yield delta.content


def tool_call(messages: list[dict], tools: list[dict],
              model: Optional[str] = None, tool_choice: str = "auto") -> dict:
    """Return the first tool_call as {"name", "arguments"(dict), "id"}.

    Raises ToolCallError if the model returns no tool call.
    """
    resp = client.chat.completions.create(
        model=model or LLM_MODEL,
        messages=messages,
        tools=tools,
        tool_choice=tool_choice,
        temperature=0.2,
    )
    msg = resp.choices[0].message
    calls = getattr(msg, "tool_calls", None)
    if not calls:
        raise ToolCallError("model returned no tool call")
    call = calls[0]
    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        args = {}
    return {"id": call.id, "name": call.function.name, "arguments": args}


def chat_json(messages: list[dict], model: Optional[str] = None,
              temperature: float = 0.2) -> dict:
    """Completion that must return a JSON object. Robust to code fences / prose.

    Returns {} if no parseable JSON is found (caller decides fallback).
    """
    raw = chat(messages, model=model, temperature=temperature)
    return _extract_json(raw)


def _extract_json(raw: str) -> dict:
    if not raw:
        return {}
    raw = raw.strip()
    # Strip ```json ... ``` fences.
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1] if raw.count("```") >= 2 else raw.strip("`")
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    raw = raw.strip()
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"_": val}
    except json.JSONDecodeError:
        pass
    # Last resort: grab the outermost {...}.
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            val = json.loads(raw[start:end + 1])
            return val if isinstance(val, dict) else {"_": val}
        except json.JSONDecodeError:
            return {}
    return {}
