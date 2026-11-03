"""Resolution: where every LLM setting actually comes from.

Order everywhere is **DB row → env → built-in default**, and every resolved
value carries its *source*, because the first question on a 401 is "which of
the three produced this key?".

Request-path callers read a :class:`Snapshot` cached in-process for
``LLMHUB_CACHE_TTL_S`` seconds (default 60): settings are read on every chat
turn, and a query per request for a row that changes a few times a year is
waste. The price is that another worker sees an edit up to a TTL late. Two
callers must not pay it and pass ``fresh=True``: the settings page (an admin
who just pasted a key must see its fingerprint now) and ``test_purpose``
(a just-pasted key must be testable against the DB value, not a worker's
stale copy).

Gateway vs direct credentials:

- **gateway** — base URL: ``LLMSettings.gateway_base_url`` → env
  ``LLMHUB_GATEWAY_BASE_URL``. Key: the DB-stored caller token → env
  ``LLMHUB_GATEWAY_API_KEY`` (only while ``fallback_to_env`` is on).
- **direct** — the OpenAI-compatible endpoint named by
  ``LLMHUB_DIRECT_BASE_URL_SETTING`` / ``LLMHUB_DIRECT_API_KEY_SETTING``
  (defaults ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY``). Env only: the DB key
  slot holds a *gateway caller token* and is never sent to a provider.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.core.signals import setting_changed
from django.dispatch import receiver

from . import crypto
from .models import MODE_DIRECT, MODE_GATEWAY, LLMModel, LLMSettings
from .purposes import load_purposes

logger = logging.getLogger(__name__)

SOURCE_DB = "db"
SOURCE_ENV = "env"
SOURCE_DEFAULT = "default"
SOURCE_UNSET = "unset"


@dataclass(frozen=True)
class Resolved:
    # repr=False: a snapshot in a local variable must not print the key in a
    # debug page, a Sentry frame, or a %r log line.
    value: str = field(repr=False)
    source: str = ""  # db | env | default | unset

    def __bool__(self) -> bool:
        return bool(self.value)


@dataclass(frozen=True)
class ChatModelRow:
    """An enabled LLMModel row, detached from the ORM (safe to use in async code)."""

    key: str
    label: str
    route: str
    is_default: bool
    cost_hint: str
    max_tokens: int | None
    adapter: str
    model_id: str
    use_streaming: bool
    tools_mode: str
    warmup_max_wait_s: float
    effort: str | None
    thinking: str | None


@dataclass(frozen=True)
class Snapshot:
    mode: str
    mode_source: str
    base_url: Resolved
    api_key: Resolved
    key_undecryptable: bool
    fallback_to_env: bool
    # The DB base URL is no longer in LLMHUB_ALLOWED_BASE_URLS (saved before the
    # allowlist was set, or written by shell): it is ignored, and the page says so.
    base_url_rejected: bool
    lane_map: dict[str, str]
    saved: bool
    chat_models: tuple[ChatModelRow, ...]
    loaded_at: float = field(default_factory=time.monotonic)

    @property
    def gateway(self) -> bool:
        return self.mode == MODE_GATEWAY


def cache_ttl() -> float:
    return float(getattr(settings, "LLMHUB_CACHE_TTL_S", 60))


def test_timeout() -> float:
    return float(getattr(settings, "LLMHUB_TEST_TIMEOUT_S", 15))


def fingerprint(value: str) -> str:
    """Last 4 characters, never more than a quarter of the value."""
    if not value:
        return ""
    if len(value) < 16:
        return "…" + "*" * 4
    return "…" + value[-4:]


# ---- resolution primitives ---------------------------------------------


def _env(name: str) -> str:
    return (getattr(settings, name, "") or "").strip()


def resolve_mode(row: LLMSettings, saved: bool) -> tuple[str, str]:
    if saved and row.mode in (MODE_DIRECT, MODE_GATEWAY):
        return row.mode, SOURCE_DB
    env = _env("LLMHUB_MODE").lower()
    if env in (MODE_DIRECT, MODE_GATEWAY):
        return env, SOURCE_ENV
    return MODE_DIRECT, SOURCE_DEFAULT


def resolve_endpoint(row: LLMSettings, mode: str) -> tuple[Resolved, Resolved, bool, bool]:
    """(base_url, api_key, key_undecryptable, base_url_rejected) for the given mode."""
    if mode == MODE_DIRECT:
        base_setting = getattr(settings, "LLMHUB_DIRECT_BASE_URL_SETTING", "OPENAI_BASE_URL")
        key_setting = getattr(settings, "LLMHUB_DIRECT_API_KEY_SETTING", "OPENAI_API_KEY")
        base = _env(base_setting)
        key = _env(key_setting)
        return (
            Resolved(base, SOURCE_ENV) if base else Resolved("https://api.openai.com", SOURCE_DEFAULT),
            Resolved(key, SOURCE_ENV) if key else Resolved("", SOURCE_UNSET),
            False,
            False,
        )

    from .models import is_allowed_base_url

    rejected = bool(row.gateway_base_url) and not is_allowed_base_url(row.gateway_base_url)
    if row.gateway_base_url and not rejected:
        base = Resolved(row.gateway_base_url.rstrip("/"), SOURCE_DB)
    elif _env("LLMHUB_GATEWAY_BASE_URL"):
        base = Resolved(_env("LLMHUB_GATEWAY_BASE_URL").rstrip("/"), SOURCE_ENV)
    else:
        base = Resolved("", SOURCE_UNSET)

    undecryptable = False
    db_key = ""
    if row.gateway_api_key_enc:
        try:
            db_key = crypto.decrypt(row.gateway_api_key_enc)
            moved = crypto.rotated(row.gateway_api_key_enc)
            if moved is not None and row.pk is not None:
                # Stored under the derived key: move it under the dedicated
                # one now, so a SECRET_KEY rotation can no longer void it.
                # Same secret, so no audit row (llmhub.signals) is due.
                type(row)._base_manager.filter(pk=row.pk, gateway_api_key_enc=row.gateway_api_key_enc).update(
                    gateway_api_key_enc=moved)
                row.gateway_api_key_enc = moved
        except crypto.InvalidToken:
            # Designed state, not an exception: an undecryptable key (the
            # Fernet key changed under it) must not 500 chat, forge AND the
            # settings page needed to fix it. Treat it as unset.
            undecryptable = True
    # The env key is only ever sent to the env base URL: a host set on the page
    # gets the page's own token or nothing, so editing the URL cannot exfiltrate
    # a credential the editor never saw.
    env_base = _env("LLMHUB_GATEWAY_BASE_URL").rstrip("/")
    env_key_ok = base.source != SOURCE_DB or base.value == env_base
    if db_key:
        key = Resolved(db_key, SOURCE_DB)
    elif row.fallback_to_env and env_key_ok and _env("LLMHUB_GATEWAY_API_KEY"):
        key = Resolved(_env("LLMHUB_GATEWAY_API_KEY"), SOURCE_ENV)
    else:
        key = Resolved("", SOURCE_UNSET)
    return base, key, undecryptable, rejected


def route_for(purpose: str, snap: "Snapshot | None" = None) -> Resolved:
    """The route (lane or model id) a purpose resolves to in the active mode."""
    snap = snap or snapshot()
    spec = load_purposes().get(purpose)
    if spec is None:
        raise KeyError(f"Unknown llmhub purpose {purpose!r} — register it in LLMHUB_PURPOSES.")
    assigned = (snap.lane_map or {}).get(purpose, "")
    if assigned:
        return Resolved(assigned, SOURCE_DB)
    default = spec.gateway_route if snap.gateway else spec.direct_route
    return Resolved(default, SOURCE_DEFAULT) if default else Resolved("", SOURCE_UNSET)


# ---- snapshot + cache --------------------------------------------------

_lock = threading.Lock()
_cached: Snapshot | None = None
_warned_env_fallback = False


def _chat_rows() -> tuple[ChatModelRow, ...]:
    rows = LLMModel.objects.filter(enabled=True).order_by("sort", "id")
    return tuple(
        ChatModelRow(
            key=r.key,
            label=r.label,
            route=r.route,
            is_default=r.is_default,
            cost_hint=r.cost_hint,
            max_tokens=r.max_tokens,
            adapter=r.adapter,
            model_id=r.model_id,
            use_streaming=r.use_streaming,
            tools_mode=r.tools_mode,
            warmup_max_wait_s=r.warmup_max_wait_s,
            effort=r.effort,
            thinking=r.thinking,
        )
        for r in rows
    )


def build_snapshot(row: LLMSettings | None = None) -> Snapshot:
    """Read the DB (sync ORM) and resolve everything. Never cached by itself."""
    global _warned_env_fallback
    if row is None:
        row = LLMSettings.objects.filter(pk=1).first()
    saved = row is not None
    row = row or LLMSettings(pk=1)
    mode, mode_source = resolve_mode(row, saved)
    base, key, undecryptable, rejected = resolve_endpoint(row, mode)
    if mode == MODE_GATEWAY and key.source == SOURCE_ENV and not _warned_env_fallback:
        _warned_env_fallback = True
        logger.warning(
            "llmhub: gateway key supplied by env fallback (LLMHUB_GATEWAY_API_KEY); "
            "scheduled for retirement — paste the caller token on /erp/llm/ and turn fallback_to_env off."
        )
    return Snapshot(
        mode=mode,
        mode_source=mode_source,
        base_url=base,
        api_key=key,
        key_undecryptable=undecryptable,
        fallback_to_env=row.fallback_to_env,
        base_url_rejected=rejected,
        lane_map=dict(row.lane_map or {}),
        saved=saved,
        chat_models=_chat_rows(),
    )


def _in_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def snapshot(*, fresh: bool = False) -> Snapshot:
    """The current resolved configuration (TTL-cached unless ``fresh``).

    Inside a running event loop the sync ORM is off-limits; there the last
    cached snapshot is returned even if stale, and ``asnapshot()`` is the
    way to force a read.
    """
    global _cached
    now = time.monotonic()
    with _lock:
        cached = _cached
    if not fresh and cached is not None and now - cached.loaded_at < cache_ttl():
        return cached
    if _in_event_loop():
        if cached is not None:
            return cached
        raise RuntimeError("llmhub.conf.snapshot() needs the ORM; use `await asnapshot()` from async code.")
    snap = build_snapshot()
    with _lock:
        _cached = snap
    return snap


async def asnapshot(*, fresh: bool = False) -> Snapshot:
    from asgiref.sync import sync_to_async

    return await sync_to_async(snapshot)(fresh=fresh)


def invalidate() -> None:
    """Drop this process's cached snapshot (other workers catch up within the TTL)."""
    global _cached
    with _lock:
        _cached = None


@receiver(setting_changed)
def _settings_changed(sender, setting: str, **kwargs: Any) -> None:
    if setting.startswith(("LLMHUB_", "OPENAI_")):
        invalidate()
