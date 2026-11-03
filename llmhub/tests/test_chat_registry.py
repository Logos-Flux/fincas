"""The chat registry driven by llmhub: gateway rows vs the direct-mode code registry."""

from __future__ import annotations

from django.test import override_settings

from chat.llm import build_adapter, registry
from chat.llm.openai_compat import OpenAICompatAdapter
from chat.llm.registry import GATEWAY_KEY, MODELS, get_model, is_known
from llmhub.models import LLMModel, LLMSettings

from .helpers import DB_KEY, GATEWAY, ISOLATED, HubTestCase, save_gateway


@override_settings(**ISOLATED)
class GatewayRegistryTests(HubTestCase):
    def setUp(self):
        super().setUp()
        save_gateway()
        LLMModel.objects.create(key="fast", label="Fast", route="fast-lane", sort=1, max_tokens=2000)
        LLMModel.objects.create(key="deep", label="Deep", route="deep-lane", sort=2, is_default=True)
        LLMModel.objects.create(key="off", label="Off", route="off-lane", sort=3, enabled=False)

    def test_models_reflect_enabled_rows_in_place(self):
        imported = MODELS  # a reference taken at import time elsewhere
        get_model("fast")  # any registry read triggers refresh()
        assert imported is registry.MODELS
        assert list(imported) == ["fast", "deep"]
        assert "off" not in imported
        assert "claude-sonnet-5" not in imported
        assert all(m.credentials == "llmhub" and m.adapter == "openai_compat" for m in imported.values())

    def test_row_fields(self):
        fast, deep = get_model("fast"), get_model("deep")
        assert fast.model_id == "fast-lane" and fast.label == "Fast"
        assert fast.max_tokens == 2000
        assert deep.max_tokens == 16000  # null row → gateway default ceiling
        assert deep.default and not fast.default

    def test_first_row_default_when_none_flagged(self):
        LLMModel.objects.update(is_default=False)
        assert registry.default_model().key == "fast"

    def test_legacy_gateway_key_resolves_to_default(self):
        assert get_model(GATEWAY_KEY).key == "deep"
        assert is_known(GATEWAY_KEY)

    def test_removed_key_is_unknown(self):
        assert is_known("fast")
        assert not is_known("off")
        assert not is_known("claude-sonnet-5")
        assert not is_known("claude-opus-4-7")  # a code-registry alias means nothing here
        assert get_model("claude-sonnet-5").key == "deep"

    def test_build_adapter_uses_llmhub_credentials(self):
        adapter = build_adapter(get_model("fast"))
        assert isinstance(adapter, OpenAICompatAdapter)
        assert adapter._config.base_url == GATEWAY
        assert adapter._config.api_key == DB_KEY
        assert "key from db" in adapter._config.provider_label

    def test_disabling_row_updates_registry(self):
        LLMModel.objects.filter(key="fast").update(enabled=False)
        get_model("deep")
        assert list(MODELS) == ["deep"]

    def test_gateway_enabled(self):
        assert registry.gateway_enabled()


@override_settings(**ISOLATED)
class GatewayNoRowsTests(HubTestCase):
    def test_fallback_entry_on_chat_default_lane(self):
        save_gateway(lane_map={"chat_default": "erp-lane"})
        model = registry.default_model()
        assert list(MODELS) == [GATEWAY_KEY]
        assert model.key == GATEWAY_KEY
        assert model.model_id == "erp-lane"
        assert model.credentials == "llmhub"
        assert model.max_tokens == 16000
        assert is_known(GATEWAY_KEY)
        assert get_model("anything").key == GATEWAY_KEY


@override_settings(**ISOLATED)
class DirectRegistryTests(HubTestCase):
    def test_direct_mode_uses_code_registry(self):
        save_gateway()
        LLMModel.objects.create(key="fast", label="Fast", route="fast-lane")
        get_model("fast")
        assert "fast" in MODELS
        LLMSettings.objects.filter(pk=1).update(mode="direct")
        get_model("x")
        assert "fast" not in MODELS
        assert "claude-sonnet-5" in MODELS
        assert registry.default_model().key == "claude-sonnet-5"
        assert get_model("claude-opus-4-7").key == "claude-opus-5"  # code aliases are back
        assert not registry.gateway_enabled()
        assert all(m.credentials == "settings" for m in MODELS.values())

    def test_no_row_is_code_registry(self):
        assert registry.default_model().key == "claude-sonnet-5"
        assert set(MODELS) == set(registry.build_code_models())
