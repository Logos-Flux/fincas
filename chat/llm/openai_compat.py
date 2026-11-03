"""OpenAI-compatible adapter for any /v1/chat/completions provider.

Talks directly to an OpenAI-shape endpoint (OpenAI itself, Groq,
DeepSeek, Together, a self-hosted vLLM, etc.) using the standard
`Authorization: Bearer <api_key>` header.

The view keeps its message list in Anthropic shape as the canonical
internal format. This adapter translates on the way out and back on the
way in. Reasons:

1. The Anthropic adapter already works and is well-tested — we want it
   as the canonical shape so only one side of the pipe changes per
   provider.
2. Anthropic's tool_use + tool_result content-block shape maps cleanly
   to OpenAI's assistant.tool_calls + role="tool" messages; the reverse
   is lossier for OpenAI → Anthropic because Anthropic requires tool_use
   blocks to live inside a single assistant message.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from .base import Event, LLMAdapter, ModelResolved, StreamError, TextDelta, ToolUse, TurnEnd

# LiteLLM names the deployment that actually served the request here. The
# body's `model` field only echoes the lane that was asked for, so this
# header is the one place the concrete model is visible.
_RESOLVED_MODEL_HEADER = "x-litellm-model-name"

# Backoff schedule for 503 cold-start retries: first retry after
# _WARMUP_INITIAL_DELAY seconds, doubling up to _WARMUP_MAX_DELAY.
_WARMUP_INITIAL_DELAY = 3.0
_WARMUP_MAX_DELAY = 15.0


@dataclass(frozen=True)
class OpenAICompatConfig:
    """Endpoint config for any OpenAI-shape Chat Completions provider.

    `base_url` is everything up to but not including `/v1/chat/completions`.
    `provider_label` is just used in error messages so users can tell
    which upstream failed.

    `warmup_max_wait_s` > 0 marks the upstream as scale-to-zero: HTTP 503
    means "cold start in progress", so the adapter silently retries with
    backoff for up to that many seconds before surfacing a friendly
    "still waking up" error. 0 (the default) keeps the old behavior of
    treating 503 like any other upstream error.
    """

    base_url: str
    api_key: str
    provider_label: str = "openai"
    timeout: float = 60.0
    warmup_max_wait_s: float = 0.0


class OpenAICompatAdapter(LLMAdapter):
    """Adapter for any provider that speaks the OpenAI Chat Completions API."""

    def __init__(
        self,
        config: OpenAICompatConfig,
        use_streaming: bool = True,
    ) -> None:
        """Adapter constructor.

        `use_streaming` controls whether we open an SSE stream or POST
        once and parse the full response. Set to False for providers
        whose OpenAI-compat shim doesn't correctly emit `tool_calls`
        in streaming mode — Workers AI Llama 3.3 is the motivating
        example: its non-streaming response has properly structured
        `tool_calls`, but the streaming path just dumps the model's
        raw text output as content.

        When use_streaming=False, text is still yielded via TextDelta
        events (as a single chunk) so the view doesn't care which
        transport was used.
        """
        self._config = config
        self._use_streaming = use_streaming

    # ---- Public interface ------------------------------------------------

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 4096,
        effort: str | None = None,
        thinking: str | None = None,
    ) -> AsyncIterator[Event]:
        if not (self._config.api_key or "").strip():
            yield StreamError(
                message=(
                    f"{self._config.provider_label} API key is not configured. "
                    "Set the configured API key setting to enable this model."
                ),
            )
            return
        # effort/thinking are Anthropic reasoning controls with no
        # OpenAI-shape equivalent here — accepted and ignored.
        url = f"{self._config.base_url.rstrip('/')}/v1/chat/completions"
        headers = {
            "authorization": f"Bearer {self._config.api_key}",
            "content-type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": model,
            "messages": _anthropic_to_openai_messages(system, messages),
            "stream": self._use_streaming,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = _anthropic_to_openai_tools(tools)
            payload["tool_choice"] = "auto"

        if self._use_streaming:
            headers["accept"] = "text/event-stream"
            async for event in self._do_streaming_request(url, headers, payload):
                yield event
        else:
            headers["accept"] = "application/json"
            async for event in self._do_blocking_request(url, headers, payload):
                yield event

    # ---- Cold-start (503) retry helpers ------------------------------------

    def _warmup_deadline(self) -> float | None:
        """Absolute time.monotonic() cutoff for 503 retries, or None if disabled."""
        if self._config.warmup_max_wait_s > 0:
            return time.monotonic() + self._config.warmup_max_wait_s
        return None

    def _warmup_exhausted_error(
        self, provider_error: dict[str, Any] | None
    ) -> StreamError:
        return StreamError(
            message=(
                f"{self._config.provider_label} is waking up from a cold start — "
                "this can take a few minutes. Please try again shortly."
            ),
            provider_error=provider_error,
        )

    # ---- Streaming path --------------------------------------------------

    async def _do_streaming_request(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> AsyncIterator[Event]:
        deadline = self._warmup_deadline()
        delay = _WARMUP_INITIAL_DELAY
        try:
            async with httpx.AsyncClient(timeout=self._config.timeout) as client:
                while True:
                    yielded_any = False
                    try:
                        async with client.stream("POST", url, headers=headers, json=payload) as response:
                            if response.status_code >= 400:
                                body = await response.aread()
                                try:
                                    provider_error = json.loads(body)
                                except Exception:
                                    provider_error = {"raw": body.decode("utf-8", errors="replace")[:500]}
                                if response.status_code == 503 and deadline is not None:
                                    # Scale-to-zero upstream still warming. If
                                    # another backoff step fits inside the budget,
                                    # fall through to the sleep-and-retry below;
                                    # otherwise give up with a friendly error.
                                    if time.monotonic() + delay > deadline:
                                        yield self._warmup_exhausted_error(provider_error)
                                        return
                                else:
                                    yield StreamError(
                                        message=(
                                            f"{self._config.provider_label} returned {response.status_code}: {provider_error}"
                                        ),
                                        provider_error=provider_error,
                                    )
                                    return
                            else:
                                resolved = _resolved_model(response)
                                if resolved:
                                    yield resolved
                                async for event in self._parse_openai_sse(response):
                                    yielded_any = True
                                    yield event
                                return
                    except httpx.HTTPError as exc:
                        # Cold-starting scale-to-zero upstreams don't always
                        # answer a clean 503: under concurrent requests the
                        # connection can be dropped before any response is
                        # sent (observed live via Caddy→Modal). Treat that
                        # like a 503 and retry under the same warmup budget —
                        # but NEVER after events were yielded (a retry would
                        # duplicate output; mid-stream failures propagate).
                        if (
                            yielded_any
                            or deadline is None
                            or time.monotonic() + delay > deadline
                        ):
                            yield StreamError(
                                message=f"Transport error talking to {self._config.provider_label}: {exc}"
                            )
                            return

                    # Only reachable on a retryable 503 or cold-start
                    # disconnect — wait out part of the cold start, then
                    # re-POST the identical payload.
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, _WARMUP_MAX_DELAY)
        except httpx.HTTPError as exc:
            yield StreamError(message=f"Transport error talking to {self._config.provider_label}: {exc}")

    # ---- Blocking (non-streaming) path -----------------------------------

    async def _do_blocking_request(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> AsyncIterator[Event]:
        """POST once, parse the complete response, synthesize stream events.

        Used for providers whose streaming tool-call implementation is
        broken. The user still sees a chat-like UX because the view
        renders TextDelta/ToolUse events as they arrive — there's just
        a single large TextDelta instead of incremental ones.
        """
        deadline = self._warmup_deadline()
        delay = _WARMUP_INITIAL_DELAY
        while True:
            try:
                async with httpx.AsyncClient(timeout=self._config.timeout) as client:
                    response = await client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                # Cold-start connection drops retry like 503s (see the
                # streaming path). Nothing has been yielded yet on this
                # path, so a retry can never duplicate output.
                if deadline is not None and time.monotonic() + delay <= deadline:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, _WARMUP_MAX_DELAY)
                    continue
                yield StreamError(message=f"Transport error talking to {self._config.provider_label}: {exc}")
                return

            if response.status_code == 503 and deadline is not None:
                # Scale-to-zero upstream still warming — retry with backoff
                # until the budget runs out, then give a friendly error.
                if time.monotonic() + delay > deadline:
                    try:
                        provider_error = response.json()
                    except Exception:
                        provider_error = {"raw": response.text[:500]}
                    yield self._warmup_exhausted_error(provider_error)
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, _WARMUP_MAX_DELAY)
                continue
            break

        if response.status_code >= 400:
            try:
                provider_error = response.json()
            except Exception:
                provider_error = {"raw": response.text[:500]}
            yield StreamError(
                message=(
                    f"{self._config.provider_label} returned {response.status_code}: {provider_error}"
                ),
                provider_error=provider_error,
            )
            return

        try:
            body = response.json()
        except Exception as exc:
            yield StreamError(message=f"Failed to parse {self._config.provider_label} response: {exc}")
            return

        resolved = _resolved_model(response)
        if resolved:
            yield resolved

        choices = body.get("choices") or []
        if not choices:
            yield TurnEnd(stop_reason="incomplete")
            return

        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content")
        structured_tool_calls = message.get("tool_calls") or []

        # Fallback path for providers whose OpenAI-compat shim doesn't
        # recognize the model's native tool-call format and dumps it
        # into `content` as text — typically a `<tools>{...}</tools>`
        # tag, a fenced ```json``` block, or a bare `{"name", "arguments"}`
        # JSON object. If we find one, we parse it into a synthetic
        # ToolUse event AND suppress the text so the user doesn't see
        # the raw tool-call markup.
        text_tool_calls: list[dict[str, Any]] = []
        remaining_text = content or ""
        if content and not structured_tool_calls:
            text_tool_calls, remaining_text = _extract_text_tool_calls(content)

        if remaining_text:
            yield TextDelta(text=remaining_text)

        # Structured tool calls come first (the normal path).
        for call in structured_tool_calls:
            fn = call.get("function") or {}
            try:
                parsed_args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                yield StreamError(
                    message=(
                        f"{self._config.provider_label} emitted malformed tool-call JSON "
                        f"for {fn.get('name')}: {(fn.get('arguments') or '')[:200]}"
                    )
                )
                continue
            yield ToolUse(
                id=call.get("id") or "",
                name=fn.get("name") or "",
                input=_coerce_arg_types(parsed_args),
            )

        # Synthetic tool calls from text parsing. We override the finish
        # reason below since the upstream reported "stop" but we rescued
        # a real tool_use.
        synthesized_tool_use = False
        for i, call in enumerate(text_tool_calls):
            synthesized_tool_use = True
            yield ToolUse(
                id=call.get("id") or f"text_call_{i}",
                name=call.get("name") or "",
                input=_coerce_arg_types(call.get("arguments") or {}),
            )

        usage = body.get("usage") or {}
        raw_stop = choice.get("finish_reason")
        stop_reason = _normalise_finish_reason(raw_stop)
        if synthesized_tool_use and stop_reason != "tool_use":
            stop_reason = "tool_use"

        yield TurnEnd(
            stop_reason=stop_reason,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )

    # ---- OpenAI SSE parser -----------------------------------------------

    async def _parse_openai_sse(
        self, response: httpx.Response
    ) -> AsyncIterator[Event]:
        """Translate OpenAI-format streaming into our Event types.

        OpenAI emits a sequence of `data: {...}` frames where each frame
        has `choices[0].delta` carrying either text (`delta.content`) or
        a `delta.tool_calls` list of partial tool-call fragments. The
        final frame carries `choices[0].finish_reason` and may also
        carry a `usage` block (if the caller sent stream_options).

        Tool-call streaming works by index: each fragment has a `.index`
        attribute, and successive fragments with the same index accumulate
        their `function.arguments` as a partial JSON string. We buffer
        per-index and emit one ToolUse event when the stream ends.
        """
        # Per-index accumulators for streaming tool calls
        tool_buffers: dict[int, dict[str, Any]] = {}

        finish_reason: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None

        async for raw in response.aiter_lines():
            if not raw or raw.startswith(":"):
                continue
            if not raw.startswith("data: "):
                continue
            data = raw[len("data: "):]
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            # Most providers put usage in a separate final chunk
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                input_tokens = usage.get("prompt_tokens", input_tokens)
                output_tokens = usage.get("completion_tokens", output_tokens)

            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}

            # Text delta
            text = delta.get("content")
            if text:
                yield TextDelta(text=text)

            # Tool-call fragments
            tool_calls = delta.get("tool_calls") or []
            for frag in tool_calls:
                idx = frag.get("index", 0)
                buf = tool_buffers.setdefault(
                    idx,
                    {"id": "", "name": "", "arguments": ""},
                )
                if frag.get("id"):
                    buf["id"] = frag["id"]
                fn = frag.get("function") or {}
                if fn.get("name"):
                    buf["name"] = fn["name"]
                if fn.get("arguments"):
                    buf["arguments"] += fn["arguments"]

            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

        # Flush any accumulated tool calls in index order
        for idx in sorted(tool_buffers.keys()):
            buf = tool_buffers[idx]
            try:
                parsed_args = json.loads(buf["arguments"]) if buf["arguments"] else {}
            except json.JSONDecodeError:
                yield StreamError(
                    message=(
                        f"{self._config.provider_label} emitted malformed tool-call JSON "
                        f"for {buf['name']}: {buf['arguments'][:200]}"
                    )
                )
                continue
            yield ToolUse(id=buf["id"] or f"call_{idx}", name=buf["name"], input=parsed_args)

        # Normalise OpenAI's finish_reason to our scheme
        #   "stop"       → "end_turn"
        #   "tool_calls" → "tool_use"   (matches Anthropic's stop_reason)
        #   "length"     → "max_tokens"
        stop_reason = _normalise_finish_reason(finish_reason)
        yield TurnEnd(
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


# ---- Translation helpers -----------------------------------------------


def _resolved_model(response: httpx.Response) -> ModelResolved | None:
    """The concrete model the upstream reports serving, when it reports one."""
    name = (response.headers.get(_RESOLVED_MODEL_HEADER) or "").strip()
    return ModelResolved(name=name) if name else None


def _normalise_finish_reason(openai_reason: str | None) -> str:
    if openai_reason == "stop":
        return "end_turn"
    if openai_reason == "tool_calls":
        return "tool_use"
    if openai_reason == "length":
        return "max_tokens"
    return openai_reason or "incomplete"


def _anthropic_to_openai_tools(
    anthropic_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """{name, description, input_schema} → {type=function, function={name, description, parameters}}."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description") or "",
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for tool in anthropic_tools
    ]


