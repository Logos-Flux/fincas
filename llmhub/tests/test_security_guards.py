"""Guards added after review: credential exfiltration via the base URL, stale
allowlist entries, and secrets in reprs."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import override_settings

from llmhub import conf
from llmhub.client import Endpoint
from llmhub.models import LLMSettings, validate_base_url

from .helpers import (
    DB_KEY,
    ENV_KEY,
    GATEWAY,
    ISOLATED,
    HubTestCase,
    json_response,
    mock_http,
    models_body,
    save_gateway,
)


@override_settings(**ISOLATED)
class EnvKeyStaysWithEnvHostTests(HubTestCase):
    def test_env_key_not_sent_to_a_page_set_host(self):
        save_gateway(key=None, base="https://attacker.example")
        with override_settings(LLMHUB_GATEWAY_BASE_URL=GATEWAY, LLMHUB_GATEWAY_API_KEY=ENV_KEY):
            snap = conf.snapshot(fresh=True)
        assert snap.base_url.value == "https://attacker.example"
        assert not snap.api_key and snap.api_key.source == "unset"

    def test_env_key_used_when_db_host_equals_env_host(self):
        save_gateway(key=None, base=GATEWAY)
        with override_settings(LLMHUB_GATEWAY_BASE_URL=GATEWAY, LLMHUB_GATEWAY_API_KEY=ENV_KEY):
            snap = conf.snapshot(fresh=True)
        assert snap.api_key.value == ENV_KEY and snap.api_key.source == "env"

    def test_db_url_outside_current_allowlist_is_ignored(self):
        save_gateway(key=DB_KEY, base="https://old.example")  # saved while unlocked
        with override_settings(LLMHUB_ALLOWED_BASE_URLS=[GATEWAY], LLMHUB_GATEWAY_BASE_URL=GATEWAY):
            snap = conf.snapshot(fresh=True)
        assert snap.base_url_rejected
        assert snap.base_url.value == GATEWAY and snap.base_url.source == "env"


@override_settings(**ISOLATED)
class BaseUrlChangeNeedsTokenTests(HubTestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user("s", password="pw", is_staff=True)
        self.client.force_login(self.staff)

    def test_moving_url_without_token_is_refused(self):
        save_gateway(key=DB_KEY)
        # The 400 re-render lists the gateway's models: answer it from a mock,
        # never the network (core.testing.no_network).
        with mock_http(lambda request: json_response(body=models_body("erp-lane"))):
            resp = self.client.post(
                "/erp/llm/connection/",
                {"mode": "gateway", "gateway_base_url": "https://other.example", "new_api_key": ""},
            )
        assert resp.status_code == 400
        assert LLMSettings.objects.get(pk=1).gateway_base_url == GATEWAY

    def test_moving_url_with_new_token_is_accepted(self):
        save_gateway(key=DB_KEY)
        resp = self.client.post(
            "/erp/llm/connection/",
            {
                "mode": "gateway",
                "gateway_base_url": "https://other.example",
                "new_api_key": "sk-new-token-for-other-0001",
            },
        )
        assert resp.status_code == 302
        assert LLMSettings.objects.get(pk=1).gateway_base_url == "https://other.example"


@override_settings(**ISOLATED)
class ValidatorAndReprTests(HubTestCase):
    def test_rejects_credentials_in_url(self):
        with self.assertRaises(ValidationError):
            validate_base_url("https://user:pass@gw.example")

    def test_http_only_for_local_hosts(self):
        validate_base_url("http://127.0.0.1:4000")
        validate_base_url("http://10.0.0.5:4000")
        with self.assertRaises(ValidationError):
            validate_base_url("http://gw.example")

    @override_settings(LLMHUB_ALLOWED_BASE_URLS=[" https://gw.example ", ""])
    def test_allowlist_entries_are_stripped(self):
        validate_base_url("https://gw.example")

    def test_secrets_not_in_reprs(self):
        ep = Endpoint(purpose="p", mode="gateway", base_url=GATEWAY, api_key=DB_KEY, key_source="db", route="r")
        assert DB_KEY not in repr(ep)
        assert DB_KEY not in repr(conf.Resolved(DB_KEY, "db"))
