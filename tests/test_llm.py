"""StreamCollector assembly guards.

The local model occasionally streams a degenerate tool-call delta: an index
(sometimes an id) with no function name and no arguments ever arriving.
Emitting that as a real tool call writes {"function": {"name": null}} into the
message history, which the LiteLLM/vLLM endpoint then rejects with a 400 on the
*next* round — bricking the whole conversation (seen in logs as
"agent stream failed mid-response" / BadRequestError).
"""

from __future__ import annotations

from types import SimpleNamespace

from aboyeur.llm import StreamCollector


def _chunk(*, content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _tc(index, *, id=None, name=None, arguments=None):
    fn = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=id, function=fn)


def test_named_tool_call_is_assembled():
    collector = StreamCollector(iter([
        _chunk(tool_calls=[_tc(0, id="t1", name="search_recipes")]),
        _chunk(tool_calls=[_tc(0, arguments='{"query": ')]),
        _chunk(tool_calls=[_tc(0, arguments='"steak"}')]),
    ]))
    list(collector)  # drain
    assert collector.tool_calls == [
        {"id": "t1", "name": "search_recipes", "arguments": '{"query": "steak"}'}
    ]


def test_nameless_tool_call_delta_is_dropped():
    collector = StreamCollector(iter([
        _chunk(tool_calls=[_tc(0, id="chatcmpl-tool-85a6", name=None, arguments="")]),
    ]))
    list(collector)
    assert collector.tool_calls == []


def test_nameless_delta_does_not_swallow_the_answer():
    """A degenerate tool-call delta must not discard held content — with no
    real tool call, the content is the answer, not filler."""
    collector = StreamCollector(iter([
        _chunk(content="here "),
        _chunk(content="you go"),
        _chunk(tool_calls=[_tc(0, id="x", name=None)]),
    ]))
    streamed = "".join(collector)
    assert streamed == "here you go"
    assert collector.content == "here you go"
    assert collector.tool_calls == []


def test_named_tool_call_still_drops_filler_content():
    collector = StreamCollector(iter([
        _chunk(content="\n\n"),
        _chunk(tool_calls=[_tc(0, id="t1", name="get_recipe")]),
        _chunk(tool_calls=[_tc(0, arguments='{"id": 3}')]),
    ]))
    streamed = "".join(collector)
    assert streamed == ""
    assert collector.content == ""
    assert collector.tool_calls == [{"id": "t1", "name": "get_recipe", "arguments": '{"id": 3}'}]
