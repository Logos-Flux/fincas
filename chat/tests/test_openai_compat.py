"""Unit tests for OpenAICompatAdapter.

Same strategy as test_anthropic.py: httpx.MockTransport stubs the
upstream, we assert the adapter yields the right Event sequence, and
we verify the Anthropic→OpenAI message + tool translators agree on
shape without live network.
"""

from __future__ import annotations

import asyncio
import json
from typing import Iterable
from unittest.mock import patch

import httpx

from chat.llm.base import StreamError, TextDelta, ToolUse, TurnEnd
from chat.llm.openai_compat import (
    OpenAICompatAdapter,
    OpenAICompatConfig,
    _anthropic_to_openai_messages,
    _anthropic_to_openai_tools,
    _coerce_arg_types,
    _extract_text_tool_calls,
    _try_parse_tool_call,
    _unwrap_typed_value,
)

CONFIG = OpenAICompatConfig(
    base_url="https://api.openai.com",
    api_key="sk-test",
    provider_label="openai",
)


def _sse_lines(events: Iterable[dict]) -> bytes:
    """Pack a list of OpenAI-format streaming chunks into SSE wire format."""
    parts = [f"data: {json.dumps(evt)}" for evt in events]
    parts.append("data: [DONE]")
    parts.append("")
    return "\n".join(parts).encode("utf-8")


def _make_adapter(response_body: bytes, status: int = 200) -> OpenAICompatAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=response_body, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)

    class FakeClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    patcher = patch("chat.llm.openai_compat.httpx.AsyncClient", FakeClient)
    patcher.start()
    adapter = OpenAICompatAdapter(CONFIG)
    adapter._patcher = patcher  # type: ignore[attr-defined]
    return adapter


async def _collect(adapter):
    results = []
    async for event in adapter.stream(
        model="@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        system="test",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
    ):
        results.append(event)
    adapter._patcher.stop()
    return results


# ---- Streaming parser tests ----------------------------------------------


class TestOpenAISseParsing:
    def test_text_deltas_become_text_events(self) -> None:
        body = _sse_lines([
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hello "}, "finish_reason": None}],
            },
            {
                "id": "c1",
                "choices": [{"index": 0, "delta": {"content": "world"}, "finish_reason": None}],
            },
            {
                "id": "c1",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 2},
            },
        ])
        adapter = _make_adapter(body)
        events = asyncio.run(_collect(adapter))

        text_events = [e for e in events if isinstance(e, TextDelta)]
        assert [e.text for e in text_events] == ["Hello ", "world"]

        turn_ends = [e for e in events if isinstance(e, TurnEnd)]
        assert len(turn_ends) == 1
        # OpenAI "stop" maps to our "end_turn"
        assert turn_ends[0].stop_reason == "end_turn"
        assert turn_ends[0].input_tokens == 12
        assert turn_ends[0].output_tokens == 2

    def test_tool_call_accumulates_across_chunks(self) -> None:
        body = _sse_lines([
            {
                "choices": [{
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_abc",
                            "type": "function",
                            "function": {"name": "find_brand", "arguments": ""},
                        }],
                    },
                    "finish_reason": None,
                }],
            },
            {
                "choices": [{
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": '{"query":'}}],
                    },
                    "finish_reason": None,
                }],
            },
            {
                "choices": [{
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": ' "sol-ark"}'}}],
                    },
                    "finish_reason": None,
                }],
            },
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8},
            },
        ])
        adapter = _make_adapter(body)
        events = asyncio.run(_collect(adapter))

        tool_events = [e for e in events if isinstance(e, ToolUse)]
        assert len(tool_events) == 1
        assert tool_events[0].id == "call_abc"
        assert tool_events[0].name == "find_brand"
        assert tool_events[0].input == {"query": "sol-ark"}

        # Tool calls map to "tool_use"
        turn_ends = [e for e in events if isinstance(e, TurnEnd)]
        assert turn_ends[0].stop_reason == "tool_use"

    def test_two_parallel_tool_calls_by_index(self) -> None:
        body = _sse_lines([
            {
                "choices": [{
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "c1", "type": "function", "function": {"name": "find_brand", "arguments": '{"query": "a"}'}},
                            {"index": 1, "id": "c2", "type": "function", "function": {"name": "list_products", "arguments": '{"limit": 5}'}},
                        ],
                    },
                    "finish_reason": None,
                }],
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ])
        adapter = _make_adapter(body)
        events = asyncio.run(_collect(adapter))

        tool_events = [e for e in events if isinstance(e, ToolUse)]
        assert len(tool_events) == 2
        assert {t.name for t in tool_events} == {"find_brand", "list_products"}

    def test_http_error_yields_stream_error(self) -> None:
        body = b'{"error": {"message": "Unauthorized"}}'

        def handler(request):
            return httpx.Response(401, content=body, headers={"content-type": "application/json"})

        transport = httpx.MockTransport(handler)

        class FakeClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        with patch("chat.llm.openai_compat.httpx.AsyncClient", FakeClient):
            adapter = OpenAICompatAdapter(CONFIG)

            async def run():
                events = []
                async for e in adapter.stream(
                    model="x",
                    system="",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                ):
                    events.append(e)
                return events

            events = asyncio.run(run())

        assert any(isinstance(e, StreamError) for e in events)
        err = next(e for e in events if isinstance(e, StreamError))
        assert "401" in err.message
        assert "openai" in err.message


