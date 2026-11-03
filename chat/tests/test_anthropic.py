"""Unit tests for AnthropicAdapter.

Uses httpx.MockTransport to avoid live Anthropic calls — tests are fast,
deterministic, and don't depend on API credits. Instead of patching the
adapter's internal client we monkeypatch `httpx.AsyncClient` within the
module so the production code stays unchanged.
"""

from __future__ import annotations

import asyncio
import json
from typing import Iterable
from unittest.mock import patch

import httpx

from chat.llm.anthropic import AnthropicAdapter, AnthropicConfig
from chat.llm.base import StreamError, TextDelta, ToolUse, TurnEnd

CONFIG = AnthropicConfig(api_key="sk-ant-test")


def _sse_lines(events: Iterable[dict]) -> bytes:
    """Pack a list of Anthropic SSE event dicts into the wire format."""
    parts: list[str] = []
    for evt in events:
        parts.append(f"event: {evt['type']}")
        parts.append(f"data: {json.dumps(evt)}")
        parts.append("")
    parts.append("")
    return "\n".join(parts).encode("utf-8")


def _make_adapter_with_mock(response_body: bytes, status: int = 200) -> AnthropicAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=response_body, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)

    class FakeClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    patcher = patch("chat.llm.anthropic.httpx.AsyncClient", FakeClient)
    patcher.start()
    adapter = AnthropicAdapter(CONFIG)
    adapter._patcher = patcher  # type: ignore[attr-defined]
    return adapter


async def _collect(adapter: AnthropicAdapter):
    results = []
    async for event in adapter.stream(
        model="claude-sonnet-5",
        system="test",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
    ):
        results.append(event)
    adapter._patcher.stop()  # type: ignore[attr-defined]
    return results


