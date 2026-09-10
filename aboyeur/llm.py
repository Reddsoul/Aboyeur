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
        # Asks the endpoint for a final usage-only chunk so the admin console
        # can chart token spend. Purely advisory: a self-hosted vLLM/LiteLLM
        # build may ignore it, in which case StreamCollector.usage stays None
        # and the console shows tokens as unknown rather than as zero.
        stream_options={"include_usage": True},
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
        # (prompt_tokens, completion_tokens) from the endpoint's final usage
        # chunk, or None if it never sent one. Best-effort by design.
        self.usage: tuple[int | None, int | None] | None = None

    def __iter__(self) -> Iterator[str]:
        pending: str | None = None
        for chunk in self._chunks:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                self.usage = (
                    getattr(usage, "prompt_tokens", None),
                    getattr(usage, "completion_tokens", None),
                )
            # The usage chunk carries an empty `choices` list — reading
            # choices[0] on it would IndexError and kill the stream one chunk
            # short of the answer being flushed.
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.tool_calls:
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
                # Only a *named* tool call means the held content was filler for
                # a tool round. A degenerate nameless delta (see .tool_calls)
                # gets dropped later, so keep the pending answer chunk.
                if any(s["name"] for s in self.tool_call_parts.values()):
                    pending = None
            if delta.content:
                if pending is not None:
                    self.content_parts.append(pending)
                    yield pending
                pending = delta.content
        if pending is not None and not any(
            s["name"] for s in self.tool_call_parts.values()
        ):
            self.content_parts.append(pending)
            yield pending

    @property
    def content(self) -> str:
        return "".join(self.content_parts)

    @property
    def tool_calls(self) -> list[dict]:
        # The local model occasionally streams a degenerate tool-call delta —
        # an index (sometimes an id) with no function name and no arguments
        # ever arriving. Emitting that as a real tool call puts
        # {"function": {"name": null}} into the message history, which the
        # LiteLLM/vLLM endpoint then rejects with a 400 on the *next* round,
        # bricking the whole conversation. Drop any slot that never got a name.
        return [
            self.tool_call_parts[i]
            for i in sorted(self.tool_call_parts)
            if self.tool_call_parts[i]["name"]
        ]
