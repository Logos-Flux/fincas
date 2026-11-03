"""llmhub.client over a mocked httpx transport — no network."""

from __future__ import annotations

import asyncio
import json
from unittest import mock

import httpx
from asgiref.sync import async_to_sync
from django.test import override_settings

from chat.llm.base import ModelResolved, StreamError, TextDelta, ToolUse, TurnEnd
from llmhub import client, conf
from llmhub.models import LaneObservation, LLMModel, LLMSettings

from .helpers import (
    DB_KEY,
    GATEWAY,
    ISOLATED,
    HubTestCase,
    completion_body,
    json_response,
    mock_http,
    models_body,
    requires_llmhub_chat,  # noqa: I001
    reset_state,
    save_gateway,
)


def _complete(**kwargs):
    return async_to_sync(client.complete_async)("chat_default", [{"role": "user", "content": "hi"}], **kwargs)


@override_settings(**ISOLATED)
class CompleteTests(HubTestCase):
    def setUp(self):
        reset_state()
        save_gateway()

    def test_happy_path(self):
        def handler(request):
            assert request.url == httpx.URL(f"{GATEWAY}/v1/chat/completions")
            assert request.headers["authorization"] == f"Bearer {DB_KEY}"
            body = json.loads(request.content)
            assert body["model"] == "erp-lane" and body["stream"] is False
            return json_response(200, completion_body("pong"), {"x-litellm-model-name": "vendor/model-7"})

        with mock_http(handler) as calls:
            done = _complete()
        assert len(calls) == 1
        assert done.text == "pong"
        assert done.route == "erp-lane"
        assert done.resolved_model == "vendor/model-7"
        assert done.key_source == "db"
        obs = LaneObservation.objects.get(route="erp-lane")
        assert obs.model == "vendor/model-7"

    def test_system_prompt_and_extra(self):
        def handler(request):
            body = json.loads(request.content)
            assert body["messages"][0] == {"role": "system", "content": "be terse"}
            assert body["tools"] == [{"googleSearch": {}}]
            assert body["max_tokens"] == 50
            return json_response(200, completion_body("ok"))

        with mock_http(handler):
            done = _complete(system="be terse", extra={"tools": [{"googleSearch": {}}]}, max_tokens=50)
        # No header → falls back to the body's model and records no observation.
        assert done.resolved_model == "erp-lane"
        assert not LaneObservation.objects.exists()

    def test_no_observation_in_direct_mode(self):
        LLMSettings.objects.filter(pk=1).update(mode="direct", lane_map={"chat_default": "gpt-x"})
        with override_settings(OPENAI_API_KEY="sk-direct", OPENAI_BASE_URL="https://prov.test"):
            with mock_http(lambda r: json_response(200, completion_body(), {"x-litellm-model-name": "m"})) as calls:
                done = _complete()
        assert calls[0].headers["authorization"] == "Bearer sk-direct"
        assert done.key_source == "env"
        assert not LaneObservation.objects.exists()

    def _error(self, status: int) -> client.LLMHubError:
        with mock_http(lambda r: json_response(status, {"error": {"message": "nope"}})):
            with self.assertRaises(client.LLMHubError) as ctx:
                _complete()
        return ctx.exception

    def test_status_taxonomy(self):
        for status, category in [
            (400, "bad_lane"),
            (401, "auth"),
            (403, "edge_forbidden"),
            (429, "rate_limited"),
            (503, "cold_lane"),
            (500, "upstream"),
        ]:
            with self.subTest(status=status):
                exc = self._error(status)
                assert exc.category == category
                assert exc.status == status
                assert "key_source=db" in str(exc)
                assert "route=erp-lane" in str(exc)
                assert DB_KEY not in str(exc)

    def test_503_retries_within_warmup_budget(self):
        responses = [
            json_response(503, {"error": "warming"}),
            json_response(503, {}),
            json_response(200, completion_body("warm")),
        ]
        with mock.patch.object(client, "_WARMUP_INITIAL_DELAY", 0.01), mock_http(lambda r: responses.pop(0)) as calls:
            done = _complete(warmup_max_wait_s=5)
        assert done.text == "warm"
        assert len(calls) == 3

    def test_503_without_budget_is_cold_lane(self):
        with mock_http(lambda r: json_response(503, {})) as calls:
            with self.assertRaises(client.LLMHubError) as ctx:
                _complete()
        assert ctx.exception.category == "cold_lane"
        assert len(calls) == 1

    def test_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        with mock_http(handler):
            with self.assertRaises(client.LLMHubError) as ctx:
                _complete(timeout=2)
        assert ctx.exception.category == "timeout"
        assert "key_source=db" in str(ctx.exception)

    def test_connect_error(self):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        with mock_http(handler):
            with self.assertRaises(client.LLMHubError) as ctx:
                _complete()
        assert ctx.exception.category == "unreachable"
        assert "key_source=db" in str(ctx.exception)

    def test_malformed_200(self):
        for body in ({"choices": []}, {"nope": 1}):
            with self.subTest(body=body):
                with mock_http(lambda r, b=body: json_response(200, b)):
                    with self.assertRaises(client.LLMHubError) as ctx:
                        _complete()
                assert ctx.exception.category == "shape"
                assert "key_source=db" in str(ctx.exception)

    def test_non_json_200(self):
        with mock_http(lambda r: httpx.Response(200, text="<html>proxy</html>")):
            with self.assertRaises(client.LLMHubError) as ctx:
                _complete()
        assert ctx.exception.category == "shape"

    def test_sync_wrapper(self):
        with mock_http(lambda r: json_response(200, completion_body("sync ok"))):
            done = client.complete("chat_default", [{"role": "user", "content": "hi"}])
        assert done.text == "sync ok"
        assert done.key_source == "db"