class TestStreamSuccess:
    def test_text_deltas_yield_text_events(self) -> None:
        body = _sse_lines([
            {"type": "message_start", "message": {"usage": {"input_tokens": 12}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " world"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ])
        adapter = _make_adapter_with_mock(body)
        events = asyncio.run(_collect(adapter))

        text_events = [e for e in events if isinstance(e, TextDelta)]
        assert [e.text for e in text_events] == ["Hello", " world"]

        turn_ends = [e for e in events if isinstance(e, TurnEnd)]
        assert len(turn_ends) == 1
        assert turn_ends[0].stop_reason == "end_turn"
        assert turn_ends[0].input_tokens == 12
        assert turn_ends[0].output_tokens == 5

    def test_tool_use_accumulates_partial_json(self) -> None:
        body = _sse_lines([
            {"type": "message_start", "message": {"usage": {"input_tokens": 20}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_123", "name": "find_brand", "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"query":'},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": ' "sol-ark"}'},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 8}},
            {"type": "message_stop"},
        ])
        adapter = _make_adapter_with_mock(body)
        events = asyncio.run(_collect(adapter))

        tool_events = [e for e in events if isinstance(e, ToolUse)]
        assert len(tool_events) == 1
        assert tool_events[0].id == "toolu_123"
        assert tool_events[0].name == "find_brand"
        assert tool_events[0].input == {"query": "sol-ark"}

        turn_ends = [e for e in events if isinstance(e, TurnEnd)]
        assert turn_ends[0].stop_reason == "tool_use"

    def test_interleaved_text_and_tool_use(self) -> None:
        body = _sse_lines([
            {"type": "message_start", "message": {"usage": {"input_tokens": 30}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Looking up the brand..."}},
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "toolu_999", "name": "find_brand", "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"query": "x"}'},
            },
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 12}},
            {"type": "message_stop"},
        ])
        adapter = _make_adapter_with_mock(body)
        events = asyncio.run(_collect(adapter))

        ordered_types = [type(e).__name__ for e in events]
        assert "TextDelta" in ordered_types
        assert "ToolUse" in ordered_types
        assert ordered_types[-1] == "TurnEnd"


class TestStreamErrors:
    def test_http_error_yields_stream_error_event(self) -> None:
        body = b'{"error":{"type":"invalid_request_error","message":"Bad payload"}}'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, content=body, headers={"content-type": "application/json"})

        transport = httpx.MockTransport(handler)

        class FakeClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        with patch("chat.llm.anthropic.httpx.AsyncClient", FakeClient):
            adapter = AnthropicAdapter(CONFIG)

            async def run():
                events = []
                async for e in adapter.stream(
                    model="claude-sonnet-5",
                    system="test",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                ):
                    events.append(e)
                return events

            events = asyncio.run(run())

        assert len(events) == 1
        assert isinstance(events[0], StreamError)
        assert "400" in events[0].message
        assert events[0].provider_error == {
            "error": {"type": "invalid_request_error", "message": "Bad payload"}
        }

    def test_malformed_tool_json_becomes_error_event(self) -> None:
        body = _sse_lines([
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_bad", "name": "find_brand", "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"query": unclosed'},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}},
            {"type": "message_stop"},
        ])
        adapter = _make_adapter_with_mock(body)
        events = asyncio.run(_collect(adapter))

        errors = [e for e in events if isinstance(e, StreamError)]
        assert len(errors) == 1
        assert "malformed tool-call JSON" in errors[0].message
        assert "find_brand" in errors[0].message

        assert any(isinstance(e, TurnEnd) for e in events)


class TestRegistry:
    def test_exactly_one_default_model(self) -> None:
        from chat.llm.registry import MODELS

        default = [m for m in MODELS.values() if m.default]
        assert len(default) == 1
        assert default[0].key == "claude-sonnet-5"

    def test_get_model_falls_back_to_default(self) -> None:
        from chat.llm.registry import get_model

        result = get_model("some-nonexistent-key")
        assert result.default is True

    def test_get_model_returns_exact_match(self) -> None:
        from chat.llm.registry import get_model

        result = get_model("claude-opus-5")
        assert result.key == "claude-opus-5"
        assert result.adapter == "anthropic"

    def test_retired_key_resolves_through_aliases(self) -> None:
        """Sessions started on an older registry keep their tier, not the default.

        `claude-opus-4-7` is gone from MODELS, but a ChatSession row that
        stored it must still land on Opus — falling through to the default
        would silently downgrade the conversation to Sonnet.
        """
        from chat.llm.registry import get_model

        assert get_model("claude-opus-4-7").key == "claude-opus-5"
        assert get_model("claude-sonnet-4-6").key == "claude-sonnet-5"

    def test_aliases_point_at_live_registry_keys(self) -> None:
        from chat.llm.registry import ALIASES, MODELS

        for retired, current in ALIASES.items():
            assert retired not in MODELS, f"{retired} is still a live key"
            assert current in MODELS, f"{retired} aliases missing key {current}"

    def test_default_model_key_matches_default_entry(self) -> None:
        from chat.llm.registry import default_model_key, get_model

        assert get_model(default_model_key()).default is True

    def test_claude_5_entries_carry_thinking_headroom(self) -> None:
        """max_tokens covers thinking + reply on Claude 5, so it needs raising.

        These entries leave `thinking` unset (adaptive by default); the
        4096-token global would otherwise be split with the reasoning budget
        and truncate answers mid-sentence.
        """
        from chat.llm.registry import MODELS

        for key in ("claude-sonnet-5", "claude-opus-5"):
            mc = MODELS[key]
            assert mc.max_tokens is not None and mc.max_tokens >= 16000
            assert mc.thinking is None

    def test_haiku_sends_no_effort(self) -> None:
        """Haiku 4.5 rejects output_config.effort with a 400."""
        from chat.llm.registry import MODELS

        assert MODELS["claude-haiku-4-5"].effort is None


class TestRequestPayload:
    """The reasoning controls only appear when the registry asks for them."""

    def _capture(self, **stream_kwargs) -> dict:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                content=_sse_lines([{"type": "message_stop"}]),
                headers={"content-type": "text/event-stream"},
            )

        transport = httpx.MockTransport(handler)

        class FakeClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        with patch("chat.llm.anthropic.httpx.AsyncClient", FakeClient):
            adapter = AnthropicAdapter(CONFIG)

            async def run():
                async for _ in adapter.stream(
                    model="claude-sonnet-5",
                    system="test",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    **stream_kwargs,
                ):
                    pass

            asyncio.run(run())
        return captured

    def test_effort_and_thinking_omitted_by_default(self) -> None:
        payload = self._capture()
        assert "output_config" not in payload
        assert "thinking" not in payload

    def test_effort_sent_as_output_config(self) -> None:
        payload = self._capture(effort="medium")
        assert payload["output_config"] == {"effort": "medium"}

    def test_thinking_sent_as_type_object(self) -> None:
        payload = self._capture(thinking="disabled")
        assert payload["thinking"] == {"type": "disabled"}

    def test_max_tokens_passes_through(self) -> None:
        payload = self._capture(max_tokens=16000)
        assert payload["max_tokens"] == 16000
