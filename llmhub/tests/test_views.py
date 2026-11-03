"""The staff-only /erp/llm/ page and its POST handlers."""

from __future__ import annotations

from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import override_settings

from chat.llm.base import ToolUse
from chat.models import ChatSession
from llmhub import client, conf, crypto
from llmhub.models import LLMModel, LLMSettings, LLMSettingsAudit

from .helpers import DB_KEY, ENV_KEY, GATEWAY, ISOLATED, HubTestCase, requires_llmhub_chat, save_gateway

User = get_user_model()
URL = "/erp/llm/"
LANES = ["erp-lane", "other-lane"]


def listed(*ids, category="", message=""):
    return client.ModelsList(list(ids), None, bool(category), category, message)


class StaffPageTestCase(HubTestCase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user(username="admin", password="x", is_staff=True)
        self.client.force_login(self.staff)

    def post(self, path, data=None, **kw):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(URL + path, data or {}, **kw)

    def audit(self, action):
        return list(LLMSettingsAudit.objects.filter(action=action))


@override_settings(**ISOLATED)
class AccessTests(HubTestCase):
    def test_anonymous_redirects_to_login(self):
        for method, path in (("get", ""), ("post", "connection/"), ("post", "test/chat_default/")):
            with self.subTest(path=path):
                response = getattr(self.client, method)(URL + path)
                assert response.status_code == 302
                assert response["Location"].startswith("/erp/login/")

    def test_non_staff_forbidden(self):
        self.client.force_login(User.objects.create_user(username="clerk", password="x"))
        assert self.client.get(URL).status_code == 403
        assert self.client.post(URL + "lanes/", {"lane_chat_default": "x"}).status_code == 403
        assert not LLMSettings.objects.exists()


@override_settings(**ISOLATED)
class PageRenderTests(StaffPageTestCase):
    def test_direct_mode_renders(self):
        with mock.patch.object(client, "available_models") as models:
            response = self.client.get(URL)
        assert response.status_code == 200
        assert b"DIRECT mode" in response.content
        models.assert_not_called()  # direct mode never asks a gateway

    def test_token_never_rendered(self):
        save_gateway()
        enc = LLMSettings.objects.get(pk=1).gateway_api_key_enc
        with mock.patch.object(client, "available_models", return_value=listed(*LANES)):
            response = self.client.get(URL)
        body = response.content.decode()
        assert response.status_code == 200
        assert DB_KEY not in body and enc not in body
        assert conf.fingerprint(DB_KEY) in body

    def test_gateway_down_shows_degraded_banner(self):
        save_gateway()
        down = listed(category="unreachable", message="connection refused")
        with mock.patch.object(client, "available_models", return_value=down):
            response = self.client.get(URL)
        assert response.status_code == 200
        assert "Gateway unreachable (unreachable: connection refused)" in response.content.decode()
        assert "erp-lane" in response.content.decode()  # the DB assignment still shows

    def test_models_403_shows_runbook(self):
        save_gateway()
        with mock.patch.object(
            client, "available_models", return_value=listed(category="edge_forbidden", message="HTTP 403")
        ):
            response = self.client.get(URL)
        assert response.status_code == 200
        assert "Allow /v1/models for this caller at the edge" in response.content.decode()

    def test_real_available_models_failure_still_renders(self):
        """No mock of available_models: the real client against an unreachable host."""
        import httpx

        from .helpers import mock_http

        save_gateway()

        def boom(request):
            raise httpx.ConnectError("refused", request=request)

        with mock_http(boom):
            response = self.client.get(URL)
        assert response.status_code == 200
        assert "Gateway unreachable" in response.content.decode()


KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


@override_settings(**ISOLATED)
class UndecryptablePageTests(StaffPageTestCase):
    def test_banner_and_single_audit_row(self):
        with override_settings(LLMHUB_FERNET_KEY=KEY_A):
            save_gateway()
        with (
            override_settings(LLMHUB_FERNET_KEY=KEY_B, LLMHUB_GATEWAY_API_KEY=ENV_KEY),
            mock.patch.object(client, "available_models", return_value=listed(*LANES)),
        ):
            first = self.client.get(URL)
            second = self.client.get(URL)
        for response in (first, second):
            assert response.status_code == 200
            assert "Stored key undecryptable" in response.content.decode()
        rows = self.audit(LLMSettingsAudit.KEY_UNDECRYPTABLE)
        assert len(rows) == 1
        assert rows[0].actor == self.staff

    def test_rotation_after_undecryptable_starts_a_new_episode(self):
        with override_settings(LLMHUB_FERNET_KEY=KEY_A):
            save_gateway()
        with (
            override_settings(LLMHUB_FERNET_KEY=KEY_B),
            mock.patch.object(client, "available_models", return_value=listed(*LANES)),
        ):
            self.client.get(URL)
            response = self.post(
                "connection/",
                {"mode": "gateway", "gateway_base_url": GATEWAY, "new_api_key": "sk-fresh-token-000000-NEWK"},
            )
            assert response.status_code == 302
            rotated = self.audit(LLMSettingsAudit.KEY_ROTATED)
            assert rotated[0].old_masked == "(undecryptable)"
            snap = conf.snapshot(fresh=True)
            assert not snap.key_undecryptable and snap.api_key.value == "sk-fresh-token-000000-NEWK"


@override_settings(**ISOLATED)
class ConnectionFormTests(StaffPageTestCase):
    def _gateway_post(self, **extra):
        data = {"mode": "gateway", "gateway_base_url": GATEWAY, "new_api_key": DB_KEY}
        data.update(extra)
        with mock.patch.object(client, "available_models", return_value=listed(*LANES)):
            return self.post("connection/", data)

    @override_settings(LLMHUB_ALLOWED_BASE_URLS=["https://allowed.test"])
    def test_base_url_outside_allowlist_rejected(self):
        response = self._gateway_post(gateway_base_url="https://evil.test")
        assert response.status_code == 400
        assert "LLMHUB_ALLOWED_BASE_URLS" in response.content.decode()
        assert not LLMSettings.objects.filter(gateway_base_url="https://evil.test").exists()

    @override_settings(LLMHUB_ALLOWED_BASE_URLS=["https://allowed.test"])
    def test_base_url_inside_allowlist_saved(self):
        response = self._gateway_post(gateway_base_url="https://allowed.test/")
        assert response.status_code == 302
        assert LLMSettings.objects.get(pk=1).gateway_base_url == "https://allowed.test"

    def test_empty_allowlist_is_unlocked(self):
        response = self._gateway_post(gateway_base_url="https://anything.test")
        assert response.status_code == 302
        assert LLMSettings.objects.get(pk=1).gateway_base_url == "https://anything.test"
        row = self.audit(LLMSettingsAudit.BASE_URL_CHANGED)[0]
        assert row.actor == self.staff and row.new_masked == "https://anything.test"

    def test_gateway_without_any_key_rejected(self):
        response = self._gateway_post(new_api_key="")
        assert response.status_code == 400
        assert "Gateway mode needs a caller token" in response.content.decode()
        assert not LLMSettings.objects.exists()

    def test_gateway_with_env_key_accepted(self):
        with override_settings(LLMHUB_GATEWAY_API_KEY=ENV_KEY):
            response = self._gateway_post(new_api_key="")
        assert response.status_code == 302

    def test_whitespace_token_rejected(self):
        response = self._gateway_post(new_api_key="sk-with space-in-it-000000")
        assert response.status_code == 400

    def test_key_rotation_fingerprint_is_immediate(self):
        self._gateway_post()
        with override_settings(LLMHUB_CACHE_TTL_S=3600):
            conf.snapshot()  # prime the process cache with the old key
            new_key = "sk-rotated-caller-token-ROT8"
            # Written behind the cache's back (no signal): only an uncached read sees it.
            LLMSettings.objects.filter(pk=1).update(gateway_api_key_enc=crypto.encrypt(new_key))
            assert conf.snapshot().api_key.value == DB_KEY  # the TTL cache is stale
            with mock.patch.object(client, "available_models", return_value=listed(*LANES)):
                body = self.client.get(URL).content.decode()
        # new_key never went through a view, so only the uncached status read can show it
        # (the old fingerprint still appears, legitimately, in the audit table).
        assert conf.fingerprint(new_key) in body

    def test_key_rotation_audit(self):
        self._gateway_post()
        new_key = "sk-rotated-caller-token-ROT8"
        self._gateway_post(new_api_key=new_key)
        rows = self.audit(LLMSettingsAudit.KEY_ROTATED)
        assert len(rows) == 2
        latest = rows[0]
        assert latest.actor == self.staff
        assert latest.old_masked == conf.fingerprint(DB_KEY)
        assert latest.new_masked == conf.fingerprint(new_key)
        text = " ".join(f"{a.old_masked} {a.new_masked} {a.detail} {a.field}" for a in LLMSettingsAudit.objects.all())
        assert DB_KEY not in text and new_key not in text
        assert conf.snapshot(fresh=True).api_key.value == new_key

    def test_clear_key(self):
        self._gateway_post()
        with override_settings(LLMHUB_GATEWAY_API_KEY=ENV_KEY):
            response = self._gateway_post(new_api_key="", clear_api_key="on")
        assert response.status_code == 302
        assert LLMSettings.objects.get(pk=1).gateway_api_key_enc == ""
        assert len(self.audit(LLMSettingsAudit.KEY_CLEARED)) == 1

    def test_mode_change_audit_counts_orphaned_lanes(self):
        LLMSettings(pk=1, lane_map={"chat_default": "gpt-x"}).save()  # made in direct mode (env)
        response = self._gateway_post()
        assert response.status_code == 302
        row = self.audit(LLMSettingsAudit.MODE_CHANGED)[0]
        assert (row.old_masked, row.new_masked) == ("direct", "gateway")
        assert row.actor == self.staff
        assert "1 lane assignment(s)" in row.detail
        assert LLMSettings.objects.get(pk=1).lane_map == {"chat_default": "gpt-x"}  # kept

    def test_saving_without_mode_change_does_not_pin_mode(self):
        with override_settings(LLMHUB_MODE="gateway"):
            self._gateway_post()
        assert LLMSettings.objects.get(pk=1).mode == ""
        assert not self.audit(LLMSettingsAudit.MODE_CHANGED)

    def test_view_save_writes_no_backstop_rows(self):
        self._gateway_post()
        assert not self.audit(LLMSettingsAudit.FIELD_CHANGED)


@override_settings(**ISOLATED)
class LaneTests(StaffPageTestCase):
    def test_direct_mode_lane_saved_with_actor(self):
        response = self.post("lanes/", {"lane_chat_default": "gpt-x"})
        assert response.status_code == 302
        assert LLMSettings.objects.get(pk=1).lane_map == {"chat_default": "gpt-x"}
        row = self.audit(LLMSettingsAudit.LANE_CHANGED)[0]
        assert row.actor == self.staff
        assert (row.field, row.old_masked, row.new_masked) == ("lane_map.chat_default", "(default)", "gpt-x")
        assert not self.audit(LLMSettingsAudit.FIELD_CHANGED)

    def test_lane_not_on_gateway_rejected(self):
        save_gateway()
        with mock.patch.object(client, "available_models", return_value=listed(*LANES)):
            self.post("lanes/", {"lane_chat_default": "ghost-lane"})
        assert LLMSettings.objects.get(pk=1).lane_map == {"chat_default": "erp-lane"}
        assert not self.audit(LLMSettingsAudit.LANE_CHANGED)

    def test_lane_on_gateway_accepted(self):
        save_gateway()
        with mock.patch.object(client, "available_models", return_value=listed(*LANES)):
            self.post("lanes/", {"lane_chat_default": "other-lane"})
        assert LLMSettings.objects.get(pk=1).lane_map == {"chat_default": "other-lane"}

    def test_blank_lane_restores_default(self):
        save_gateway()
        with mock.patch.object(client, "available_models", return_value=listed(*LANES)):
            self.post("lanes/", {"lane_chat_default": ""})
        assert LLMSettings.objects.get(pk=1).lane_map == {}
        assert self.audit(LLMSettingsAudit.LANE_CHANGED)[0].new_masked == "(default)"


OPENAI_STREAM = "chat.llm.openai_compat.OpenAICompatAdapter.stream"


def _tool_stream(self, **kwargs):
    async def gen():
        yield ToolUse(id="t", name="get_server_time")

    return gen()


@override_settings(**ISOLATED)
class PurposeTestEndpointTests(StaffPageTestCase):
    @requires_llmhub_chat
    def test_returns_json_and_saves_last_test(self):
        save_gateway()
        with mock.patch(OPENAI_STREAM, _tool_stream):
            response = self.post("test/chat_default/")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok" and data["tool_call"] == "ok" and data["key_source"] == "db"
        assert LLMSettings.objects.get(pk=1).last_test["chat_default"]["status"] == "ok"
        assert not LLMSettingsAudit.objects.exists()  # bookkeeping, not an audited change

    def test_unknown_purpose_404(self):
        assert self.post("test/nope/").status_code == 404

    def test_models_refresh_json(self):
        save_gateway()
        with mock.patch.object(
            client, "available_models", return_value=listed(category="edge_forbidden", message="HTTP 403")
        ):
            data = self.post("models-refresh/").json()
        assert data["error_category"] == "edge_forbidden"
        assert "Allow /v1/models" in data["runbook"]


@override_settings(**ISOLATED)
class ChatModelCrudTests(StaffPageTestCase):
    def add(self, key, route=None, **extra):
        # Posted exactly as the page's add form does — it has no sort input.
        data = {"new-key": key, "new-label": key.title(), "new-route": route or f"{key}-lane", "new-enabled": "on"}
        data.update(extra)
        return self.post("models/add/", data)

    def action(self, obj, name):
        return self.post(f"models/{obj.pk}/{name}/")

    def test_add_first_is_default(self):
        response = self.add("fast")
        assert response.status_code == 302, response.content.decode()[:2000]
        fast = LLMModel.objects.get(key="fast")
        assert fast.is_default and fast.enabled
        self.add("deep")
        assert not LLMModel.objects.get(key="deep").is_default
        row = self.audit(LLMSettingsAudit.MODEL_ADDED)[0]
        assert row.actor == self.staff and row.field == "LLMModel[deep]"
        assert list(LLMModel.objects.values_list("key", flat=True)) == ["fast", "deep"]

    def test_add_rejects_route_not_on_gateway(self):
        save_gateway()
        with mock.patch.object(client, "available_models", return_value=listed(*LANES)):
            response = self.add("fast", route="ghost-lane")
        assert response.status_code == 400
        assert not LLMModel.objects.exists()

    def test_default_is_mutually_exclusive(self):
        for key in ("a", "b", "c"):
            self.add(key)
        a, b, c = (LLMModel.objects.get(key=k) for k in "abc")
        # Even a duplicated default (a shell write) is demoted by the one UPDATE.
        LLMModel.objects.filter(key="c").update(is_default=True)
        self.action(b, "default")
        assert list(LLMModel.objects.filter(is_default=True).values_list("key", flat=True)) == ["b"]
        row = self.audit(LLMSettingsAudit.MODEL_DEFAULT_CHANGED)[0]
        assert (row.old_masked, row.new_masked) == ("a, c", "b")

    def test_cannot_disable_default(self):
        self.add("a")
        a = LLMModel.objects.get(key="a")
        self.action(a, "toggle")
        a.refresh_from_db()
        assert a.enabled
        response = self.post(
            f"models/{a.pk}/", {f"m{a.pk}-label": "A", f"m{a.pk}-route": "a-lane", f"m{a.pk}-sort": "1"}
        )
        assert response.status_code == 302
        a.refresh_from_db()
        assert a.enabled

    def test_toggle_non_default(self):
        self.add("a")
        self.add("b")
        b = LLMModel.objects.get(key="b")
        self.action(b, "toggle")
        b.refresh_from_db()
        assert not b.enabled
        assert self.audit(LLMSettingsAudit.MODEL_TOGGLED)[0].new_masked == "False"

    def test_cannot_delete_default(self):
        self.add("a")
        self.action(LLMModel.objects.get(key="a"), "delete")
        assert LLMModel.objects.filter(key="a").exists()
        assert not self.audit(LLMSettingsAudit.MODEL_DELETED)

    def test_delete_mentions_referencing_sessions(self):
        self.add("a")
        self.add("b")
        ChatSession.objects.create(user=self.staff, model_id="b")
        ChatSession.objects.create(user=self.staff, model_id="b")
        self.action(LLMModel.objects.get(key="b"), "delete")
        assert not LLMModel.objects.filter(key="b").exists()
        row = self.audit(LLMSettingsAudit.MODEL_DELETED)[0]
        assert "2 chat session(s)" in row.detail
        assert row.actor == self.staff

    def test_reorder(self):
        for key in ("a", "b", "c"):
            self.add(key)
        order = lambda: list(LLMModel.objects.values_list("key", flat=True))  # noqa: E731
        self.action(LLMModel.objects.get(key="c"), "up")
        assert order() == ["a", "c", "b"]
        self.action(LLMModel.objects.get(key="a"), "down")
        assert order() == ["c", "a", "b"]
        self.action(LLMModel.objects.get(key="c"), "up")  # already first: no-op
        assert order() == ["c", "a", "b"]
        assert self.audit(LLMSettingsAudit.MODEL_REORDERED)[0].new_masked == "c > a > b"

    def test_edit_records_changes(self):
        self.add("a")
        a = LLMModel.objects.get(key="a")
        self.post(
            f"models/{a.pk}/",
            {
                f"m{a.pk}-label": "Renamed",
                f"m{a.pk}-route": "a-lane",
                f"m{a.pk}-sort": str(a.sort),
                f"m{a.pk}-enabled": "on",
            },
        )
        a.refresh_from_db()
        assert a.label == "Renamed"
        row = self.audit(LLMSettingsAudit.MODEL_CHANGED)[0]
        assert row.field == "LLMModel[a].label" and row.new_masked == "Renamed"

    def test_unknown_action(self):
        self.add("a")
        assert self.action(LLMModel.objects.get(key="a"), "explode").status_code == 400


class LLMModelCleanTests(HubTestCase):
    def test_unset_credential_settings_rejected(self):
        m = LLMModel(key="x", label="X", route="r", base_url_setting="NOPE_BASE_URL", api_key_setting="NOPE_KEY")
        with self.assertRaises(ValidationError) as ctx:
            m.full_clean()
        assert set(ctx.exception.message_dict) >= {"base_url_setting", "api_key_setting"}

    @override_settings(HOUSE_BASE_URL="https://house.test", HOUSE_KEY="k")
    def test_set_credential_settings_accepted(self):
        LLMModel(
            key="x", label="X", route="r", base_url_setting="HOUSE_BASE_URL", api_key_setting="HOUSE_KEY"
        ).full_clean()
