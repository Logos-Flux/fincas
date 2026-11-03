"""Model registry — dropdown labels → provider, model id, adapter class.

The dropdown in templates/chat/drawer.html iterates over MODELS.values()
and uses `key` as the submitted form value, `label` as the displayed text.

Each entry carries an `adapter` string that tells the view which
LLMAdapter subclass to instantiate for that model:

- "anthropic" → AnthropicAdapter (direct Anthropic Messages API)
- "openai_compat" → OpenAICompatAdapter (any OpenAI-shape provider)

`MODELS` is **built**, not written: `BASE_MODELS` below holds the generic
entries every deployment gets, and each instance adds its own through the
`CHAT_EXTRA_MODELS` setting (a list of ModelConfig kwargs dicts). Adding a
self-hosted or per-customer endpoint is therefore a settings change, not an
edit to this module. Retired keys work the same way — `BASE_ALIASES` here,
`CHAT_EXTRA_MODEL_ALIASES` in instance settings.

That split is deliberate: this file ships in the reusable core, so it must
not name any one deployment's models. A deployment that configures nothing
gets the generic Claude entries; one with no provider credentials at all
gets a dropdown it never uses, which is the correct outcome for a customer
who hasn't bought into chat.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Any, Literal

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

ADAPTERS = ("anthropic", "openai_compat")


@dataclass(frozen=True)
class ModelConfig:
    key: str                           # Stable identifier persisted on ChatSession.model_id
    label: str                         # What the user sees in the dropdown
    adapter: Literal["anthropic", "openai_compat"]
    model_id: str                      # The provider's native model identifier
    use_streaming: bool = True         # False for providers whose streaming tool-call parser is broken
    default: bool = False              # Exactly one should be default
    cost_hint: str = ""                # Optional short blurb for the dropdown
    # Which Django settings hold the endpoint credentials for this model.
    # Only consumed by adapter="openai_compat" — lets multiple OpenAI-shape
    # providers (OpenAI proper, self-hosted vLLM, Modal, …) coexist in the
    # registry with distinct base URLs and API keys.
    base_url_setting: str = "OPENAI_BASE_URL"
    api_key_setting: str = "OPENAI_API_KEY"
    # "full" sends the whole ERP tool catalog; "none" sends no tools at all.
    # Last rung of the fallback ladder for providers whose tool-call parsing
    # is unusable even on the blocking path (use_streaming=False comes first).
    tools_mode: Literal["full", "none"] = "full"
    # If > 0, the openai_compat adapter treats upstream HTTP 503 as
    # "cold start in progress" and retries with backoff for up to this many
    # seconds before giving up with a friendly error. Used for scale-to-zero
    # deployments (Modal vLLM takes ~5 min to warm and serves 503 meanwhile).
    warmup_max_wait_s: float = 0.0
    # Per-model output ceiling. None → fall back to settings.CHAT_MAX_TOKENS.
    # On Claude 5 models this budget covers thinking *and* the visible reply,
    # so entries that think need more headroom than the global default.
    max_tokens: int | None = None
    # Anthropic `output_config.effort` — "low" | "medium" | "high" | "xhigh" |
    # "max". Only set it on models that support it (Claude Opus 4.5+, Sonnet
    # 4.6+); Haiku 4.5 rejects the field with a 400.
    effort: str | None = None
    # Anthropic `thinking.type` — "adaptive" | "disabled". None omits the
    # field, which means adaptive on Claude 5 models and no thinking on older
    # ones. Leave it None for tool-using models: with thinking disabled,
    # Opus 5 occasionally writes a tool call into its visible text instead of
    # emitting a tool_use block, and the call silently never runs.
    thinking: str | None = None
    # Where the endpoint credentials come from. "settings" reads the Django
    # settings named by base_url_setting / api_key_setting; "llmhub" resolves
    # base URL + key through llmhub (DB row → env → default) — the gateway-mode
    # entries built from LLMModel rows use it.
    credentials: Literal["settings", "llmhub"] = "settings"


# The entries every deployment gets. Order matters — it's the display order
# in the dropdown, and instance entries append after these.
#
# Generic only: hosted models reachable with a provider key anyone can buy.
# Self-hosted and per-customer endpoints belong in CHAT_EXTRA_MODELS.
BASE_MODELS: list[ModelConfig] = [
    ModelConfig(
        key="claude-sonnet-5",
        label="Claude Sonnet 5",
        adapter="anthropic",
        model_id="claude-sonnet-5",
        default=True,
        cost_hint="Balanced speed and quality. Default.",
        max_tokens=16000,
        effort="medium",
    ),
    ModelConfig(
        key="claude-opus-5",
        label="Claude Opus 5",
        adapter="anthropic",
        model_id="claude-opus-5",
        cost_hint="Highest quality, slowest, most expensive.",
        max_tokens=16000,
        effort="medium",
    ),
    ModelConfig(
        key="claude-haiku-4-5",
        label="Claude Haiku 4.5",
        adapter="anthropic",
        model_id="claude-haiku-4-5-20251001",
        cost_hint="Fast and cheap.",
    ),
]


# Retired registry keys → their current replacement. ChatSession.model_id is
# persisted, so sessions started before a model swap keep sending the old key
# forever. Without this map they'd all silently land on the default entry,
# which quietly downgrades an Opus conversation to Sonnet. Add a row here
# whenever a BASE_MODELS entry is replaced rather than deleting the key
# outright; instances do the same for their own entries through
# CHAT_EXTRA_MODEL_ALIASES.
BASE_ALIASES: dict[str, str] = {
    "claude-sonnet-4-6": "claude-sonnet-5",
    "claude-opus-4-7": "claude-opus-5",
    "claude-opus-4-6": "claude-opus-5",
    "claude-sonnet-4-5": "claude-sonnet-5",
}


def _model_from_config(entry: Any, index: int) -> ModelConfig:
    """Turn one CHAT_EXTRA_MODELS dict into a validated ModelConfig.

    Every failure here raises rather than skipping the entry: a model that
    silently vanishes from the dropdown looks identical to one that was never
    configured, and the deployment that hits it is the one least able to tell
    the difference.
    """
    where = f"CHAT_EXTRA_MODELS[{index}]"
    if isinstance(entry, ModelConfig):
        return entry
    if not isinstance(entry, dict):
        raise ImproperlyConfigured(f"{where} must be a dict of ModelConfig kwargs, got {type(entry).__name__}.")
    try:
        model = ModelConfig(**entry)
    except TypeError as exc:
        raise ImproperlyConfigured(f"{where} is not a valid ModelConfig: {exc}") from exc
    if model.adapter not in ADAPTERS:
        raise ImproperlyConfigured(
            f"{where} has adapter={model.adapter!r}; must be one of {', '.join(ADAPTERS)}."
        )
    for field in ("key", "label", "model_id"):
        if not getattr(model, field):
            raise ImproperlyConfigured(f"{where} is missing a non-empty {field}.")
    return model


def load_extra_models() -> list[ModelConfig]:
    """Instance-supplied registry entries, from settings.CHAT_EXTRA_MODELS."""
    configured = getattr(settings, "CHAT_EXTRA_MODELS", []) or []
    if not isinstance(configured, (list, tuple)):
        raise ImproperlyConfigured(
            f"CHAT_EXTRA_MODELS must be a list of ModelConfig kwargs dicts, got {type(configured).__name__}."
        )
    return [_model_from_config(entry, i) for i, entry in enumerate(configured)]


# Registry key used when gateway mode has no LLMModel rows and falls back to
# the chat_default purpose's lane. Sessions from the single-lane era persisted
# this key too, so it stays resolvable (aliased to the default) forever.
GATEWAY_KEY = "gateway"


def build_code_models() -> dict[str, ModelConfig]:
    """BASE_MODELS + the instance's own entries, keyed by registry key.

    This is the direct-mode registry. An instance entry may claim
    `default=True`, in which case it becomes the sole default and the base
    default is demoted — a deployment whose only working credentials are for
    its own endpoint has to be able to land there without editing core.
    """
    extra = load_extra_models()
    defaults = [m.key for m in extra if m.default]
    if len(defaults) > 1:
        raise ImproperlyConfigured(
            "CHAT_EXTRA_MODELS declares more than one default model: " + ", ".join(defaults) + "."
        )

    base = [replace(m, default=False) for m in BASE_MODELS] if defaults else list(BASE_MODELS)

    models: dict[str, ModelConfig] = {}
    for model in base + extra:
        if model.key in models:
            raise ImproperlyConfigured(
                f"CHAT_EXTRA_MODELS reuses the registry key {model.key!r}, which is already taken."
            )
        models[model.key] = model
    return models


def build_gateway_models(snap) -> dict[str, ModelConfig]:
    """Gateway mode: one entry per enabled LLMModel row, each a gateway lane.

    Every entry calls its `route` (a lane) through the llmhub-resolved base
    URL and caller token; which concrete model serves a lane is set in the
    gateway's own admin UI. With no rows configured, the drawer still works:
    it falls back to one entry on the chat_default purpose's lane — an empty
    dropdown would read as a bug.
    """
    from llmhub.conf import route_for

    default_tokens = int(getattr(settings, "LLMHUB_CHAT_MAX_TOKENS", 16000))
    models: dict[str, ModelConfig] = {}
    rows = list(snap.chat_models)
    has_default = any(r.is_default for r in rows)
    for i, row in enumerate(rows):
        models[row.key] = ModelConfig(
            key=row.key,
            label=row.label,
            adapter="openai_compat",
            model_id=row.route,
            use_streaming=row.use_streaming,
            default=row.is_default if has_default else i == 0,
            cost_hint=row.cost_hint,
            tools_mode=row.tools_mode,
            warmup_max_wait_s=row.warmup_max_wait_s,
            # Lanes can point at reasoning models, whose hidden reasoning
            # bills against max_tokens; the 4096 global default leaves them
            # nothing for the visible reply.
            max_tokens=row.max_tokens or default_tokens,
            credentials="llmhub",
        )
    if not models:
        lane = route_for("chat_default", snap).value
        models[GATEWAY_KEY] = ModelConfig(
            key=GATEWAY_KEY,
            label=lane or "(no chat lane configured)",
            adapter="openai_compat",
            model_id=lane,
            default=True,
            cost_hint="Model is chosen by the gateway lane.",
            max_tokens=default_tokens,
            credentials="llmhub",
        )
    return models


def build_models(snap=None) -> dict[str, ModelConfig]:
    """The registry for the active llmhub mode (code registry when unknown)."""
    if snap is not None and snap.gateway:
        return build_gateway_models(snap)
    return build_code_models()


def build_aliases(models: dict[str, ModelConfig]) -> dict[str, str]:
    """Retired key → live key.

    Direct mode: BASE_ALIASES + settings.CHAT_EXTRA_MODEL_ALIASES, validated.
    Gateway mode: code-registry aliases name models that are not offered, so
    only the single-lane-era `gateway` key is mapped (to the default entry);
    every other unknown key falls through get_model() to the default anyway.
    """
    if any(m.credentials == "llmhub" for m in models.values()):
        if GATEWAY_KEY in models:
            return {}
        default = next((m.key for m in models.values() if m.default), next(iter(models)))
        return {GATEWAY_KEY: default}
    configured = getattr(settings, "CHAT_EXTRA_MODEL_ALIASES", {}) or {}
    if not isinstance(configured, dict):
        raise ImproperlyConfigured(
            f"CHAT_EXTRA_MODEL_ALIASES must be a dict of retired key → live key, got {type(configured).__name__}."
        )
    for retired, current in configured.items():
        if retired in models:
            raise ImproperlyConfigured(
                f"CHAT_EXTRA_MODEL_ALIASES maps {retired!r}, which is still a live registry key."
            )
        if current not in models:
            raise ImproperlyConfigured(
                f"CHAT_EXTRA_MODEL_ALIASES maps {retired!r} to {current!r}, which is not a registered model."
            )
    return {**BASE_ALIASES, **configured}


# Mutated in place — never rebound — so that every `from … import MODELS`
# binding (context processor, views, drawer, check commands) stays live.
# Built from code at import (no DB access at import time); refresh() swaps in
# the llmhub-driven registry on first use and whenever the llmhub snapshot
# (TTL-cached) changes.
MODELS: dict[str, ModelConfig] = build_models()
ALIASES: dict[str, str] = build_aliases(MODELS)
_built_from: list = [None]  # the llmhub Snapshot MODELS was last built from


_refresh_lock = threading.Lock()


def _replace(models: dict[str, ModelConfig]) -> None:
    aliases = build_aliases(models)
    if models == MODELS and aliases == ALIASES:
        return  # nothing changed: never touch the dicts other threads are reading
    MODELS.clear()
    MODELS.update(models)
    ALIASES.clear()
    ALIASES.update(aliases)


def refresh() -> None:
    """Rebuild MODELS/ALIASES in place if the llmhub configuration changed.

    Cheap when nothing changed (the snapshot is TTL-cached and compared by
    identity). Never raises: without a readable llmhub configuration — the
    table not migrated yet, or no DB — the current registry stays.
    """
    try:
        from llmhub.conf import snapshot

        snap = snapshot()
    except Exception:  # noqa: BLE001 — no readable config (no table / no DB / event loop): keep current
        return
    if snap is _built_from[0]:
        return
    with _refresh_lock:
        if snap is _built_from[0]:
            return
        # _replace() skips the mutation when the rebuilt registry is equal, so
        # the every-TTL snapshot turnover costs a compare, not a clear/update
        # window in which a reader could see an empty MODELS.
        _replace(build_models(snap))
        _built_from[0] = snap


@receiver(setting_changed)
def _reload_registry(sender, setting, **kwargs) -> None:
    """Rebuild the registry when a test overrides the instance settings.

    Both names are read once at import, so without this an override_settings
    block would assert against the registry the *previous* settings built.
    Updated in place because callers hold a direct reference to these dicts.
    """
    if setting not in ("CHAT_EXTRA_MODELS", "CHAT_EXTRA_MODEL_ALIASES", "LLMHUB_MODE", "LLMHUB_CHAT_MAX_TOKENS"):
        return
    _built_from[0] = None
    _replace(build_code_models())


def default_model() -> ModelConfig:
    """The entry flagged `default=True`, or the first one if none is."""
    refresh()
    for m in MODELS.values():
        if m.default:
            return m
    if not MODELS:
        # Only reachable for a deployment that strips BASE_MODELS and
        # configures no replacements. Say so, rather than surfacing a
        # StopIteration from three frames down inside a chat request.
        raise ImproperlyConfigured(
            "No chat models are registered — add entries to BASE_MODELS or settings.CHAT_EXTRA_MODELS."
        )
    return next(iter(MODELS.values()))


def gateway_enabled() -> bool:
    """True when chat is routed through the llmhub gateway (lanes, not models)."""
    refresh()
    return any(m.credentials == "llmhub" for m in MODELS.values())


def default_model_key() -> str:
    """Registry key of the default model — the fallback for a missing model_id."""
    return default_model().key


def is_known(key: str) -> bool:
    """True if `key` is a live entry or a mapped alias (not a removed model)."""
    refresh()
    return key in MODELS or (key in ALIASES and ALIASES[key] in MODELS)


def get_model(key: str) -> ModelConfig:
    """Look up a model by its registry key, falling back to the default.

    Resolves retired keys through ALIASES first, so an old session that
    stored `claude-opus-4-7` continues on Opus rather than dropping to the
    Sonnet default. Anything still unknown (a model we removed outright,
    or a garbage value from the browser) falls back to the default.
    """
    refresh()
    if key in MODELS:
        return MODELS[key]
    aliased = ALIASES.get(key)
    if aliased and aliased in MODELS:
        return MODELS[aliased]
    return default_model()
