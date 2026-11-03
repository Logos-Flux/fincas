"""Chat views.

Phase 4 adds the real ChatStreamView: a POST-only endpoint that drives
the full tool-use loop against the model provider and streams the
result back as Server-Sent Events. Everything the user sees — the
assistant's text, tool calls as they happen, tool results, turn end —
flows through this one generator.

Production serves this under ASGI (uvicorn): StreamingHttpResponse is
handed an async generator and Django's ASGI handler iterates it
natively on the event loop, so one worker can multiplex many
concurrent SSE streams. Under WSGI (plain `manage.py runserver` in
dev) we fall back to a thin sync driver that runs the same async
generator on a private event loop, one frame at a time — same frames,
still incremental, just one thread per stream.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from typing import Any, AsyncIterator, Iterator

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.handlers.asgi import ASGIRequest
from django.http import JsonResponse, StreamingHttpResponse
from django.views import View

from .llm import build_adapter
from .llm.anthropic import AnthropicAdapter  # noqa: F401
from .llm.base import ModelResolved, StreamError, TextDelta, ToolUse, TurnEnd
from .llm.registry import default_model_key, get_model, is_known
from .llm.tool_schema import anthropic_tool_schemas
from .models import ChatMessage, ChatSession
from .tools import UnknownTool, execute_tool_call

# ---- Read endpoints (Phase 1 stubs promoted — still minimal) -------------


class SessionsListView(LoginRequiredMixin, View):
    """GET /erp/chat/sessions/ — signed-in user's recent sessions."""

    def get(self, request):
        sessions = ChatSession.objects.filter(user=request.user)[:50]
        return JsonResponse(
            {
                "results": [
                    {
                        "id": str(s.id),
                        "title": s.title or "Untitled",
                        "model_id": s.model_id,
                        "updated_at": s.updated_at.isoformat(),
                    }
                    for s in sessions
                ],
            }
        )


class SessionDetailView(LoginRequiredMixin, View):
    """GET /erp/chat/sessions/<uuid>/ — messages for a single session."""

    def get(self, request, pk):
        session = ChatSession.objects.filter(user=request.user, pk=pk).first()
        if session is None:
            return JsonResponse({"error": "not_found"}, status=404)
        messages = ChatMessage.objects.filter(session=session)
        return JsonResponse(
            {
                "id": str(session.id),
                "title": session.title or "Untitled",
                # Resolve through the registry before handing it to the
                # browser: a session stored under a retired key would set
                # <select>.value to a non-existent option and blank the
                # dropdown. get_model() maps it forward to a live key.
                "model_id": get_model(session.model_id).key,
                # The stored key names a model that was removed from the
                # registry: the drawer shows it greyed out, and the next send
                # goes to model_id (the default) instead.
                "stored_model_id": session.model_id,
                "model_removed": not is_known(session.model_id),
                # The concrete model the gateway last reported for this
                # session ("" before its first reply, or off-gateway).
                "served_model": (session.metadata or {}).get("served_model", ""),
                "messages": [
                    {
                        "id": str(m.id),
                        "role": m.role,
                        "content": m.content,
                        "tool_calls": m.tool_calls,
                        "created_at": m.created_at.isoformat(),
                    }
                    for m in messages
                ],
            }
        )


class NewSessionView(LoginRequiredMixin, View):
    """POST /erp/chat/sessions/new/ — create an empty session."""

    def post(self, request):
        body = _parse_json_body(request)
        model_id = body.get("model_id") or default_model_key()
        session = ChatSession.objects.create(user=request.user, model_id=model_id)
        return JsonResponse(
            {"id": str(session.id), "title": "", "model_id": session.model_id}
        )


# ---- The streaming turn handler -------------------------------------------