@override_settings(**ISOLATED)
class ConfigErrorTests(HubTestCase):
    def setUp(self):
        reset_state()

    def test_gateway_without_key_names_all_three_sources(self):
        save_gateway(key=None)
        with self.assertRaises(client.LLMHubConfigError) as ctx:
            _complete()
        msg = str(ctx.exception)
        assert ctx.exception.category == "config"
        assert "(db)" in msg and "(env)" in msg and "default" in msg
        assert "key_source=unset" in msg

    def test_fallback_off_is_named(self):
        save_gateway(key=None, fallback_to_env=False)
        with override_settings(LLMHUB_GATEWAY_API_KEY="sk-env-but-ignored-000000"):
            with self.assertRaises(client.LLMHubConfigError) as ctx:
                _complete()
        assert "fallback_to_env is off" in str(ctx.exception)

    def test_no_route(self):
        save_gateway(lane_map={})
        with override_settings(LLMHUB_PURPOSES={"chat_default": {"label": "Chat", "tools": True}}):
            with self.assertRaises(client.LLMHubConfigError) as ctx:
                _complete()
        assert "No route" in str(ctx.exception)

    def test_no_base_url(self):
        save_gateway(base="")
        with self.assertRaises(client.LLMHubConfigError) as ctx:
            _complete()
        assert "base URL" in str(ctx.exception)

    def test_direct_mode_names_db_token_not_used(self):
        with override_settings(LLMHUB_PURPOSES={"chat_default": {"label": "Chat", "direct_route": "gpt-x"}}):
            with self.assertRaises(client.LLMHubConfigError) as ctx:
                _complete()
        msg = str(ctx.exception)
        assert "never uses the DB-stored gateway token" in msg


@override_settings(**ISOLATED)
class EmbedTests(HubTestCase):
    def setUp(self):
        reset_state()

    def _embed(self, dims):
        return client.embed(
            ["a"], route="embed-lane", base_url=GATEWAY, api_key="k" * 20, key_source="env", dimensions=dims
        )

    def test_dimension_probe_passes(self):
        with mock_http(lambda r: json_response(200, {"data": [{"embedding": [0.1] * 8}]})) as calls:
            vectors = self._embed(8)
        assert len(vectors[0]) == 8
        assert json.loads(calls[0].content) == {"model": "embed-lane", "input": ["a"]}

    def test_dimension_mismatch(self):
        with mock_http(lambda r: json_response(200, {"data": [{"embedding": [0.1] * 4}]})):
            with self.assertRaises(client.DimensionMismatch) as ctx:
                self._embed(8)
        assert ctx.exception.category == "dimension"
        assert "4-dim" in str(ctx.exception) and "key_source=env" in str(ctx.exception)

    def test_embed_http_error(self):
        with mock_http(lambda r: json_response(401, {})):
            with self.assertRaises(client.LLMHubError) as ctx:
                self._embed(8)
        assert ctx.exception.category == "auth"


TOOL_CALLS = [{"id": "c1", "type": "function", "function": {"name": "get_server_time", "arguments": "{}"}}]
RESEARCH = {
    "chat_default": {"label": "Chat", "tools": True},
    "research": {"label": "Research", "gateway_route": "research-lane", "tools": True},
}


