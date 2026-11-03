"""Tests for the tool-use loop inside ChatStreamView._run_turn.

These bypass Django's test Client entirely — the HTTP layer is boring
wiring we already trust, while the tool-use loop has real branching
logic (tool_use → execute → loop, end_turn → stop, error → abort) that
we want to test directly.

We instantiate the view, swap in a fake adapter and a fake tool
executor, and iterate `_run_turn` as an async generator. Everything
runs in a single thread with a single event loop, so we don't have to
contend with sync_to_async, SQLite threading, or streaming_content.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TransactionTestCase

from chat.llm.base import StreamError, TextDelta, ToolUse, TurnEnd
from chat.llm.registry import MODELS
from chat.models import ChatMessage, ChatSession
from chat.views import ChatStreamView, _load_messages_for_model

User = get_user_model()


def _fake_stream(sequences: list[list]):
    """Return a fake adapter.stream() method that cycles through event lists.

    Each call to stream() yields the next list in `sequences`. Used to
    simulate multi-turn tool-use loops: first call returns tool_use +
    tool_use stop_reason, second call returns text + end_turn.
    """
    state = {"i": 0}

    async def stream(self, **kwargs) -> AsyncIterator:
        idx = state["i"]
        state["i"] += 1
        for evt in sequences[idx]:
            yield evt

    return stream


async def _drive(session, context, model_config):
    """Iterate _run_turn and collect all yielded events as a list."""
    view = ChatStreamView()
    events = []
    async for evt in view._run_turn(session, context, model_config):
        events.append(evt)
    return events


class _ExecutorConnectionsClosed:
    """Close the DB connection asgiref's shared sync_to_async thread opened.

    Outside an async_to_sync caller, thread-sensitive sync_to_async runs the
    ORM on one long-lived executor thread with its own connection. The test
    runner closes only the main thread's, so on Postgres that idle session
    blocked dropping the test database ("being accessed by other users").
    """

    def tearDown(self):
        from asgiref.sync import sync_to_async
        from django.db import connections

        asyncio.run(sync_to_async(connections.close_all)())
        super().tearDown()


class ChatStreamViewTests(_ExecutorConnectionsClosed, TransactionTestCase):
    """Uses TransactionTestCase because the loop's sync_to_async ORM helpers
    spawn worker threads that need visibility of main-thread writes."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="chat_stream_test",
            email="chat_stream@example.com",
            password="test-password",
        )
        self.session = ChatSession.objects.create(user=self.user)
        # Seed the user message so _run_turn's history load has something
        # to find. Normally the view's post() method creates this.
        ChatMessage.objects.create(
            session=self.session,
            role="user",
            content="hello",
            context={"path": "/erp/"},
        )
        from chat.llm.registry import get_model
        self.model_config = get_model("claude-sonnet-5")

    def _run(self, sequences, execute_result=None):
        """Drive _run_turn with a fake adapter + tool executor."""
        fake_stream_fn = _fake_stream(sequences)

        async def fake_execute(name, arguments):
            if callable(execute_result):
                return execute_result(name, arguments)
            return execute_result or {"ok": True}

        with patch(
            "chat.views.AnthropicAdapter.stream",
            new=fake_stream_fn,
        ), patch("chat.views.execute_tool_call", new=fake_execute):
            return asyncio.run(_drive(self.session, {"path": "/erp/"}, self.model_config))

    # ---- Text-only turn --------------------------------------------------

    def test_text_only_turn_yields_deltas_and_persists(self):
        events = self._run(
            [[
                TextDelta(text="Hello "),
                TextDelta(text="there"),
                TurnEnd(stop_reason="end_turn", input_tokens=10, output_tokens=2),
            ]],
        )
        types = [e["type"] for e in events]
        self.assertEqual(types.count("text_delta"), 2)
        self.assertEqual(types.count("turn_end"), 1)

        # Assistant message persisted
        asst = ChatMessage.objects.filter(session=self.session, role="assistant").first()
        self.assertIsNotNone(asst)
        self.assertEqual(asst.content, "Hello there")

        # Session metadata bumped
        self.session.refresh_from_db()
        self.assertEqual(self.session.metadata.get("turn_count"), 1)
        self.assertEqual(self.session.metadata.get("total_input_tokens"), 10)

    # ---- Tool-use loop ---------------------------------------------------

    def test_tool_use_executes_then_loops(self):
        events = self._run(
            [
                [
                    ToolUse(id="toolu_1", name="find_brand", input={"query": "sol-ark"}),
                    TurnEnd(stop_reason="tool_use", input_tokens=12, output_tokens=3),
                ],
                [
                    TextDelta(text="Found it."),
                    TurnEnd(stop_reason="end_turn", input_tokens=20, output_tokens=2),
                ],
            ],
            execute_result={
                "match_quality": "exact",
                "item": {"id": "brand-uuid", "title": "Sol-Ark"},
            },
        )
        types = [e["type"] for e in events]
        self.assertIn("tool_use", types)
        self.assertIn("tool_result", types)
        self.assertIn("text_delta", types)

        # Ordering: tool_use before tool_result before text_delta
        self.assertLess(types.index("tool_use"), types.index("tool_result"))
        self.assertLess(types.index("tool_result"), types.index("text_delta"))

        # Persistence: seed user + assistant(tool_use) + tool(result) + assistant(text)
        roles = list(
            ChatMessage.objects.filter(session=self.session)
            .order_by("created_at")
            .values_list("role", flat=True)
        )
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])

    # ---- Error handling --------------------------------------------------

    def test_stream_error_yields_error_and_turn_end(self):
        events = self._run(
            [[
                TextDelta(text="partial "),
                StreamError(message="upstream 500", provider_error={"status": 500}),
                TurnEnd(stop_reason="error"),
            ]],
        )
        errors = [e for e in events if e["type"] == "error"]
        turn_ends = [e for e in events if e["type"] == "turn_end"]
        self.assertEqual(len(errors), 1)
        self.assertIn("upstream 500", errors[0]["message"])
        self.assertEqual(len(turn_ends), 1)

    def test_unknown_tool_becomes_error_envelope(self):
        def fake_execute(name, arguments):
            from chat.tools import UnknownTool
            raise UnknownTool(f"Unknown tool: {name}")

        events = self._run(
            [
                [
                    ToolUse(id="toolu_bad", name="not_a_real_tool", input={}),
                    TurnEnd(stop_reason="tool_use"),
                ],
                [
                    TextDelta(text="Recovered."),
                    TurnEnd(stop_reason="end_turn"),
                ],
            ],
            execute_result=fake_execute,
        )
        # The tool_result frame should carry the UnknownTool envelope
        tool_results = [e for e in events if e["type"] == "tool_result"]
        self.assertEqual(len(tool_results), 1)
        self.assertEqual(tool_results[0]["content"].get("error"), "UnknownTool")

        # And the loop should still recover to produce a final text turn
        self.assertIn("text_delta", [e["type"] for e in events])

    # ---- Safety bound ----------------------------------------------------

    def test_runaway_tool_loop_hits_iteration_limit(self):
        # Every turn emits a tool_use, forever. The safety bound in
        # _run_turn should stop after 8 iterations and yield an error.
        sequences = [
            [
                ToolUse(id=f"toolu_{n}", name="list_brands", input={}),
                TurnEnd(stop_reason="tool_use"),
            ]
            for n in range(20)  # way more than the bound
        ]

        events = self._run(sequences, execute_result=[])

        errors = [e for e in events if e["type"] == "error"]
        self.assertTrue(
            any("iterations" in e.get("message", "") for e in errors),
            f"Expected iteration-limit error, got {[e.get('message') for e in errors]}",
        )


