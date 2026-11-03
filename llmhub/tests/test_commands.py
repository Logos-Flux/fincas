"""check_llm_models (drift, exit codes) and llmhub_check (smoke)."""

from __future__ import annotations

import json
from io import StringIO
from unittest import mock

import httpx
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from llmhub import client

from .helpers import (
    GATEWAY,
    ISOLATED,
    HubTestCase,
    json_response,
    mock_http,
    models_body,
    requires_llmhub_chat,
    save_gateway,
)

PROBE = dict(
    LLMHUB_EMBED_PROBE_BASE_URL=GATEWAY,
    LLMHUB_EMBED_PROBE_API_KEY="sk-probe-key-00000000",
    LLMHUB_EMBED_PROBE_ROUTE="embed-lane",
    LLMHUB_EMBED_PROBE_DIMENSIONS=8,
)


def run(name, *args):
    out = StringIO()
    call_command(name, *args, stdout=out, stderr=StringIO())
    return out.getvalue()


def gateway_handler(ids=("erp-lane",), dims=8, models_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return json_response(models_status, models_body(*ids))
        if request.url.path == "/v1/embeddings":
            assert json.loads(request.content)["model"] == "embed-lane"
            return json_response(200, {"data": [{"embedding": [0.0] * dims}]})
        raise AssertionError(f"unexpected call {request.url}")

    return handler


@override_settings(**ISOLATED)
@requires_llmhub_chat
class CheckLLMModelsTests(HubTestCase):
    def _raises(self, *args) -> CommandError:
        with self.assertRaises(CommandError) as ctx:
            run("check_llm_models", *args)
        return ctx.exception

    def test_direct_mode_needs_no_credentials(self):
        with mock_http(gateway_handler()) as calls:
            out = run("check_llm_models")
        assert "direct mode" in out and "No drift." in out
        assert "dimension probe skipped" in out
        assert calls == []

    def test_gateway_healthy(self):
        save_gateway()
        with mock_http(gateway_handler()):
            out = run("check_llm_models", "--fail-on-drift")
        assert "✓ erp-lane" in out and "No drift." in out
        assert "chat_default     key from db" in out

    def test_gateway_down_exits_2(self):
        save_gateway()

        def boom(request):
            raise httpx.ConnectError("refused", request=request)

        with mock_http(boom):
            exc = self._raises()
        assert exc.returncode == 2

    def test_gateway_401_exits_2(self):
        save_gateway()
        with mock_http(gateway_handler(models_status=401)):
            assert self._raises().returncode == 2

    def test_drift_reported_but_zero_without_flag(self):
        save_gateway()
        with mock_http(gateway_handler(ids=("other-lane",))):
            out = run("check_llm_models")
        assert "NOT offered to this key" in out and "Drift check complete." in out

    def test_drift_exits_3_with_flag(self):
        save_gateway()
        with mock_http(gateway_handler(ids=("other-lane",))):
            assert self._raises("--fail-on-drift").returncode == 3

    def test_chat_rows_count_as_expected_routes(self):
        from llmhub.models import LLMModel

        save_gateway()
        LLMModel.objects.create(key="deep", label="Deep", route="deep-lane")
        with mock_http(gateway_handler(ids=("erp-lane",))):
            exc = self._raises("--fail-on-drift")
        assert exc.returncode == 3

    @override_settings(**PROBE)
    def test_embed_probe_ok(self):
        save_gateway()
        with mock_http(gateway_handler(dims=8)):
            out = run("check_llm_models")
        assert "ok: embed-lane: 8-dim" in out

    @override_settings(**PROBE)
    def test_dimension_mismatch_exits_4(self):
        save_gateway()
        with mock_http(gateway_handler(dims=4)):
            assert self._raises().returncode == 4

    @override_settings(**PROBE)
    def test_no_embed_probe_flag(self):
        save_gateway()
        with mock_http(gateway_handler(dims=4)) as calls:
            run("check_llm_models", "--no-embed-probe")
        assert all(c.url.path != "/v1/embeddings" for c in calls)

    def test_env_fallback_flagged(self):
        save_gateway(key=None)
        with override_settings(LLMHUB_GATEWAY_API_KEY="sk-env-caller-token-000-ENVK", LLMHUB_GATEWAY_BASE_URL=GATEWAY):
            with mock_http(gateway_handler()):
                out = run("check_llm_models")
        assert "scheduled for retirement" in out


@override_settings(**ISOLATED)
class LLMHubCheckTests(HubTestCase):
    def _fails(self, *args) -> tuple[CommandError, str]:
        out = StringIO()
        with self.assertRaises(CommandError) as ctx:
            call_command("llmhub_check", *args, stdout=out, stderr=StringIO())
        return ctx.exception, out.getvalue()

    def test_no_live_direct_with_anthropic_key(self):
        with override_settings(ANTHROPIC_API_KEY="sk-ant-test"), mock_http(gateway_handler()) as calls:
            out = run("llmhub_check", "--no-live")
        assert "mode: direct (from env)" in out
        assert "claude-sonnet-5" in out and "key: ANTHROPIC_API_KEY" in out
        assert "all purposes OK" in out
        assert calls == []

    def test_no_live_direct_without_key_fails(self):
        exc, out = self._fails("--no-live")
        assert exc.returncode == 1
        assert "ANTHROPIC_API_KEY — EMPTY" in out

    @requires_llmhub_chat
    def test_no_live_gateway(self):
        save_gateway()
        out = run("llmhub_check", "--no-live")
        assert "mode: gateway (from db)" in out and "key: …DBKY (db)" in out
        assert "erp-lane" in out and "all purposes OK" in out

    def test_no_live_gateway_without_key_fails(self):
        save_gateway(key=None)
        exc, out = self._fails("--no-live")
        assert exc.returncode == 1
        assert "key: (none) (unset)" in out

    def test_no_live_other_purpose_route(self):
        purposes = {"chat_default": {"label": "Chat"}, "research": {"label": "R", "gateway_route": "research-lane"}}
        save_gateway()
        with override_settings(LLMHUB_PURPOSES=purposes):
            out = run("llmhub_check", "--no-live", "--purpose", "research")
        assert "research-lane" in out and "route from default" in out

    def test_live_ok(self):
        save_gateway()

        async def ok(name, *, snap=None, timeout=None):
            return client.TestResult(
                purpose=name,
                route="erp-lane",
                status="ok",
                resolved_model="vendor/m",
                tool_call="ok",
                key_source="db",
                elapsed_ms=12,
            )

        with mock.patch.object(client, "test_purpose", ok):
            out = run("llmhub_check")
        assert "OK 12ms · served by vendor/m · tool call OK" in out
        assert "all purposes OK" in out

    def test_live_failure_exits_1(self):
        save_gateway()

        async def fail(name, *, snap=None, timeout=None):
            return client.TestResult(
                purpose=name, route="erp-lane", category="auth", message="HTTP 401", key_source="db"
            )

        with mock.patch.object(client, "test_purpose", fail):
            exc, out = self._fails()
        assert exc.returncode == 1
        assert "auth: HTTP 401 (key from db)" in out

    def test_unknown_purpose(self):
        exc, _ = self._fails("--no-live", "--purpose", "nope")
        assert "Unknown purpose" in str(exc)

    def test_undecryptable_flagged(self):
        from cryptography.fernet import Fernet

        with override_settings(LLMHUB_FERNET_KEY=Fernet.generate_key().decode()):
            save_gateway()
        with override_settings(LLMHUB_FERNET_KEY=Fernet.generate_key().decode()):
            _, out = self._fails("--no-live")
        assert "undecryptable" in out