# ---- Message + tool translator tests ------------------------------------


class TestAnthropicToOpenAiMessages:
    def test_system_becomes_system_message(self):
        out = _anthropic_to_openai_messages("be helpful", [])
        assert out == [{"role": "system", "content": "be helpful"}]

    def test_plain_user_string_passthrough(self):
        out = _anthropic_to_openai_messages(
            "", [{"role": "user", "content": "hi"}]
        )
        assert out == [{"role": "user", "content": "hi"}]

    def test_assistant_with_text_and_tool_use(self):
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Looking up..."},
                    {"type": "tool_use", "id": "tu1", "name": "find_brand", "input": {"query": "x"}},
                ],
            }
        ]
        out = _anthropic_to_openai_messages("", messages)
        assert len(out) == 1
        assert out[0]["role"] == "assistant"
        assert out[0]["content"] == "Looking up..."
        tcs = out[0]["tool_calls"]
        assert len(tcs) == 1
        assert tcs[0]["id"] == "tu1"
        assert tcs[0]["function"]["name"] == "find_brand"
        assert json.loads(tcs[0]["function"]["arguments"]) == {"query": "x"}

    def test_tool_only_assistant_uses_empty_string_not_null(self):
        """Workers AI rejects content:null. We emit content:"" instead,
        which standard OpenAI also accepts."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu1", "name": "list_brands", "input": {}},
                ],
            }
        ]
        out = _anthropic_to_openai_messages("", messages)
        assert out[0]["content"] == ""
        assert out[0]["content"] is not None

    def test_user_tool_result_becomes_tool_role(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu1",
                        "content": '{"id": "brand-uuid", "title": "X"}',
                    }
                ],
            }
        ]
        out = _anthropic_to_openai_messages("", messages)
        assert len(out) == 1
        assert out[0]["role"] == "tool"
        assert out[0]["tool_call_id"] == "tu1"
        assert out[0]["content"] == '{"id": "brand-uuid", "title": "X"}'

    def test_full_tool_use_round_trip(self):
        """System + user + assistant(tool_use) + user(tool_result) + assistant(text)."""
        messages = [
            {"role": "user", "content": "find the sol-ark brand"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu1", "name": "find_brand", "input": {"query": "sol-ark"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu1", "content": '{"item": {"id": "b1"}}'},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "Found it: Sol-Ark."}]},
        ]
        out = _anthropic_to_openai_messages("sys", messages)
        assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "assistant"]
        # Second-to-last is the tool echo
        assert out[-2]["tool_call_id"] == "tu1"
        # Last is the final assistant text
        assert out[-1]["content"] == "Found it: Sol-Ark."


class TestCoerceArgTypes:
    """Llama 3.3 via Workers AI often stringifies bool/int/null args.
    The coercer rescues the common cases so downstream tool execution
    sees the right Python types."""

    def test_stringified_true_becomes_bool(self):
        out = _coerce_arg_types({"visible": "true"})
        assert out == {"visible": True}

    def test_stringified_false_becomes_bool(self):
        out = _coerce_arg_types({"visible": "false"})
        assert out == {"visible": False}

    def test_stringified_null_becomes_none(self):
        out = _coerce_arg_types({"brand": "null"})
        assert out == {"brand": None}

    def test_stringified_int_becomes_int(self):
        out = _coerce_arg_types({"limit": "100"})
        assert out == {"limit": 100}

    def test_negative_stringified_int(self):
        out = _coerce_arg_types({"offset": "-5"})
        assert out == {"offset": -5}

    def test_plain_string_passthrough(self):
        out = _coerce_arg_types({"query": "sol-ark"})
        assert out == {"query": "sol-ark"}

    def test_real_bool_passthrough(self):
        out = _coerce_arg_types({"visible": True})
        assert out == {"visible": True}


class TestUnwrapTypedValue:
    """DeepSeek R1 Distill Qwen 32B wraps tool-call arguments as
    `{"type": "string", "value": ...}` instead of the bare value.
    `_unwrap_typed_value` and `_coerce_arg_types` need to recover."""

    def test_unwraps_string_envelope(self):
        assert _unwrap_typed_value({"type": "string", "value": "sol-ark"}) == "sol-ark"

    def test_unwraps_null_envelope(self):
        assert _unwrap_typed_value({"type": "string", "value": None}) is None

    def test_unwraps_integer_envelope(self):
        assert _unwrap_typed_value({"type": "integer", "value": 1}) == 1

    def test_unwraps_boolean_envelope(self):
        assert _unwrap_typed_value({"type": "boolean", "value": True}) is True

    def test_passes_through_real_dict(self):
        # A legit dict (e.g. for the `fields` arg on update_*) must NOT be
        # unwrapped — it has more keys than just type+value.
        real = {"phone": "555-1234", "email": "x@y.com"}
        assert _unwrap_typed_value(real) == real

    def test_passes_through_dict_with_only_value(self):
        # Missing the "type" key — leave alone.
        assert _unwrap_typed_value({"value": "x"}) == {"value": "x"}

    def test_passes_through_primitives(self):
        assert _unwrap_typed_value("sol-ark") == "sol-ark"
        assert _unwrap_typed_value(42) == 42
        assert _unwrap_typed_value(None) is None
        assert _unwrap_typed_value(True) is True


class TestTryParseToolCall:
    def test_valid_shape(self):
        raw = '{"name": "list_orders", "arguments": {"limit": 1}}'
        result = _try_parse_tool_call(raw)
        assert result == {"name": "list_orders", "arguments": {"limit": 1}}

    def test_empty_arguments(self):
        raw = '{"name": "list_brands", "arguments": {}}'
        result = _try_parse_tool_call(raw)
        assert result == {"name": "list_brands", "arguments": {}}

    def test_string_arguments_recovered(self):
        # Some models serialise arguments as a JSON string
        raw = '{"name": "list_orders", "arguments": "{\\"limit\\": 1}"}'
        result = _try_parse_tool_call(raw)
        assert result == {"name": "list_orders", "arguments": {"limit": 1}}

    def test_rejects_non_dict(self):
        assert _try_parse_tool_call("[]") is None
        assert _try_parse_tool_call("42") is None

    def test_rejects_missing_name(self):
        assert _try_parse_tool_call('{"arguments": {}}') is None

    def test_rejects_empty_name(self):
        assert _try_parse_tool_call('{"name": "", "arguments": {}}') is None

    def test_rejects_malformed_json(self):
        assert _try_parse_tool_call('{"name": "x", "arguments":}') is None


class TestExtractTextToolCalls:
    """Three text-embedded tool-call formats that Workers AI models emit
    when the OpenAI-compat shim doesn't recognize their native output."""

    def test_qwen_tools_tag(self):
        content = '<tools>\n{"name": "list_orders", "arguments": {"limit": 1, "ordering": "-order_date"}}\n</tools>'
        calls, remaining = _extract_text_tool_calls(content)
        assert len(calls) == 1
        assert calls[0]["name"] == "list_orders"
        assert calls[0]["arguments"] == {"limit": 1, "ordering": "-order_date"}
        assert remaining == ""

    def test_fenced_json_block(self):
        content = (
            "Here's the function call:\n\n"
            '```json\n'
            '{"name": "list_orders", "arguments": {"limit": 1}}\n'
            '```'
        )
        calls, remaining = _extract_text_tool_calls(content)
        assert len(calls) == 1
        assert calls[0]["name"] == "list_orders"
        # Prose before the fence should remain
        assert "Here's the function call" in remaining
        assert "```" not in remaining

    def test_fenced_without_json_label(self):
        content = '```\n{"name": "list_brands", "arguments": {}}\n```'
        calls, remaining = _extract_text_tool_calls(content)
        assert len(calls) == 1
        assert calls[0]["name"] == "list_brands"

    def test_bare_name_arguments_object(self):
        content = (
            'I will call: {"name": "find_brand", "arguments": {"query": "sol-ark"}} '
            "to find the brand."
        )
        calls, remaining = _extract_text_tool_calls(content)
        assert len(calls) == 1
        assert calls[0]["name"] == "find_brand"
        assert calls[0]["arguments"] == {"query": "sol-ark"}
        assert "I will call:" in remaining
        assert "to find the brand." in remaining

    def test_exact_qwen_coder_output_from_bug(self):
        """The exact content shape the user reported from Qwen 2.5 Coder."""
        content = (
            "To get the last sales order, I'll use the `list_orders` function "
            "with a limit of 1 and ordering by `-order_date`.\n\n"
            "Here's the function call:\n\n"
            "```json\n"
            '{"name": "list_orders", "arguments": {"limit": 1, "ordering": "-order_date"}}\n'
            "```"
        )
        calls, remaining = _extract_text_tool_calls(content)
        assert len(calls) == 1
        assert calls[0]["name"] == "list_orders"
        assert calls[0]["arguments"] == {"limit": 1, "ordering": "-order_date"}

    def test_prose_without_tool_call_unchanged(self):
        content = "I don't know how to answer that question."
        calls, remaining = _extract_text_tool_calls(content)
        assert calls == []
        assert remaining == content

    def test_empty_content(self):
        calls, remaining = _extract_text_tool_calls("")
        assert calls == []
        assert remaining == ""

    def test_does_not_match_json_that_isnt_a_tool_call(self):
        """A JSON object without name+arguments must NOT be misparsed."""
        content = 'Here is some data: {"foo": "bar", "count": 5}'
        calls, remaining = _extract_text_tool_calls(content)
        assert calls == []

    def test_tools_tag_preferred_over_fence(self):
        """If both shapes appear, the <tools> tag wins (it's more explicit)."""
        content = (
            '<tools>{"name": "a", "arguments": {}}</tools>\n'
            '```json\n{"name": "b", "arguments": {}}\n```'
        )
        calls, remaining = _extract_text_tool_calls(content)
        # First shape matched → second is ignored (deliberate: we don't
        # want the same logical call rendered twice)
        assert len(calls) == 1
        assert calls[0]["name"] == "a"