class ChatStreamView(LoginRequiredMixin, View):
    """POST /erp/chat/stream/

    Body JSON:
      {
        "session_id": "<uuid>",
        "message": "user text",
        "context": { "path": "...", "page_type": "...", ... },
        "model_id": "claude-sonnet-5"
      }

    Response: text/event-stream with frames like
      data: {"type": "text_delta", "text": "..."}
      data: {"type": "model", "name": "provider-model"}   (gateway lanes only)
      data: {"type": "tool_use", "name": "...", "id": "...", "input": {...}}
      data: {"type": "tool_result", "tool_use_id": "...", "content": {...}}
      data: {"type": "turn_end", "stop_reason": "end_turn", ...}
      data: {"type": "error", "message": "..."}
    """

    def post(self, request):
        body = _parse_json_body(request)
        session_id = body.get("session_id")
        user_text = (body.get("message") or "").strip()
        context = body.get("context") or {}
        requested_model = body.get("model_id") or default_model_key()

        if not session_id or not user_text:
            return JsonResponse(
                {"error": "bad_request", "message": "session_id and message are required"},
                status=400,
            )

        session = ChatSession.objects.filter(user=request.user, pk=session_id).first()
        if session is None:
            return JsonResponse({"error": "not_found"}, status=404)

        model_config = get_model(requested_model)
        # Persist the user's chosen model on the session so it survives a
        # page reload even mid-conversation.
        if session.model_id != model_config.key:
            session.model_id = model_config.key
            session.save(update_fields=["model_id", "updated_at"])

        # Save the user message before streaming so even a crash mid-turn
        # leaves the history in a consistent state.
        ChatMessage.objects.create(
            session=session,
            role="user",
            content=user_text,
            context=context,
        )
        # Auto-title from the first user message.
        if not session.title:
            session.title = user_text[:60] + ("…" if len(user_text) > 60 else "")
            session.save(update_fields=["title", "updated_at"])

        # Under ASGI (uvicorn in prod, `runserver` with daphne/uvicorn in
        # dev) hand Django the async generator directly — the ASGI handler
        # iterates it on the event loop and one worker multiplexes many
        # streams. Under WSGI (plain dev runserver, WSGI test client) Django
        # would fully BUFFER an async iterator before sending, so we drive
        # the same generator with a thin sync adapter instead to keep the
        # frames incremental.
        # Build the adapter here, on the sync request path: gateway-lane
        # credentials resolve through llmhub's (ORM-backed) settings, which the
        # async stream below may not touch. It also snapshots the config per
        # request — a settings edit mid-stream never flaps an open SSE turn.
        adapter = build_adapter(model_config)
        if isinstance(request, ASGIRequest):
            streaming_content: Any = self._stream_events(session, context, model_config, adapter)
        else:
            streaming_content = self._stream_events_wsgi(session, context, model_config, adapter)

        response = StreamingHttpResponse(
            streaming_content,
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-cache"
        # Disable proxy buffering so chunks reach the browser immediately.
        response["X-Accel-Buffering"] = "no"
        return response

    # ---- SSE frame stream (async-native) -----------------------------------

    async def _stream_events(
        self,
        session: ChatSession,
        context: dict[str, Any],
        model_config,
        adapter=None,
    ) -> AsyncIterator[bytes]:
        """Async generator yielding encoded SSE frames for one chat turn.

        Composes directly with the async adapters (httpx async streams,
        including openai_compat's 503-warmup retry loop) — everything runs
        on the serving event loop with no bridging.

        Error / disconnect semantics:
        - Any exception escaping the tool-use loop is surfaced to the
          client as a final `error` frame instead of crashing the worker.
        - Client disconnects arrive as CancelledError (ASGI cancels the
          iterator task) or GeneratorExit (generator closed). Both are
          re-raised untouched; the `aclosing` block guarantees
          `_run_turn` — and therefore any open httpx stream inside the
          adapter — is unwound.
        """
        try:
            async with aclosing(self._run_turn(session, context, model_config, adapter)) as events:
                async for event in events:
                    yield f"data: {json.dumps(event)}\n\n".encode("utf-8")
        except (asyncio.CancelledError, GeneratorExit):
            # Client went away — nothing to send, just unwind cleanly.
            raise
        except Exception as exc:  # noqa: BLE001 — surface to client, don't kill the worker
            yield (
                "data: "
                + json.dumps({"type": "error", "message": f"Stream crashed: {exc}"})
                + "\n\n"
            ).encode("utf-8")

    # ---- WSGI fallback driver ----------------------------------------------

    def _stream_events_wsgi(
        self,
        session: ChatSession,
        context: dict[str, Any],
        model_config,
        adapter=None,
    ) -> Iterator[bytes]:
        """Sync driver for WSGI servers (plain dev runserver, tests).

        Runs `_stream_events` on a private event loop one frame at a time
        so SSE stays incremental even without ASGI. Error handling lives
        in `_stream_events`; this wrapper only bridges sync↔async and
        guarantees the generator is closed if the client disconnects.
        """
        agen = self._stream_events(session, context, model_config, adapter)
        with asyncio.Runner() as runner:
            try:
                while True:
                    try:
                        yield runner.run(agen.__anext__())
                    except StopAsyncIteration:
                        break
            finally:
                try:
                    runner.run(agen.aclose())
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    pass

    # ---- Async tool-use loop ---------------------------------------------

    async def _run_turn(
        self,
        session: ChatSession,
        context: dict[str, Any],
        model_config,
        adapter=None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Drive the model turn(s) until end_turn, yielding one event dict per step.

        This is where the tool-use loop lives. Every iteration of the outer
        `while True` is one round-trip to the model. A round-trip ends
        either with end_turn (we're done) or tool_use (we run the tools,
        append the results, and loop again).
        """
        from asgiref.sync import sync_to_async

        if adapter is None:
            adapter = build_adapter(model_config)
        # tools_mode="none" is the last rung of the per-provider fallback
        # ladder (after use_streaming=False) for models whose tool-call
        # output can't be parsed at all.
        tools = [] if model_config.tools_mode == "none" else anthropic_tool_schemas()
        system_prompt = _build_system_prompt(context)

        # Rebuild the message list from DB history every turn so we
        # naturally pick up the user message we just saved plus any
        # assistant/tool messages the previous loop iteration persisted.
        # Wrapped in sync_to_async because _load_messages_for_model uses
        # the sync ORM and Django 5+ forbids that from async context.
        messages = await sync_to_async(_load_messages_for_model)(session)

        max_iterations = 8  # safety bound against runaway tool-use loops
        iteration = 0

        while iteration < max_iterations:
            iteration += 1

            assistant_text_buffer: list[str] = []
            tool_calls_this_turn: list[dict[str, Any]] = []
            stop_reason: str | None = None
            input_tokens: int | None = None
            output_tokens: int | None = None
            stream_error: StreamError | None = None

            async for event in adapter.stream(
                model=model_config.model_id,
                system=system_prompt,
                messages=messages,
                tools=tools,
                # Per-model ceiling wins over the global default: on Claude 5
                # models max_tokens covers thinking as well as the visible
                # reply, so CHAT_MAX_TOKENS alone would truncate mid-answer.
                max_tokens=model_config.max_tokens or settings.CHAT_MAX_TOKENS,
                effort=model_config.effort,
                thinking=model_config.thinking,
            ):
                if isinstance(event, TextDelta):
                    assistant_text_buffer.append(event.text)
                    yield {"type": "text_delta", "text": event.text}

                elif isinstance(event, ToolUse):
                    tool_calls_this_turn.append(
                        {"id": event.id, "name": event.name, "input": event.input}
                    )
                    yield {
                        "type": "tool_use",
                        "id": event.id,
                        "name": event.name,
                        "input": event.input,
                    }

                elif isinstance(event, TurnEnd):
                    stop_reason = event.stop_reason
                    input_tokens = event.input_tokens
                    output_tokens = event.output_tokens

                elif isinstance(event, ModelResolved):
                    # Re-reported on every round-trip of the tool loop, so a
                    # gateway fallback mid-turn shows up as it happens.
                    await _record_served_model(session, event.name, model_config)
                    yield {"type": "model", "name": event.name}

                elif isinstance(event, StreamError):
                    stream_error = event
                    yield {
                        "type": "error",
                        "message": event.message,
                        "provider_error": event.provider_error,
                    }

            if stream_error is not None:
                # Persist whatever we got before aborting the turn.
                await _persist_assistant_message(
                    session,
                    assistant_text_buffer,
                    tool_calls_this_turn,
                )
                yield {"type": "turn_end", "stop_reason": "error"}
                return

            # Persist the assistant message (text + any tool_use blocks)
            # so a page reload mid-turn still sees what happened.
            await _persist_assistant_message(
                session,
                assistant_text_buffer,
                tool_calls_this_turn,
            )

            # Append the model's turn to the working messages list in
            # Anthropic content-block format so the next iteration has
            # the full history when we call the model again.
            messages.append(_assistant_content_blocks(assistant_text_buffer, tool_calls_this_turn))

            if stop_reason != "tool_use" or not tool_calls_this_turn:
                # Model is done. Update session accounting and stop.
                await _update_session_metadata(session, input_tokens, output_tokens)
                yield {
                    "type": "turn_end",
                    "stop_reason": stop_reason or "end_turn",
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }
                return

            # Execute each tool call and append the results as a single
            # user message containing tool_result content blocks.
            tool_result_blocks: list[dict[str, Any]] = []
            for call in tool_calls_this_turn:
                try:
                    result = await execute_tool_call(call["name"], call["input"])
                except UnknownTool as exc:
                    result = {"error": "UnknownTool", "message": str(exc)}

                yield {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "name": call["name"],
                    "content": result,
                }
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "content": json.dumps(result, default=str),
                    }
                )
                # Persist each tool result as its own row so the
                # transcript can replay them in order.
                await _persist_tool_result(session, call, result)

            messages.append({"role": "user", "content": tool_result_blocks})
            # Loop back — call the model again with the new tool results.

        # Safety bound hit. Yield an explanatory error frame and stop.
        yield {
            "type": "error",
            "message": f"Tool-use loop exceeded {max_iterations} iterations; aborting.",
        }
        yield {"type": "turn_end", "stop_reason": "iteration_limit"}


# ---- Helpers --------------------------------------------------------------


def _parse_json_body(request) -> dict[str, Any]:
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return {}


def _build_system_prompt(context: dict[str, Any]) -> str:
    """Assemble the system prompt with page context baked in.

    The context dict is scraped from the browser (pathname + any
    data-chat-context attribute) and is what lets the assistant
    answer "what's its stock?" without needing find_product.
    """
    base = (
        "You are a helpful assistant embedded in the Fincas admin at /erp/. "
        "You have access to tools for reading and modifying ERP data — products, "
        "variants, brands, categories, prices, stock, customers, orders, quotes, "
        "suppliers, purchase orders, and receipts.\n\n"
        "Always prefer tool calls over guessing. Never fabricate UUIDs or invoice "
        "numbers. When a find_* tool returns match_quality='fuzzy', confirm with "
        "the user before performing a write. Delete tools require confirm=True "
        "and you must get the user's explicit confirmation before calling them.\n"
    )
    if context:
        base += (
            "\nThe user is currently viewing: "
            + (context.get("path") or "<unknown>")
            + "\nPage context (JSON): "
            + json.dumps(context, default=str)
            + "\n\nUse this to resolve references like 'this product', 'it', or "
            "'that customer' without calling find_* tools when the context already "
            "contains the ID."
        )
    return base


def _load_messages_for_model(session: ChatSession) -> list[dict[str, Any]]:
    """Load the most recent N messages and convert to Anthropic's expected shape.

    We want the *latest* window rows, not the oldest. Earlier versions
    of this function sliced `.order_by("created_at")[:window]` which
    returns the OLDEST window messages in ascending order — so once a
    session had more than `window` messages, every new user message
    fell off the end and the model kept seeing the exact same
    stale history every turn, producing identical responses to every
    follow-up prompt. The fix is to order DESCENDING first, slice,
    then reverse the result back to chronological order.

    Tool-role messages from our DB map back into user-role content blocks
    with type=tool_result, which is how Anthropic wants tool results.
    """
    window = settings.CHAT_HISTORY_WINDOW
    rows = list(
        ChatMessage.objects.filter(session=session).order_by("-created_at")[:window]
    )
    rows.reverse()  # back to chronological order
    # Drop leading rows that would create an orphan tool_result or tool_use
    # at messages[0]. Anthropic rejects a conversation whose first block is
    # a tool_result with no matching tool_use in the previous message, which
    # is exactly what happens when the window boundary lands between an
    # assistant row (tool_use) and its tool row (tool_result). Trim until
    # the first surviving row is a plain user turn.
    while rows and rows[0].role != "user":
        rows.pop(0)
    messages: list[dict[str, Any]] = []
    for row in rows:
        if row.role == "user":
            messages.append({"role": "user", "content": row.content})
        elif row.role == "assistant":
            # Reconstruct assistant content blocks: text first, then tool_use
            blocks: list[dict[str, Any]] = []
            if row.content:
                blocks.append({"type": "text", "text": row.content})
            for call in row.tool_calls or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id", ""),
                        "name": call.get("name", ""),
                        "input": call.get("input", {}),
                    }
                )
            if blocks:
                messages.append({"role": "assistant", "content": blocks})
        elif row.role == "tool":
            # Tool results are appended as user-role messages in Anthropic's API.
            tool_blocks: list[dict[str, Any]] = []
            for call in row.tool_calls or []:
                tool_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.get("id", ""),
                        "content": json.dumps(call.get("result", {}), default=str),
                    }
                )
            if tool_blocks:
                messages.append({"role": "user", "content": tool_blocks})
    return messages


def _assistant_content_blocks(
    text_buffer: list[str],
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pack the streamed deltas + tool calls into an assistant message."""
    blocks: list[dict[str, Any]] = []
    joined_text = "".join(text_buffer).strip()
    if joined_text:
        blocks.append({"type": "text", "text": joined_text})
    for call in tool_calls:
        blocks.append(
            {
                "type": "tool_use",
                "id": call["id"],
                "name": call["name"],
                "input": call["input"],
            }
        )
    return {"role": "assistant", "content": blocks}


async def _persist_assistant_message(
    session: ChatSession,
    text_buffer: list[str],
    tool_calls: list[dict[str, Any]],
) -> None:
    """Save one assistant message to the DB.

    We run this through asgiref's sync_to_async bridge because
    ChatMessage.objects.create() is sync and we're in an async context.
    """
    from asgiref.sync import sync_to_async

    joined = "".join(text_buffer)
    if not joined and not tool_calls:
        return

    await sync_to_async(ChatMessage.objects.create)(
        session=session,
        role="assistant",
        content=joined,
        tool_calls=tool_calls,
    )


async def _persist_tool_result(
    session: ChatSession,
    call: dict[str, Any],
    result: Any,
) -> None:
    from asgiref.sync import sync_to_async

    await sync_to_async(ChatMessage.objects.create)(
        session=session,
        role="tool",
        content=_summarise_tool_result(result),
        tool_calls=[
            {
                "id": call["id"],
                "name": call["name"],
                "input": call["input"],
                "result": result,
            }
        ],
    )


def _summarise_tool_result(result: Any) -> str:
    """One-line summary of a tool result, for the transcript row's content field."""
    if isinstance(result, dict) and "error" in result:
        return f"ERROR: {result.get('message', 'unknown error')}"
    if isinstance(result, list):
        return f"{len(result)} row(s) returned"
    if isinstance(result, dict):
        id_hint = result.get("id") or result.get("deleted") or "ok"
        return f"result: {id_hint}"
    return str(result)[:200]


async def _update_session_metadata(
    session: ChatSession,
    input_tokens: int | None,
    output_tokens: int | None,
) -> None:
    from asgiref.sync import sync_to_async

    def _bump():
        meta = session.metadata or {}
        meta["total_input_tokens"] = (meta.get("total_input_tokens") or 0) + (input_tokens or 0)
        meta["total_output_tokens"] = (meta.get("total_output_tokens") or 0) + (output_tokens or 0)
        meta["turn_count"] = (meta.get("turn_count") or 0) + 1
        session.metadata = meta
        session.save(update_fields=["metadata", "updated_at"])

    await sync_to_async(_bump)()


async def _record_served_model(session: ChatSession, name: str, model_config) -> None:
    """Remember which concrete model answered: on the session and per lane.

    Bookkeeping only — a failure here must never fail the chat turn.
    """
    import logging

    from asgiref.sync import sync_to_async

    from llmhub.client import aobserve

    if model_config.credentials == "llmhub":
        await aobserve(model_config.model_id, name)
    meta = session.metadata or {}
    if meta.get("served_model") == name:
        return

    def _save():
        meta["served_model"] = name
        session.metadata = meta
        session.save(update_fields=["metadata", "updated_at"])

    try:
        await sync_to_async(_save)()
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("chat: could not record served model %s", name)