class _FakeAdapter:
    """Minimal adapter double: one text delta then end_turn."""

    async def stream(self, **kwargs) -> AsyncIterator:
        yield TextDelta(text="Hi!")
        yield TurnEnd(stop_reason="end_turn", input_tokens=5, output_tokens=1)


class ChatStreamCsrfTests(_ExecutorConnectionsClosed, TransactionTestCase):
    """SEC-106: /erp/chat/stream/ must enforce CSRF.

    The view used to be @csrf_exempt; MF.3 removed that. These tests go
    through the full HTTP stack with enforce_csrf_checks=True (the normal
    test client silently skips CSRF) to prove a POST without the token is
    rejected and a POST with it streams normally.
    """

    STREAM_URL = "/erp/chat/stream/"

    def setUp(self):
        self.user = User.objects.create_user(
            username="chat_csrf_test",
            is_staff=True,
            email="chat_csrf@example.com",
            password="test-password",
        )
        self.session = ChatSession.objects.create(user=self.user)
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)

    def _payload(self) -> str:
        return json.dumps(
            {
                "session_id": str(self.session.id),
                "message": "hello",
                "context": {"path": "/erp/"},
            }
        )

    def test_post_without_csrf_token_returns_403(self):
        resp = self.client.post(
            self.STREAM_URL, self._payload(), content_type="application/json"
        )
        self.assertEqual(resp.status_code, 403)
        # And nothing was persisted — the request died in CSRF middleware.
        self.assertFalse(
            ChatMessage.objects.filter(session=self.session).exists()
        )

    def test_post_with_csrf_token_streams(self):
        # The login page renders {% csrf_token %}, which sets the cookie —
        # same way the real drawer JS obtains it (getCookie("csrftoken")).
        self.client.get("/erp/login/")
        token = self.client.cookies["csrftoken"].value

        with patch("chat.views.build_adapter", return_value=_FakeAdapter()):
            resp = self.client.post(
                self.STREAM_URL,
                self._payload(),
                content_type="application/json",
                headers={"X-CSRFToken": token},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp["Content-Type"], "text/event-stream")
            body = b"".join(resp.streaming_content).decode("utf-8")

        self.assertIn('"type": "text_delta"', body)
        self.assertIn('"type": "turn_end"', body)