def fake_stream(*events, delay: float = 0.0, seen: list | None = None):
    """A stand-in for an adapter's ``stream()`` that yields fixed events."""

    async def stream(self, **kwargs):
        if seen is not None:
            seen.append((self, kwargs))
        if delay:
            await asyncio.sleep(delay)
        for event in events:
            yield event

    return stream


OPENAI_STREAM = "chat.llm.openai_compat.OpenAICompatAdapter.stream"
ANTHROPIC_STREAM = "chat.llm.anthropic.AnthropicAdapter.stream"


@override_settings(**ISOLATED)
class TestPurposeChatDrawerPathTests(HubTestCase):
    """chat_default is tested through the chat drawer's own registry default + adapter."""

    def setUp(self):
        reset_state()
        save_gateway()

    def _run(self, **kw):
        return async_to_sync(client.test_purpose)("chat_default", **kw)

    @requires_llmhub_chat
    def test_tool_call_ok_gateway(self):
        seen = []
        events = (ModelResolved(name="vendor/m-1"), ToolUse(id="t1", name="get_server_time"), TurnEnd("tool_use"))
        with mock.patch(OPENAI_STREAM, fake_stream(*events, seen=seen)):
            result = self._run()
        assert result.status == "ok", result
        assert result.tool_call == "ok"
        assert result.resolved_model == "vendor/m-1"
        assert result.route == "erp-lane"
        assert result.key_source == "db"
        adapter, kwargs = seen[0]
        assert adapter._config.base_url == GATEWAY and adapter._config.api_key == DB_KEY
        assert kwargs["model"] == "erp-lane"
        assert kwargs["tools"] == [client.CHAT_TOOL_PROBE]

    @requires_llmhub_chat
    def test_no_tool_call(self):
        with mock.patch(OPENAI_STREAM, fake_stream(TextDelta("the time is noon"), TurnEnd("end_turn"))):
            result = self._run()
        assert result.status == "fail"
        assert result.category == "no_tool_call"
        assert result.tool_call == "missing"

    def test_timeout_message(self):
        with mock.patch(OPENAI_STREAM, fake_stream(TurnEnd("end_turn"), delay=1.0)):
            result = self._run(timeout=0.05)
        assert result.status == "fail"
        assert result.category == "timeout"
        assert "lane may be warming up" in result.message

    @requires_llmhub_chat
    def test_stream_error_classified(self):
        for message, category in [
            ("gateway lane returned 401: bad key", "auth"),
            ("HTTP 403: edge said no", "edge_forbidden"),
            ("gateway lane is not configured (no API key)", "config"),
            ("something odd", "upstream"),
        ]:
            with self.subTest(message=message):
                with mock.patch(OPENAI_STREAM, fake_stream(StreamError(message=message))):
                    result = self._run()
                assert result.status == "fail"
                assert result.category == category

    @requires_llmhub_chat
    def test_gateway_uses_row_default(self):
        LLMModel.objects.create(key="fast", label="Fast", route="fast-lane", sort=1)
        LLMModel.objects.create(key="deep", label="Deep", route="deep-lane", sort=2, is_default=True)
        seen = []
        with mock.patch(OPENAI_STREAM, fake_stream(ToolUse(id="t", name="get_server_time"), seen=seen)):
            result = self._run()
        assert result.status == "ok" and result.route == "deep-lane"
        assert seen[0][1]["model"] == "deep-lane"

    def test_direct_mode_uses_code_registry_default(self):
        LLMSettings.objects.filter(pk=1).update(mode="direct")
        seen = []
        with override_settings(ANTHROPIC_API_KEY="sk-ant-test"):
            with mock.patch(ANTHROPIC_STREAM, fake_stream(ToolUse(id="t", name="get_server_time"), seen=seen)):
                result = self._run()
        assert result.status == "ok", result
        assert result.key_source == "env"
        assert result.route == "claude-sonnet-5"
        assert seen[0][1]["effort"] == "medium"


