"""Direct Anthropic Messages API adapter.

POSTs to https://api.anthropic.com/v1/messages with the project's
ANTHROPIC_API_KEY. The streaming SSE shape is exactly what we want
internally, so the parser stays simple.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from .base import Event, LLMAdapter, StreamError, TextDelta, ToolUse, TurnEnd

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class AnthropicConfig:
    api_key: str
    base_url: str = ANTHROPIC_BASE_URL
    timeout: float = 60.0


class AnthropicAdapter(LLMAdapter):
    """Adapter that speaks Anthropic Messages API directly."""

    def __init__(self, config: AnthropicConfig) -> None:
        self._config = config

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
                    "Anthropic API key is not configured. Set ANTHROPIC_API_KEY "
                    "to enable this model."
                ),
            )
            return
        url = f"{self._config.base_url.rstrip('/')}/v1/messages"
        headers = {
            "x-api-key": self._config.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "stream": True,
            "system": system,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
        if effort:
            payload["output_config"] = {"effort": effort}
        if thinking:
            payload["thinking"] = {"type": thinking}

        try:
            async with httpx.AsyncClient(timeout=self._config.timeout) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        try:
                            provider_error = json.loads(body)
                        except Exception:
                            provider_error = {"raw": body.decode("utf-8", errors="replace")[:500]}
                        yield StreamError(
                            message=(
                                f"Anthropic returned {response.status_code}: "
                                f"{provider_error.get('error', provider_error)}"
                            ),
                            provider_error=provider_error,
                        )
                        return

                    async for event in self._parse_anthropic_sse(response):
                        yield event
        except httpx.HTTPError as exc:
            yield StreamError(message=f"Transport error talking to Anthropic: {exc}")

    async def _parse_anthropic_sse(
        self, response: httpx.Response
    ) -> AsyncIterator[Event]:
        """Translate Anthropic's streaming event shape into our Event types.

        Anthropic emits a sequence of SSE frames:
          message_start, content_block_start, content_block_delta,
          content_block_stop, message_delta, message_stop.

        For text blocks, content_block_delta carries `delta.text`.
        For tool_use blocks, content_block_start carries the tool name
        and id, and the subsequent content_block_delta frames carry
        `delta.partial_json` — the tool-call arguments being streamed as
        JSON fragments that we accumulate until content_block_stop.
        """
        tool_blocks: dict[int, dict[str, Any]] = {}
        current_index: int | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        stop_reason: str | None = None

        async for raw in response.aiter_lines():
            if not raw or raw.startswith(":"):
                continue
            if raw.startswith("event: "):
                continue
            if not raw.startswith("data: "):
                continue
            payload_str = raw[len("data: "):]
            try:
                evt = json.loads(payload_str)
            except json.JSONDecodeError:
                continue

            evt_type = evt.get("type")

            if evt_type == "message_start":
                usage = (evt.get("message") or {}).get("usage") or {}
                input_tokens = usage.get("input_tokens")

            elif evt_type == "content_block_start":
                current_index = evt.get("index")
                block = evt.get("content_block") or {}
                if block.get("type") == "tool_use":
                    tool_blocks[current_index] = {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "json_buf": "",
                    }

            elif evt_type == "content_block_delta":
                idx = evt.get("index")
                delta = evt.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        yield TextDelta(text=text)
                elif delta.get("type") == "input_json_delta":
                    tool = tool_blocks.get(idx)
                    if tool is not None:
                        tool["json_buf"] += delta.get("partial_json", "")

            elif evt_type == "content_block_stop":
                idx = evt.get("index")
                tool = tool_blocks.pop(idx, None)
                if tool is not None:
                    try:
                        args = json.loads(tool["json_buf"]) if tool["json_buf"] else {}
                    except json.JSONDecodeError:
                        yield StreamError(
                            message=(
                                f"Model emitted malformed tool-call JSON for {tool['name']}: "
                                f"{tool['json_buf'][:200]}"
                            )
                        )
                        continue
                    yield ToolUse(id=tool["id"], name=tool["name"], input=args)

            elif evt_type == "message_delta":
                delta = evt.get("delta") or {}
                if "stop_reason" in delta:
                    stop_reason = delta["stop_reason"]
                usage = evt.get("usage") or {}
                if "output_tokens" in usage:
                    output_tokens = usage["output_tokens"]

            elif evt_type == "message_stop":
                yield TurnEnd(
                    stop_reason=stop_reason,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                return

        yield TurnEnd(
            stop_reason=stop_reason or "incomplete",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
