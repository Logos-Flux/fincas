"""Unit tests for the chat model registry + per-model adapter wiring.

Covers: per-model credential settings (base_url_setting / api_key_setting),
the tools_mode fallback flag, the warmup_max_wait_s cold-start budget, and
extra model loading via settings.CHAT_EXTRA_MODELS.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from chat.llm import build_adapter
from chat.llm.anthropic import AnthropicAdapter
from chat.llm.openai_compat import OpenAICompatAdapter
from chat.llm.registry import ALIASES, BASE_MODELS, MODELS, ModelConfig, get_model


class TestModelConfigDefaults(SimpleTestCase):
    def test_credential_settings_default_to_openai(self):
        mc = ModelConfig(key="x", label="X", adapter="openai_compat", model_id="x")
        assert mc.base_url_setting == "OPENAI_BASE_URL"
        assert mc.api_key_setting == "OPENAI_API_KEY"

    def test_tools_mode_defaults_to_full(self):
        mc = ModelConfig(key="x", label="X", adapter="openai_compat", model_id="x")
        assert mc.tools_mode == "full"

    def test_warmup_defaults_to_disabled(self):
        mc = ModelConfig(key="x", label="X", adapter="openai_compat", model_id="x")
        assert mc.warmup_max_wait_s == 0.0

    def test_existing_anthropic_entries_keep_defaults(self):
        for key in ("claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"):
            mc = MODELS[key]
            assert mc.adapter == "anthropic"
            assert mc.tools_mode == "full"
            assert mc.base_url_setting == "OPENAI_BASE_URL"
            assert mc.api_key_setting == "OPENAI_API_KEY"

    def test_exactly_one_default_model(self):
        assert sum(1 for m in MODELS.values() if m.default) == 1


EXTRA = {
    "key": "house-llm",
    "label": "House LLM",
    "adapter": "openai_compat",
    "model_id": "house-llm",
    "base_url_setting": "HOUSE_LLM_BASE_URL",
    "api_key_setting": "HOUSE_LLM_API_KEY",
    "warmup_max_wait_s": 30.0,
}


@override_settings(CHAT_EXTRA_MODELS=[EXTRA])
class TestExtraModelEntry(SimpleTestCase):
    def test_registered(self):
        assert "house-llm" in MODELS

    def test_shape(self):
        mc = MODELS["house-llm"]
        assert mc.adapter == "openai_compat"
        assert mc.model_id == "house-llm"
        assert mc.base_url_setting == "HOUSE_LLM_BASE_URL"
        assert mc.api_key_setting == "HOUSE_LLM_API_KEY"
        assert mc.tools_mode == "full"
        assert mc.use_streaming is True
        assert mc.default is False
        assert mc.warmup_max_wait_s == 30.0

    def test_get_model_returns_it(self):
        assert get_model("house-llm") is MODELS["house-llm"]


class TestGetModelFallback(SimpleTestCase):
    def test_unknown_key_falls_back_to_default(self):
        assert get_model("does-not-exist").default is True


@override_settings(CHAT_EXTRA_MODELS=[], CHAT_EXTRA_MODEL_ALIASES={})
class TestUnconfiguredInstance(SimpleTestCase):
    """An instance that configures nothing gets the generic entries only.

    This is the golden-core shape: no self-hosted models, no customer
    endpoints, and still a resolvable default.
    """

    def test_only_base_models_registered(self):
        assert list(MODELS) == [m.key for m in BASE_MODELS]

    def test_default_still_resolves(self):
        assert get_model("anything-at-all").key == "claude-sonnet-5"


class TestRegistryModuleIsInstanceAgnostic(SimpleTestCase):
    """The module itself must stay clean — this is what golden ships."""

    def test_no_instance_names_in_source(self):
        from pathlib import Path

        from django.conf import settings

        from chat.llm import registry
        from core import tripwire_patterns

        source = Path(registry.__file__).read_text()
        # The deployment names live in the tripwire's pattern files, not here.
        vocabulary = tripwire_patterns.compile_all(settings.BASE_DIR)
        if vocabulary is not None:
            hit = vocabulary.search(source)
            assert hit is None, f"registry.py names {hit.group(0)!r}"


class TestExtraModelsLoading(SimpleTestCase):
    @override_settings(CHAT_EXTRA_MODELS=[EXTRA])
    def test_entry_is_appended_after_the_base_models(self):
        assert list(MODELS) == [m.key for m in BASE_MODELS] + ["house-llm"]

    @override_settings(CHAT_EXTRA_MODELS=[EXTRA])
    def test_entry_keeps_its_configured_fields(self):
        mc = MODELS["house-llm"]
        assert mc.label == "House LLM"
        assert mc.base_url_setting == "HOUSE_LLM_BASE_URL"
        assert mc.api_key_setting == "HOUSE_LLM_API_KEY"
        assert mc.warmup_max_wait_s == 30.0

    @override_settings(CHAT_EXTRA_MODELS=[{**EXTRA, "default": True}])
    def test_entry_can_claim_the_default(self):
        assert get_model("house-llm").default is True
        assert sum(1 for m in MODELS.values() if m.default) == 1
        assert MODELS["claude-sonnet-5"].default is False


class TestExtraModelsValidation(SimpleTestCase):
    def _assert_rejected(self, **settings_kwargs):
        with pytest.raises(ImproperlyConfigured):
            with override_settings(**settings_kwargs):
                pass

    def test_extra_models_must_be_a_list(self):
        self._assert_rejected(CHAT_EXTRA_MODELS="not-a-list")

    def test_entries_must_be_dicts(self):
        self._assert_rejected(CHAT_EXTRA_MODELS=["not-a-dict"])

    def test_missing_required_key(self):
        entry = {k: v for k, v in EXTRA.items() if k != "key"}
        self._assert_rejected(CHAT_EXTRA_MODELS=[entry])

    def test_unknown_adapter(self):
        self._assert_rejected(CHAT_EXTRA_MODELS=[{**EXTRA, "adapter": "magic"}])

    def test_cannot_shadow_a_base_model_key(self):
        self._assert_rejected(
            CHAT_EXTRA_MODELS=[{**EXTRA, "key": "claude-sonnet-5"}]
        )

    def test_duplicate_keys_within_extra_models_rejected(self):
        self._assert_rejected(
            CHAT_EXTRA_MODELS=[EXTRA, {**EXTRA, "label": "Twin"}]
        )

    def test_multiple_default_claims_rejected(self):
        self._assert_rejected(
            CHAT_EXTRA_MODELS=[
                {**EXTRA, "default": True},
                {**EXTRA, "key": "house-llm-2", "default": True},
            ]
        )

    def test_registry_is_unchanged_after_a_rejected_override(self):
        initial = list(MODELS)
        self.test_unknown_adapter()
        assert list(MODELS) == initial


class TestEmptyRegistry(SimpleTestCase):
    def test_default_model_says_so_instead_of_raising_stopiteration(self):
        from unittest import mock

        from chat.llm.registry import default_model

        with mock.patch.dict(MODELS, {}, clear=True):
            with pytest.raises(ImproperlyConfigured):
                default_model()


class TestExtraAliases(SimpleTestCase):
    @override_settings(
        CHAT_EXTRA_MODELS=[EXTRA],
        CHAT_EXTRA_MODEL_ALIASES={"house-llm-v1": "house-llm"},
    )
    def test_retired_instance_key_resolves_forward(self):
        assert get_model("house-llm-v1").key == "house-llm"

    @override_settings(CHAT_EXTRA_MODEL_ALIASES={"claude-sonnet-4-6": "claude-haiku-4-5"})
    def test_instance_can_redirect_a_base_alias(self):
        assert ALIASES["claude-sonnet-4-6"] == "claude-haiku-4-5"

    def test_alias_to_an_unregistered_model_is_rejected(self):
        with pytest.raises(ImproperlyConfigured):
            with override_settings(CHAT_EXTRA_MODEL_ALIASES={"old": "never-registered"}):
                pass

    def test_alias_shadowing_a_live_key_is_rejected(self):
        with pytest.raises(ImproperlyConfigured):
            with override_settings(CHAT_EXTRA_MODEL_ALIASES={"claude-opus-5": "claude-sonnet-5"}):
                pass


class TestBuildAdapterPerModelSettings(SimpleTestCase):
    @override_settings(
        CHAT_EXTRA_MODELS=[EXTRA],
        HOUSE_LLM_BASE_URL="https://llm.example.test",
        HOUSE_LLM_API_KEY="sk-house-test",
        OPENAI_BASE_URL="https://api.openai.com",
        OPENAI_API_KEY="sk-openai-test",
    )
    def test_custom_model_reads_its_own_settings(self):
        adapter = build_adapter(MODELS["house-llm"])
        assert isinstance(adapter, OpenAICompatAdapter)
        assert adapter._config.base_url == "https://llm.example.test"
        assert adapter._config.api_key == "sk-house-test"
        assert adapter._config.warmup_max_wait_s == MODELS["house-llm"].warmup_max_wait_s

    @override_settings(
        OPENAI_BASE_URL="https://api.openai.com",
        OPENAI_API_KEY="sk-openai-test",
    )
    def test_default_openai_compat_entry_reads_openai_settings(self):
        mc = ModelConfig(key="gpt-test", label="GPT Test", adapter="openai_compat", model_id="gpt-test")
        adapter = build_adapter(mc)
        assert isinstance(adapter, OpenAICompatAdapter)
        assert adapter._config.base_url == "https://api.openai.com"
        assert adapter._config.api_key == "sk-openai-test"
        assert adapter._config.warmup_max_wait_s == 0.0

    @override_settings(ANTHROPIC_API_KEY="sk-ant-test")
    def test_anthropic_entry_still_builds(self):
        adapter = build_adapter(MODELS["claude-sonnet-5"])
        assert isinstance(adapter, AnthropicAdapter)