class TestCoerceWithDeepSeekShape:
    """End-to-end: full DeepSeek-style payload through _coerce_arg_types."""

    def test_full_deepseek_list_orders_payload(self):
        # This is the exact shape from the user's bug report
        deepseek_args = {
            "search": {"type": "string", "value": ""},
            "customer": {"type": "string", "value": None},
            "status": {"type": "string", "value": None},
            "ordering": {"type": "string", "value": "-order_date"},
            "limit": {"type": "integer", "value": 1},
        }
        coerced = _coerce_arg_types(deepseek_args)
        assert coerced == {
            "search": None,         # "" → None via the empty-string rule
            "customer": None,
            "status": None,
            "ordering": "-order_date",
            "limit": 1,
        }

    def test_unwrap_then_coerce_stringified_bool(self):
        # Belt and braces: DeepSeek wraps it AND stringifies the value
        out = _coerce_arg_types({"visible": {"type": "boolean", "value": "true"}})
        assert out == {"visible": True}


class TestAnthropicToOpenAiTools:
    def test_translates_name_description_and_parameters(self):
        anthropic_tools = [
            {
                "name": "find_brand",
                "description": "Find a brand by slug",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            }
        ]
        out = _anthropic_to_openai_tools(anthropic_tools)
        assert out == [
            {
                "type": "function",
                "function": {
                    "name": "find_brand",
                    "description": "Find a brand by slug",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                },
            }
        ]

    def test_handles_missing_description_and_schema(self):
        out = _anthropic_to_openai_tools([{"name": "noop"}])
        assert out[0]["function"]["description"] == ""
        assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}}


