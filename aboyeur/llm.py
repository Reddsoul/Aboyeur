"""LiteLLM client wrapper. OpenAI-compatible (task.md §11), native tool calling.

Verified against the live `hard` endpoint before this was written (Qwen3.6-27B-VL
via LiteLLM on Spark): both plain and *streaming* tool-calling round trips
return real tool_calls with valid, incrementally-assembled JSON arguments —
no JSON-mode fallback parser needed. If that ever stops being reliable, the
fallback task.md mentions belongs here, isolated behind these two functions.
"""

from __future__ import annotations

from typing import Iterator

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from aboyeur.config import Config


def make_client(config: Config) -> OpenAI:
    # "unused" keeps a keyless self-hosted LiteLLM working; a real key
    # arrives via the user's stored LLM settings or LLM_API_KEY.
    return OpenAI(base_url=config.llm_base_url, api_key=config.llm_api_key or "unused")


def stream_chat(
    client: OpenAI,
    config: Config,
    messages: list[ChatCompletionMessageParam],
    tools: list[dict],
):
    """Raw streaming completion — yields ChatCompletionChunk objects."""
    return client.chat.completions.create(
        model=config.llm_model,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        stream=True,
    )


class StreamCollector:
    """Consumes a streaming tool-calling completion once, assembling tool_calls
    by index while yielding content chunks as text arrives.

    The model observably emits filler content (e.g. a bare "\\n\\n") before
    deciding to call a tool — streaming that straight to a terminal would
    show junk ahead of every tool call. This holds back exactly one content
    chunk at a time; if a tool_call delta shows up next, that pending chunk
    is dropped (it was filler for a tool-calling round, not an answer).
    Otherwise it flushes on the next content chunk or at stream end. Costs
    a one-chunk delay on genuine streamed answers — imperceptible in a REPL.
    """

    def __init__(self, chunks: Iterator):
        self._chunks = chunks
        self.content_parts: list[str] = []
        self.tool_call_parts: dict[int, dict] = {}

    def __iter__(self) -> Iterator[str]:
        pending: str | None = None
        for chunk in self._chunks:
            delta = chunk.choices[0].delta
            if delta.tool_calls:
                pending = None  # a tool-call round — any held content was filler
                for tc_delta in delta.tool_calls:
                    slot = self.tool_call_parts.setdefault(
                        tc_delta.index, {"id": None, "name": None, "arguments": ""}
                    )
                    if tc_delta.id:
                        slot["id"] = tc_delta.id
                    if tc_delta.function and tc_delta.function.name:
                        slot["name"] = tc_delta.function.name
                    if tc_delta.function and tc_delta.function.arguments:
                        slot["arguments"] += tc_delta.function.arguments
            if delta.content:
                if pending is not None:
                    self.content_parts.append(pending)
                    yield pending
                pending = delta.content
        if pending is not None and not self.tool_call_parts:
            self.content_parts.append(pending)
            yield pending

    @property
    def content(self) -> str:
        return "".join(self.content_parts)

    @property
    def tool_calls(self) -> list[dict]:
        return [self.tool_call_parts[i] for i in sorted(self.tool_call_parts)]
