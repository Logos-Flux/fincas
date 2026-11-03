"""Shared fixtures for the llmhub tests: isolated settings, gateway rows, mocked HTTP."""

from __future__ import annotations

import contextlib
import unittest
from typing import Callable, Iterator
from unittest import mock

import httpx
from django.core.cache import cache
from django.test import TestCase

from llmhub import conf, crypto
from llmhub.models import LLMSettings

# Every llmhub-relevant setting pinned, so a developer's shell env never leaks in.
ISOLATED = dict(
    LLMHUB_MODE="direct",
    LLMHUB_GATEWAY_BASE_URL="",
    LLMHUB_GATEWAY_API_KEY="",
    LLMHUB_ALLOWED_BASE_URLS=[],
    LLMHUB_FERNET_KEY="",
    LLMHUB_EMBED_PROBE_API_KEY="",
    LLMHUB_EMBED_PROBE_ROUTE="",
    LLMHUB_EMBED_PROBE_DIMENSIONS=None,
    LLMHUB_EMBED_PROBE_BASE_URL="",
    LLMHUB_CACHE_TTL_S=0,
    # Pin the registry to the generic one purpose: a deployment's own purposes
    # (and their default lanes) must not leak into these tests' expectations.
    LLMHUB_PURPOSES={"chat_default": {"label": "ERP chat (default)", "kind": "chat", "tools": True}},
    LLMHUB_EXTERNAL_PURPOSES=[],
    OPENAI_API_KEY="",
    OPENAI_BASE_URL="",
    ANTHROPIC_API_KEY="",
)

GATEWAY = "https://gw.test"
DB_KEY = "sk-db-caller-token-0000-DBKY"
ENV_KEY = "sk-env-caller-token-000-ENVK"

_RealAsyncClient = httpx.AsyncClient
_RealClient = httpx.Client


def reset_state() -> None:
    cache.clear()
    conf.invalidate()


def reset_registry() -> None:
    """Put the chat registry back on the code models.

    ``MODELS`` is mutated in place by ``refresh()``; a gateway-mode test would
    otherwise leave gateway rows in it for the next test that reads ``MODELS``
    directly (TestCase rolls the rows back, but nothing re-runs refresh()).
    """
    from chat.llm import registry

    if not hasattr(registry, "_built_from"):
        return  # a chat app that is not llmhub-aware yet has nothing to reset
    registry._built_from[0] = None
    registry._replace(registry.build_code_models())


class HubTestCase(TestCase):
    """TestCase with llmhub's process caches and the chat registry reset around each test."""

    def setUp(self):
        super().setUp()
        reset_state()

    def tearDown(self):
        reset_state()
        reset_registry()
        super().tearDown()


def save_gateway(*, key: str | None = DB_KEY, base: str = GATEWAY, lane_map=None, **fields) -> LLMSettings:
    """Save the singleton in gateway mode (a shell-style write)."""
    row = LLMSettings(
        pk=1,
        mode="gateway",
        gateway_base_url=base,
        gateway_api_key_enc=crypto.encrypt(key) if key else "",
        lane_map={"chat_default": "erp-lane"} if lane_map is None else lane_map,
        **fields,
    )
    row.save()
    conf.invalidate()
    return row


def json_response(status: int = 200, body=None, headers=None) -> httpx.Response:
    return httpx.Response(status, json=body if body is not None else {}, headers=headers or {})


def completion_body(text: str = "pong", tool_calls=None, model: str = "erp-lane") -> dict:
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"model": model, "choices": [{"index": 0, "message": message}], "usage": {"total_tokens": 3}}


def models_body(*ids: str) -> dict:
    return {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}


@contextlib.contextmanager
def mock_http(handler: Callable[[httpx.Request], httpx.Response]) -> Iterator[list[httpx.Request]]:
    """Route every httpx client llmhub.client builds through ``handler``. Yields the request log."""
    calls: list[httpx.Request] = []

    def logged(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    transport = httpx.MockTransport(logged)
    with (
        mock.patch("llmhub.client.httpx.AsyncClient", lambda **kw: _RealAsyncClient(transport=transport, **kw)),
        mock.patch("llmhub.client.httpx.Client", lambda **kw: _RealClient(transport=transport, **kw)),
    ):
        yield calls


def _chat_is_llmhub_aware() -> bool:
    from chat.llm import registry

    return hasattr(registry, "is_known")


# Tests of the chat ↔ llmhub integration (gateway-mode registry, the rewritten
# check_llm_models). A downstream copy of llmhub can land before its chat app is
# ported (a downstream may ship llmhub dark first); there these skip instead of fail,
# so both repos can carry the identical test files.
requires_llmhub_chat = unittest.skipUnless(
    _chat_is_llmhub_aware(), "chat app is not llmhub-aware yet (gateway registry not ported)"
)
