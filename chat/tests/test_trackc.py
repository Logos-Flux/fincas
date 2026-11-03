"""Track C: chat/LLM drawer — ordered streaming, tool allowlist, key fail-closed.

Covers docs/testing-plan.md §3 "Chat/LLM drawer": provider adapters
mocked at the HTTP boundary, streaming chunks assembling in order, tool
calls validated against the allowlist, and missing API key failing
closed with a clean error (never a traceback).
"""

import asyncio
import json
from unittest.mock import patch

import httpx
from django.test import TestCase

from chat.llm.anthropic import AnthropicAdapter, AnthropicConfig
from chat.llm.base import StreamError, TextDelta, ToolUse, TurnEnd
from chat.llm.openai_compat import OpenAICompatAdapter, OpenAICompatConfig
from chat.tools import UnknownTool, execute_tool_call


def _anthropic_sse(events):
    parts = []
    for evt in events:
        parts.append(f"event: {evt['type']}")
        parts.append(f"data: {json.dumps(evt)}")
        parts.append("")
    parts.append("")
    return "\n".join(parts).encode("utf-8")


def _openai_sse(events):
    parts = [f"data: {json.dumps(evt)}" for evt in events]
    parts.append("data: [DONE]")
    parts.append("")
    return "\n".join(parts).encode("utf-8")


def _fake_client_module(module_path, transport):
    class FakeClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    return patch(f"{module_path}.httpx.AsyncClient", FakeClient)


def _text_events(*texts):
    evts = [{"type": "message_start", "message": {"usage": {"input_tokens": 1}}}]
    evts.append({"type": "content_block_start", "index": 0,
                 "content_block": {"type": "text", "text": ""}})
    for t in texts:
        evts.append({"type": "content_block_delta", "index": 0,
                     "delta": {"type": "text_delta", "text": t}})
    evts.append({"type": "content_block_stop", "index": 0})
    evts.append({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                 "usage": {"output_tokens": 3}})
    evts.append({"type": "message_stop"})
    return evts


def _collect(coro_fn):
    return asyncio.run(coro_fn())


class TestStreamingOrder(TestCase):
    def test_anthropic_chunks_assemble_in_order(self):
        body = _anthropic_sse(_text_events("Hello", ", ", "world", "!"))
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, content=body,
                                       headers={"content-type": "text/event-stream"})
        )
        patcher = _fake_client_module("chat.llm.anthropic", transport)
        patcher.start()
        self.addCleanup(patcher.stop)
        adapter = AnthropicAdapter(AnthropicConfig(api_key="sk-test"))

        async def run():
            out = []
            async for evt in adapter.stream(
                model="m", system="s",
                messages=[{"role": "user", "content": "hi"}], tools=[],
            ):
                out.append(evt)
            return out

        events = _collect(run)
        texts = [e.text for e in events if isinstance(e, TextDelta)]
        self.assertEqual(texts, ["Hello", ", ", "world", "!"])
        self.assertEqual("".join(texts), "Hello, world!")
        self.assertTrue(any(isinstance(e, TurnEnd) for e in events))

    def test_openai_compat_chunks_assemble_in_order(self):
        def chunk(text):
            return {"choices": [{"delta": {"content": text}, "index": 0}]}

        body = _openai_sse([chunk("Foo"), chunk("Bar"), chunk("Baz")])
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, content=body,
                                       headers={"content-type": "text/event-stream"})
        )
        patcher = _fake_client_module("chat.llm.openai_compat", transport)
        patcher.start()
        self.addCleanup(patcher.stop)
        adapter = OpenAICompatAdapter(
            OpenAICompatConfig(base_url="https://x.test", api_key="sk-test")
        )

        async def run():
            out = []
            async for evt in adapter.stream(
                model="m", system="s",
                messages=[{"role": "user", "content": "hi"}], tools=[],
            ):
                out.append(evt)
            return out

        events = _collect(run)
        texts = [e.text for e in events if isinstance(e, TextDelta)]
        self.assertEqual("".join(texts), "FooBarBaz")

    def test_anthropic_tool_call_arguments_reassembled(self):
        arg_json = '{"sku": "WIDGET-1", "quantity": 3}'
        halves = [arg_json[:15], arg_json[15:]]
        evts = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "toolu_1",
                               "name": "find_variant_by_sku"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": halves[0]}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": halves[1]}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ]
        body = _anthropic_sse(evts)
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, content=body,
                                       headers={"content-type": "text/event-stream"})
        )
        patcher = _fake_client_module("chat.llm.anthropic", transport)
        patcher.start()
        self.addCleanup(patcher.stop)
        adapter = AnthropicAdapter(AnthropicConfig(api_key="sk-test"))

        async def run():
            out = []
            async for evt in adapter.stream(
                model="m", system="s",
                messages=[{"role": "user", "content": "hi"}], tools=[],
            ):
                out.append(evt)
            return out

        events = _collect(run)
        tools = [e for e in events if isinstance(e, ToolUse)]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].name, "find_variant_by_sku")
        self.assertEqual(tools[0].input, {"sku": "WIDGET-1", "quantity": 3})


class TestToolAllowlist(TestCase):
    def test_unknown_tool_rejected(self):
        with self.assertRaises(UnknownTool):
            asyncio.run(execute_tool_call("drop_all_tables", {}))

    def test_bad_arguments_return_envelope_not_traceback(self):
        result = asyncio.run(
            execute_tool_call("list_brands", {"nonsense_kwarg": 42})
        )
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("error"), "BadArguments")


class TestMissingKeyFailsClosed(TestCase):
    def test_anthropic_empty_key_yields_clean_error_no_http(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, content=b"")

        transport = httpx.MockTransport(handler)
        patcher = _fake_client_module("chat.llm.anthropic", transport)
        patcher.start()
        self.addCleanup(patcher.stop)
        adapter = AnthropicAdapter(AnthropicConfig(api_key=""))

        async def run():
            out = []
            async for evt in adapter.stream(
                model="m", system="s",
                messages=[{"role": "user", "content": "hi"}], tools=[],
            ):
                out.append(evt)
            return out

        events = _collect(run)  # must not raise
        errors = [e for e in events if isinstance(e, StreamError)]
        self.assertEqual(len(events), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("not configured", errors[0].message.lower())
        self.assertEqual(calls, [])

    def test_openai_compat_empty_key_yields_clean_error_no_http(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, content=b"")

        transport = httpx.MockTransport(handler)
        patcher = _fake_client_module("chat.llm.openai_compat", transport)
        patcher.start()
        self.addCleanup(patcher.stop)
        adapter = OpenAICompatAdapter(
            OpenAICompatConfig(base_url="https://x.test", api_key="")
        )

        async def run():
            out = []
            async for evt in adapter.stream(
                model="m", system="s",
                messages=[{"role": "user", "content": "hi"}], tools=[],
            ):
                out.append(evt)
            return out

        events = _collect(run)  # must not raise
        errors = [e for e in events if isinstance(e, StreamError)]
        self.assertEqual(len(errors), 1)
        self.assertIn("not configured", errors[0].message.lower())
        self.assertEqual(calls, [])