@override_settings(**{**ISOLATED, "LLMHUB_PURPOSES": RESEARCH})
class TestPurposeCompletionPathTests(HubTestCase):
    """Purposes other than chat_default go through complete_async / embed."""

    def setUp(self):
        reset_state()
        save_gateway()

    def _run(self, purpose="research", **kw):
        return async_to_sync(client.test_purpose)(purpose, **kw)

    def test_tool_call_ok(self):
        def handler(request):
            body = json.loads(request.content)
            assert body["model"] == "research-lane"
            assert body["tools"][0]["function"]["name"] == "get_server_time"
            return json_response(200, completion_body("", tool_calls=TOOL_CALLS), {"x-litellm-model-name": "m-1"})

        with mock_http(handler):
            result = self._run()
        assert result.status == "ok"
        assert result.tool_call == "ok"
        assert result.resolved_model == "m-1"
        assert result.key_source == "db"
        assert result.route == "research-lane"

    def test_no_tool_call(self):
        with mock_http(lambda r: json_response(200, completion_body("the time is noon"))):
            result = self._run()
        assert result.status == "fail"
        assert result.category == "no_tool_call"
        assert result.tool_call == "missing"

    def test_timeout_message(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        with mock_http(handler):
            result = self._run(timeout=3)
        assert result.status == "fail"
        assert result.category == "timeout"
        assert "lane may be warming up" in result.message

    def test_plain_ping_when_no_tools(self):
        purposes = {**RESEARCH, "summary": {"label": "Summary", "gateway_route": "sum-lane"}}
        with override_settings(LLMHUB_PURPOSES=purposes):
            with mock_http(lambda r: json_response(200, completion_body("pong"))) as calls:
                result = self._run("summary")
        assert "tools" not in json.loads(calls[0].content)
        assert result.status == "ok" and result.tool_call == "skipped"

    def test_embedding_purpose(self):
        purposes = {
            **RESEARCH,
            "vectors": {"label": "Vectors", "kind": "embedding", "gateway_route": "embed-lane", "dimensions": 3},
        }
        with override_settings(LLMHUB_PURPOSES=purposes):
            with mock_http(lambda r: json_response(200, {"data": [{"embedding": [1, 2]}]})):
                result = self._run("vectors")
        assert result.status == "fail" and result.category == "dimension"

    def test_save_test_result(self):
        LLMSettings.objects.all().delete()  # no row yet: load() must create it
        result = client.TestResult(purpose="chat_default", status="ok", at="2026-01-01T00:00:00")
        client.save_test_result(result)
        assert LLMSettings.objects.get(pk=1).last_test["chat_default"]["status"] == "ok"


@override_settings(**ISOLATED)
class AvailableModelsTests(HubTestCase):
    def setUp(self):
        reset_state()
        save_gateway()

    def test_success_caches(self):
        with mock_http(lambda r: json_response(200, models_body("b-lane", "a-lane"))) as calls:
            listed = client.available_models()
        assert calls[0].url == httpx.URL(f"{GATEWAY}/v1/models")
        assert listed.ok and listed.ids == ["a-lane", "b-lane"] and not listed.from_cache
        row = LLMSettings.objects.get(pk=1)
        assert row.models_cache == ["a-lane", "b-lane"]
        assert row.models_cache_at is not None

    def test_failure_returns_cached_list(self):
        with mock_http(lambda r: json_response(200, models_body("a-lane"))):
            first = client.available_models()

        def boom(request):
            raise httpx.ConnectError("down", request=request)

        with mock_http(boom):
            listed = client.available_models()
        assert listed.ids == ["a-lane"]
        assert listed.from_cache
        assert listed.as_of == first.as_of
        assert listed.error_category == "unreachable"
        assert not listed.ok

    def test_failure_falls_back_to_db_cache_when_process_cache_empty(self):
        with mock_http(lambda r: json_response(200, models_body("a-lane"))):
            client.available_models()
        reset_state()  # a different worker: nothing in its process cache
        with mock_http(lambda r: json_response(500, {})):
            listed = client.available_models()
        assert listed.ids == ["a-lane"] and listed.as_of is not None
        assert listed.error_category == "upstream"

    def test_timeout_never_raises(self):
        def handler(request):
            raise httpx.ConnectTimeout("slow", request=request)

        with mock_http(handler):
            listed = client.available_models()
        assert listed.error_category == "timeout" and listed.ids == []

    def test_403_is_edge_forbidden(self):
        with mock_http(lambda r: json_response(403, {})):
            listed = client.available_models()
        assert listed.error_category == "edge_forbidden"

    def test_shape(self):
        with mock_http(lambda r: json_response(200, {"models": []})):
            assert client.available_models().error_category == "shape"

    def test_no_key_is_config(self):
        LLMSettings.objects.filter(pk=1).update(gateway_api_key_enc="")
        with mock_http(lambda r: json_response(200, models_body("x"))) as calls:
            listed = client.available_models()
        assert listed.error_category == "config" and calls == []

    def test_uses_cached_snapshot_when_given(self):
        snap = conf.snapshot(fresh=True)
        with mock_http(lambda r: json_response(200, models_body("x"))):
            assert client.available_models(snap=snap).ids == ["x"]