def _anthropic_to_openai_messages(
    system: str,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Translate the view's canonical Anthropic-shape messages into OpenAI shape.

    Anthropic content blocks we need to handle:
    - user string → {"role": "user", "content": "<string>"}
    - user list of tool_result blocks → one {"role": "tool", ...} per block
    - assistant list of {text, tool_use} blocks → {"role": "assistant",
      "content": "<joined text>", "tool_calls": [...]}
    """
    result: list[dict[str, Any]] = []

    if system:
        result.append({"role": "system", "content": system})

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "user":
            if isinstance(content, str):
                result.append({"role": "user", "content": content})
            elif isinstance(content, list):
                # List of tool_result blocks (how we represent tool echoes)
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        result.append(
                            {
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id", ""),
                                "content": _ensure_str(block.get("content")),
                            }
                        )
                    elif isinstance(block, dict) and block.get("type") == "text":
                        result.append({"role": "user", "content": block.get("text", "")})
                    # Anything else: skip silently — we only produce
                    # tool_result / text in user blocks.

        elif role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []

            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append(
                            {
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": json.dumps(block.get("input") or {}),
                                },
                            }
                        )

            # Workers AI rejects `content: null` on assistant messages with
            # tool_calls — it wants an empty string. Standard OpenAI accepts
            # both, so `""` is the portable choice.
            msg_obj: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(text_parts),
            }
            if tool_calls:
                msg_obj["tool_calls"] = tool_calls
            result.append(msg_obj)

        elif role == "system":
            # Defensive: if an older session has system messages in the
            # history list, fold them in as plain system messages.
            result.append({"role": "system", "content": _ensure_str(content)})

    return result


def _ensure_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, default=str)


_TOOLS_TAG_RE = re.compile(r"<tools>\s*(\{.*?\})\s*</tools>", re.DOTALL)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_NAME_ARGS_RE = re.compile(
    r'(\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{.*?\}\s*\})',
    re.DOTALL,
)


def _extract_text_tool_calls(content: str) -> tuple[list[dict[str, Any]], str]:
    """Scan a plain-text model response for embedded tool calls.

    Workers AI's OpenAI-compat shim doesn't recognize the native
    tool-calling output of several models — Qwen 2.5 Coder wraps calls
    in `<tools>...</tools>`, Llama 3.3 leaks `<|python_tag|>` markers,
    and nearly every model sometimes drops a `{"name": ..., "arguments":
    {...}}` JSON object inside a fenced code block. All of those hit
    us as plain `content` with `finish_reason: "stop"` and an empty
    `tool_calls` array, so the Django chat view never sees a real tool
    use and the model's answer just says "here's what I'd do".

    This helper rescues the three most common shapes:

        <tools>{"name": "list_orders", "arguments": {...}}</tools>

        ```json
        {"name": "list_orders", "arguments": {...}}
        ```

        {"name": "list_orders", "arguments": {...}}

    Returns `(calls, remaining_text)` where `calls` is a list of dicts
    with `name` / `arguments` / `id`, and `remaining_text` is the
    original content with all matched tool-call markup stripped out
    (empty string if the whole content was a tool call).

    Kept deliberately conservative: we only match JSON objects that have
    both a "name" string and an "arguments" object. An assistant response
    that happens to mention JSON-ish content in prose won't be misparsed
    because the bare regex requires both keys.
    """
    if not content:
        return [], content

    calls: list[dict[str, Any]] = []
    remaining = content

    # 1. <tools>...</tools>
    for match in _TOOLS_TAG_RE.finditer(content):
        parsed = _try_parse_tool_call(match.group(1))
        if parsed is not None:
            calls.append(parsed)
            remaining = remaining.replace(match.group(0), "")

    # 2. Fenced code blocks
    if not calls:  # don't double-match
        for match in _JSON_FENCE_RE.finditer(content):
            parsed = _try_parse_tool_call(match.group(1))
            if parsed is not None:
                calls.append(parsed)
                remaining = remaining.replace(match.group(0), "")

    # 3. Bare JSON objects matching the name+arguments shape
    if not calls:
        for match in _BARE_NAME_ARGS_RE.finditer(content):
            parsed = _try_parse_tool_call(match.group(1))
            if parsed is not None:
                calls.append(parsed)
                remaining = remaining.replace(match.group(0), "")

    return calls, remaining.strip()


def _try_parse_tool_call(raw: str) -> dict[str, Any] | None:
    """Parse a candidate JSON string as a tool call. Returns None on bad shape.

    We accept the shape `{"name": "...", "arguments": {...}}` — the format
    most open models fall back to when they don't have structured tool
    output wired through. OpenAI's `{"type": "function", "function":
    {...}}` shape isn't supported here because Workers AI models don't
    emit it; models that do emit that shape land in the structured
    `tool_calls` field and never reach this fallback path.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    arguments = data.get("arguments")
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(arguments, dict):
        # Some models serialise arguments as a JSON string. Recover it.
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        else:
            arguments = {}
    return {"name": name, "arguments": arguments}


def _unwrap_typed_value(value: Any) -> Any:
    """Unwrap `{"type": "...", "value": ...}` if the model emitted that shape.

    Some models — notably DeepSeek R1 Distill Qwen 32B via Workers AI —
    emit every tool argument as a typed-value envelope:

        {"search": {"type": "string", "value": null},
         "limit":  {"type": "integer", "value": 1}}

    instead of the bare values the schema actually wants. The downstream
    tool functions don't know what to do with a dict where they expect a
    primitive, so we unwrap before dispatching.

    We're conservative: only unwrap dicts that have *exactly* the two
    keys "type" and "value", with "type" being a string. Anything else
    might be a legitimate JSON object the user actually wants to pass
    through (e.g. for the `fields` argument on update_*).
    """
    if (
        isinstance(value, dict)
        and set(value.keys()) == {"type", "value"}
        and isinstance(value.get("type"), str)
    ):
        return value["value"]
    return value


def _coerce_arg_types(args: dict[str, Any]) -> dict[str, Any]:
    """Best-effort coercion of model-emitted args back to native Python types.

    Two quirks we work around:

    1. **Stringified scalars** — Llama 3.3 (and friends) sometimes emit
       "true"/"false"/"null" as JSON strings instead of actual booleans
       and nulls, or "100" instead of the integer 100, even when the
       tool schema asks for the typed value.

    2. **Typed-value wrappers** — DeepSeek R1 Distill Qwen 32B wraps
       every argument as `{"type": "string", "value": ...}` instead of
       the bare value. See `_unwrap_typed_value` for details.

    Both are recovered transparently so the downstream tool functions
    don't have to care which provider the model came from.
    """
    coerced: dict[str, Any] = {}
    for key, value in args.items():
        # Unwrap the typed-value envelope before any further coercion.
        value = _unwrap_typed_value(value)

        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "false"):
                coerced[key] = lowered == "true"
                continue
            if lowered in ("null", "none", ""):
                coerced[key] = None
                continue
            # Try numeric coercion — integers only. We accept exact
            # matches so we don't trip over things like a SKU that
            # happens to be all digits.
            if lowered.lstrip("-").isdigit():
                try:
                    coerced[key] = int(lowered)
                    continue
                except ValueError:
                    pass
        coerced[key] = value
    return coerced