# ---- Cold-start (503 warmup) retry tests ----------------------------------


WARMUP_CONFIG = OpenAICompatConfig(
    base_url="https://gemma.example.test",
    api_key="sk-test",
    provider_label="Gemma",
    warmup_max_wait_s=90.0,
)


class _FlakyUpstream:
    """httpx.MockTransport handler: N 503s, then a canned 200 response."""

    def __init__(self, failures: int, ok_response: httpx.Response) -> None:
        self.failures = failures
        self.calls = 0
        self._ok = ok_response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls <= self.failures:
            return httpx.Response(
                503,
                json={"error": "no capacity, scaling up"},
                headers={"content-type": "application/json"},
            )
        return self._ok


async def _no_sleep(_seconds: float) -> None:
    """Replace asyncio.sleep so backoff retries run instantly in tests."""


def _run_with_transport(handler, config: OpenAICompatConfig, use_streaming: bool):
    transport = httpx.MockTransport(handler)

    class FakeClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    with patch("chat.llm.openai_compat.httpx.AsyncClient", FakeClient), \
            patch("chat.llm.openai_compat.asyncio.sleep", _no_sleep):
        adapter = OpenAICompatAdapter(config, use_streaming=use_streaming)

        async def run():
            events = []
            async for e in adapter.stream(
                model="gemma",
                system="test",
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
            ):
                events.append(e)
            return events

        return asyncio.run(run())