class LoadMessagesWindowTests(_ExecutorConnectionsClosed, TransactionTestCase):
    """Guards the CHAT_HISTORY_WINDOW slicing order.

    An earlier version of _load_messages_for_model sliced
    `.order_by("created_at")[:window]` which returns the OLDEST
    window messages — so once a session crossed the window, every
    new user message fell off the end and the model kept seeing
    the exact same stale history every turn, producing identical
    responses to every follow-up prompt. The fix: order DESCENDING,
    slice, then reverse.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="load_messages_test",
            is_staff=True,
            email="lm@example.com",
            password="pw",
        )
        self.session = ChatSession.objects.create(user=self.user)

    def _create_n_user_messages(self, n: int):
        for i in range(n):
            ChatMessage.objects.create(
                session=self.session,
                role="user",
                content=f"message {i}",
            )

    def test_returns_most_recent_messages_not_oldest(self):
        """Across a 100-message session with window=10, we should see
        messages 90–99, not 0–9."""
        self._create_n_user_messages(100)
        from django.test.utils import override_settings

        with override_settings(CHAT_HISTORY_WINDOW=10):
            loaded = _load_messages_for_model(self.session)

        assert len(loaded) == 10
        # Chronological order within the returned slice
        assert loaded[0]["content"] == "message 90"
        assert loaded[-1]["content"] == "message 99"

    def test_below_window_returns_all_in_order(self):
        """When the session has fewer messages than the window, return
        all of them in chronological order."""
        self._create_n_user_messages(5)
        from django.test.utils import override_settings

        with override_settings(CHAT_HISTORY_WINDOW=10):
            loaded = _load_messages_for_model(self.session)

        assert len(loaded) == 5
        assert [m["content"] for m in loaded] == [
            f"message {i}" for i in range(5)
        ]

    def test_window_cut_mid_tool_round_trims_orphan_tool_result(self):
        """Regression for the `messages.0.content.0: unexpected tool_use_id`
        Anthropic 400: when the window boundary lands between an assistant
        row (tool_use) and its tool row (tool_result), the tool row must
        be dropped from the head — otherwise messages[0] is a user-role
        tool_result with no preceding tool_use, which Anthropic rejects.
        """
        # Older history that will be clipped by the window:
        ChatMessage.objects.create(
            session=self.session, role="user", content="old prompt"
        )
        ChatMessage.objects.create(
            session=self.session,
            role="assistant",
            content="",
            tool_calls=[{"id": "call_A", "name": "find_brand", "input": {}}],
        )
        # This tool row is what we want to land at index 0 of the window.
        ChatMessage.objects.create(
            session=self.session,
            role="tool",
            content="1 row(s) returned",
            tool_calls=[
                {"id": "call_A", "name": "find_brand", "input": {}, "result": {"id": 1}}
            ],
        )
        ChatMessage.objects.create(
            session=self.session, role="assistant", content="Found it"
        )
        ChatMessage.objects.create(
            session=self.session, role="user", content="next question"
        )
        from django.test.utils import override_settings

        # window=3 takes the last three rows: tool, assistant(text), user.
        # Without the fix, messages[0] would be a user message whose first
        # block is a tool_result — the exact shape Anthropic 400s on.
        with override_settings(CHAT_HISTORY_WINDOW=3):
            loaded = _load_messages_for_model(self.session)

        assert loaded, "expected at least one message after trimming"
        first = loaded[0]
        assert first["role"] == "user", f"first message must be user, got {first['role']}"
        # Content should be plain text, not a list of tool_result blocks.
        assert isinstance(first["content"], str), (
            f"first message content must be a plain user string, got {first['content']!r}"
        )
        # And the user's actual new prompt must still be present as the last entry.
        assert loaded[-1]["content"] == "next question"

    def test_newest_user_message_is_always_last(self):
        """Regression guard for the bug the user hit: after enough
        messages accumulate, the NEW user message must still reach
        the model as the last entry in the history."""
        self._create_n_user_messages(30)
        # Simulate what views.post() does: save the new user message
        # right before calling _run_turn.
        ChatMessage.objects.create(
            session=self.session,
            role="user",
            content="can we mark it as paid?",
        )
        from django.test.utils import override_settings

        with override_settings(CHAT_HISTORY_WINDOW=20):
            loaded = _load_messages_for_model(self.session)

        assert len(loaded) == 20
        # The last loaded message must be the one the user just sent.
        assert loaded[-1]["content"] == "can we mark it as paid?"


class SessionDetailModelKeyTests(_ExecutorConnectionsClosed, TransactionTestCase):
    """A retired model_id must not reach the browser.

    chat.js assigns the response's model_id straight to <select>.value. A
    key with no matching <option> blanks the dropdown, so the detail view
    resolves through the registry before serialising.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="chat_detail_test",
            is_staff=True,
            email="chat_detail@example.com",
            password="test-password",
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_retired_model_id_is_resolved_forward(self):
        session = ChatSession.objects.create(user=self.user, model_id="claude-opus-4-7")
        resp = self.client.get(f"/erp/chat/sessions/{session.id}/")
        assert resp.status_code == 200
        assert resp.json()["model_id"] == "claude-opus-5"

    def test_live_model_id_passes_through(self):
        session = ChatSession.objects.create(user=self.user, model_id="claude-haiku-4-5")
        resp = self.client.get(f"/erp/chat/sessions/{session.id}/")
        assert resp.json()["model_id"] == "claude-haiku-4-5"

    def test_returned_key_always_exists_in_the_dropdown(self):
        session = ChatSession.objects.create(user=self.user, model_id="totally-bogus")
        resp = self.client.get(f"/erp/chat/sessions/{session.id}/")
        assert resp.json()["model_id"] in MODELS
