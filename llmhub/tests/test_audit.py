"""Audit trail: view rows, the signal backstop, masking, on_commit, and the actor contextvar."""

from __future__ import annotations

import asyncio

from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from llmhub import audit, crypto
from llmhub.middleware import LLMHubActorMiddleware
from llmhub.models import LLMModel, LLMSettings, LLMSettingsAudit

from .helpers import DB_KEY, ISOLATED, HubTestCase, reset_state

User = get_user_model()


def _all_audit_text() -> str:
    return " ".join(f"{a.field} {a.old_masked} {a.new_masked} {a.detail}" for a in LLMSettingsAudit.objects.all())


@override_settings(**ISOLATED)
class BackstopAuditTests(HubTestCase):
    def setUp(self):
        reset_state()

    def test_creating_row_with_defaults_writes_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            LLMSettings(pk=1).save()
        assert not LLMSettingsAudit.objects.exists()

    def test_shell_save_writes_field_rows_without_actor(self):
        with self.captureOnCommitCallbacks(execute=True):
            LLMSettings(pk=1).save()
        with self.captureOnCommitCallbacks(execute=True):
            row = LLMSettings.objects.get(pk=1)
            row.gateway_base_url = "https://gw.test"
            row.mode = "gateway"
            row.save()
        rows = {a.field: a for a in LLMSettingsAudit.objects.all()}
        assert set(rows) == {"LLMSettings.gateway_base_url", "LLMSettings.mode"}
        for a in rows.values():
            assert a.action == LLMSettingsAudit.FIELD_CHANGED
            assert a.actor is None
        assert rows["LLMSettings.gateway_base_url"].new_masked == "https://gw.test"

    def test_fallback_toggle_row(self):
        with self.captureOnCommitCallbacks(execute=True):
            LLMSettings(pk=1).save()
        with self.captureOnCommitCallbacks(execute=True):
            row = LLMSettings.objects.get(pk=1)
            row.fallback_to_env = False
            row.save()
        a = LLMSettingsAudit.objects.get()
        assert a.action == LLMSettingsAudit.FALLBACK_TOGGLED
        assert (a.old_masked, a.new_masked) == ("True", "False")

    def test_token_never_appears_from_shell(self):
        with self.captureOnCommitCallbacks(execute=True):
            LLMSettings(pk=1, gateway_api_key_enc=crypto.encrypt(DB_KEY)).save()
        a = LLMSettingsAudit.objects.get(field="LLMSettings.gateway_api_key_enc")
        assert a.new_masked == "(set)" and a.old_masked == "(empty)"
        enc = LLMSettings.objects.get(pk=1).gateway_api_key_enc
        text = _all_audit_text()
        assert DB_KEY not in text and enc not in text and DB_KEY[-4:] not in text

    def test_quiet_bookkeeping_fields(self):
        with self.captureOnCommitCallbacks(execute=True):
            LLMSettings(pk=1).save()
        with self.captureOnCommitCallbacks(execute=True):
            row = LLMSettings.objects.get(pk=1)
            row.last_test = {"chat_default": {"status": "ok"}}
            row.models_cache = ["x"]
            row.save()
        assert not LLMSettingsAudit.objects.exists()

    def test_rolled_back_save_leaves_no_row(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    LLMSettings(pk=1, mode="gateway").save()
                    raise RuntimeError("abort")
            except RuntimeError:
                pass
        assert not LLMSettingsAudit.objects.exists()
        assert not LLMSettings.objects.exists()

    def test_view_write_silences_backstop(self):
        with self.captureOnCommitCallbacks(execute=True), audit.view_write():
            LLMSettings(pk=1, mode="gateway").save()
        assert not LLMSettingsAudit.objects.exists()

    def test_model_created_and_deleted_outside_views(self):
        with self.captureOnCommitCallbacks(execute=True):
            m = LLMModel.objects.create(key="k1", label="K1", route="lane-1")
        with self.captureOnCommitCallbacks(execute=True):
            m.delete()
        actions = sorted(LLMSettingsAudit.objects.values_list("action", flat=True))
        assert actions == [LLMSettingsAudit.MODEL_ADDED, LLMSettingsAudit.MODEL_DELETED]


@override_settings(**ISOLATED)
class ActorContextTests(HubTestCase):
    def setUp(self):
        reset_state()
        self.user = User.objects.create_user(username="actor", password="x", is_staff=True)

    def test_middleware_sets_and_resets_actor(self):
        seen = []

        def view(request):
            seen.append(audit.current_actor())
            return HttpResponse("ok")

        request = RequestFactory().get("/")
        request.user = self.user
        LLMHubActorMiddleware(view)(request)
        assert seen == [self.user]
        assert audit.current_actor() is None

        # A later shell write must not inherit the request's user.
        with self.captureOnCommitCallbacks(execute=True):
            LLMSettings(pk=1, mode="gateway").save()
        assert LLMSettingsAudit.objects.get().actor is None

    def test_actor_reset_even_when_view_raises(self):
        def view(request):
            raise ValueError("boom")

        request = RequestFactory().get("/")
        request.user = self.user
        with self.assertRaises(ValueError):
            LLMHubActorMiddleware(view)(request)
        assert audit.current_actor() is None

    def test_async_middleware(self):
        seen = []

        async def view(request):
            seen.append(audit.current_actor())
            return HttpResponse("ok")

        mw = LLMHubActorMiddleware(view)
        request = RequestFactory().get("/")
        request.user = self.user
        asyncio.run(mw(request))
        assert seen == [self.user]
        assert audit.current_actor() is None

    def test_actor_stamped_on_backstop_rows_inside_request(self):
        def view(request):
            LLMSettings(pk=1, mode="gateway").save()
            return HttpResponse("ok")

        request = RequestFactory().post("/")
        request.user = self.user
        with self.captureOnCommitCallbacks(execute=True):
            LLMHubActorMiddleware(view)(request)
        assert LLMSettingsAudit.objects.get().actor == self.user

    def test_full_request_does_not_leak(self):
        self.client.force_login(self.user)
        self.client.get("/erp/login/")
        assert audit.current_actor() is None


class MaskTests(HubTestCase):
    def test_mask(self):
        assert audit.mask(None) == "(empty)"
        assert audit.mask("") == "(empty)"
        assert audit.mask("secret", secret=True) == "(set)"
        assert audit.mask("x" * 300).endswith("…") and len(audit.mask("x" * 300)) == 198
