"""LLM adapters for the ERP chat.

Models are called directly against their providers (no gateway). The
registry (registry.py) maps user-facing dropdown labels to a
(provider, model_id) tuple so switching models is a dict change.
"""

from django.conf import settings

from .anthropic import AnthropicAdapter, AnthropicConfig
from .base import Event, LLMAdapter, ToolUse
from .openai_compat import OpenAICompatAdapter, OpenAICompatConfig
from .registry import MODELS, ModelConfig, get_model

__all__ = [
    "Event",
    "LLMAdapter",
    "ToolUse",
    "AnthropicAdapter",
    "AnthropicConfig",
    "OpenAICompatAdapter",
    "OpenAICompatConfig",
    "MODELS",
    "ModelConfig",
    "get_model",
    "build_adapter",
]


def build_adapter(model_config: ModelConfig) -> LLMAdapter:
    """Instantiate the right adapter for a given model registry entry.

    Reads provider credentials from Django settings:
    - "anthropic" → settings.ANTHROPIC_API_KEY
    - "openai_compat" → the settings named by the registry entry's
      `base_url_setting` / `api_key_setting` (defaults: OPENAI_BASE_URL /
      OPENAI_API_KEY), so multiple OpenAI-shape providers can coexist.
    """
    if model_config.adapter == "anthropic":
        return AnthropicAdapter(AnthropicConfig(api_key=settings.ANTHROPIC_API_KEY))
    if model_config.credentials == "llmhub":
        # Gateway lane: base URL + caller token resolve through llmhub
        # (DB row → env → default). Called from the sync request path, so
        # the TTL-cached snapshot read is allowed to touch the ORM.
        from llmhub.conf import snapshot

        snap = snapshot()
        return OpenAICompatAdapter(
            OpenAICompatConfig(
                base_url=snap.base_url.value,
                api_key=snap.api_key.value,
                provider_label=f"gateway lane {model_config.model_id!r} (key from {snap.api_key.source})",
                warmup_max_wait_s=model_config.warmup_max_wait_s,
            ),
            use_streaming=model_config.use_streaming,
        )
    if model_config.adapter == "openai_compat":
        return OpenAICompatAdapter(
            OpenAICompatConfig(
                base_url=getattr(settings, model_config.base_url_setting, "https://api.openai.com"),
                api_key=getattr(settings, model_config.api_key_setting, ""),
                provider_label=model_config.label,
                warmup_max_wait_s=model_config.warmup_max_wait_s,
            ),
            use_streaming=model_config.use_streaming,
        )
    raise ValueError(f"Unknown adapter type: {model_config.adapter}")