def _blocking_ok_response(text: str = "Hello from Gemma") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        },
        headers={"content-type": "application/json"},
    )


def _streaming_ok_response(text: str = "Hello from Gemma") -> httpx.Response:
    body = _sse_lines([
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


class TestWarmup503Retry:
    """Modal scale-to-zero serves 503 until the vLLM container is warm
    (~5 min cold start). With warmup_max_wait_s set, the adapter retries
    503s with backoff instead of failing the turn."""

    def test_blocking_retries_503_then_succeeds(self):
        upstream = _FlakyUpstream(failures=2, ok_response=_blocking_ok_response())
        events = _run_with_transport(upstream, WARMUP_CONFIG, use_streaming=False)

        assert upstream.calls == 3
        assert not any(isinstance(e, StreamError) for e in events)
        assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hello from Gemma"]
        assert [e for e in events if isinstance(e, TurnEnd)][0].stop_reason == "end_turn"

    def test_streaming_retries_503_then_succeeds(self):
        upstream = _FlakyUpstream(failures=2, ok_response=_streaming_ok_response())
        events = _run_with_transport(upstream, WARMUP_CONFIG, use_streaming=True)

        assert upstream.calls == 3
        assert not any(isinstance(e, StreamError) for e in events)
        assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hello from Gemma"]

    def test_blocking_gives_friendly_error_when_budget_exhausted(self):
        # Budget smaller than the first backoff step → no retry fits,
        # we get the friendly "waking up" error on the first 503.
        config = OpenAICompatConfig(
            base_url="https://gemma.example.test",
            api_key="sk-test",
            provider_label="Gemma",
            warmup_max_wait_s=0.5,
        )
        upstream = _FlakyUpstream(failures=99, ok_response=_blocking_ok_response())
        events = _run_with_transport(upstream, config, use_streaming=False)

        errors = [e for e in events if isinstance(e, StreamError)]
        assert len(errors) == 1
        assert "waking up" in errors[0].message
        assert "Gemma" in errors[0].message
        assert errors[0].provider_error == {"error": "no capacity, scaling up"}

    def test_streaming_gives_friendly_error_when_budget_exhausted(self):
        config = OpenAICompatConfig(
            base_url="https://gemma.example.test",
            api_key="sk-test",
            provider_label="Gemma",
            warmup_max_wait_s=0.5,
        )
        upstream = _FlakyUpstream(failures=99, ok_response=_streaming_ok_response())
        events = _run_with_transport(upstream, config, use_streaming=True)

        errors = [e for e in events if isinstance(e, StreamError)]
        assert len(errors) == 1
        assert "waking up" in errors[0].message

    def test_503_not_retried_when_warmup_disabled(self):
        # Default config (warmup_max_wait_s=0) keeps the old behavior:
        # 503 is surfaced immediately like any other upstream error.
        upstream = _FlakyUpstream(failures=99, ok_response=_blocking_ok_response())
        events = _run_with_transport(upstream, CONFIG, use_streaming=False)

        assert upstream.calls == 1
        errors = [e for e in events if isinstance(e, StreamError)]
        assert len(errors) == 1
        assert "503" in errors[0].message

    def test_non_503_errors_not_retried_even_with_warmup(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "bad token"})

        events = _run_with_transport(handler, WARMUP_CONFIG, use_streaming=False)
        errors = [e for e in events if isinstance(e, StreamError)]
        assert len(errors) == 1
        assert "401" in errors[0].message


# ---- Adapter selection tests --------------------------------------------


class TestAdapterSelection:
    def test_adapter_selection_builds_anthropic(self):
        from django.test import override_settings

        from chat.llm import build_adapter
        from chat.llm.anthropic import AnthropicAdapter
        from chat.llm.registry import get_model

        with override_settings(ANTHROPIC_API_KEY="sk-ant-test"):
            adapter = build_adapter(get_model("claude-sonnet-5"))
        assert isinstance(adapter, AnthropicAdapter)
