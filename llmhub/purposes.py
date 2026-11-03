"""The purposes registry: which LLM jobs this deployment routes, and how.

A *purpose* is a job the ERP needs an LLM for ("chat_default", "research").
Callers ask for a purpose, never a model: ``client.complete("research", …)``.
Each purpose resolves to a *route* — a gateway lane in gateway mode, a
concrete model id in direct mode — through ``LLMSettings.lane_map`` first,
then this registry's defaults.

Configured by ``settings.LLMHUB_PURPOSES``, a dict of purpose name → spec::

    LLMHUB_PURPOSES = {
        "chat_default": {
            "label": "ERP chat (default)",
            "description": "The chat drawer's default model.",
            "kind": "chat",            # "chat" | "embedding"
            "gateway_route": "erp-assistant",   # default lane (gateway mode)
            "direct_route": "",        # default model id (direct mode)
            "tools": True,             # test with a tool-call probe
            "direct_handler": "",      # direct mode served by the caller's own client
        },
    }

Adding a purpose is one entry here plus one call site — no migration: the
per-purpose assignment lives in a JSON map, not a column per purpose.

Validated when the app loads. Every problem raises ``ImproperlyConfigured``:
a purpose that silently vanished from the settings page would look exactly
like one that was never configured.

``settings.LLMHUB_EXTERNAL_PURPOSES`` is an optional, *read-only* list of
purposes other services own (a worker, a search engine). They are shown on
the settings page for the whole map, but llmhub never routes them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

KINDS = ("chat", "embedding")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

DEFAULT_PURPOSES: dict[str, dict[str, Any]] = {
    "chat_default": {
        "label": "ERP chat (default)",
        "description": "The chat drawer's default model, and its fallback when no chat models are configured.",
        "kind": "chat",
        "gateway_route": "",
        "direct_route": "",
        "tools": True,
    },
}


@dataclass(frozen=True)
class Purpose:
    name: str
    label: str
    description: str = ""
    kind: str = "chat"
    gateway_route: str = ""
    direct_route: str = ""
    tools: bool = False
    dimensions: int | None = None  # embedding purposes: the pinned vector length
    # In direct mode this purpose is served by its caller's own client, not by
    # llmhub (e.g. "forge Gemini client (GEMINI_API_KEY)"). Tests and
    # llmhub_check then report it as delegated instead of failing it.
    direct_handler: str = ""


@dataclass(frozen=True)
class ExternalPurpose:
    name: str
    label: str
    route: str
    owner: str
    door: str = ""
    note: str = ""


def _purpose(name: Any, spec: Any) -> Purpose:
    where = f"LLMHUB_PURPOSES[{name!r}]"
    if not isinstance(name, str) or not _NAME.match(name):
        raise ImproperlyConfigured(f"{where}: purpose names must match {_NAME.pattern}.")
    if not isinstance(spec, dict):
        raise ImproperlyConfigured(f"{where} must be a dict, got {type(spec).__name__}.")
    try:
        purpose = Purpose(name=name, **spec)
    except TypeError as exc:
        raise ImproperlyConfigured(f"{where} is not a valid purpose spec: {exc}") from exc
    if not purpose.label:
        raise ImproperlyConfigured(f"{where} is missing a non-empty label.")
    if purpose.kind not in KINDS:
        raise ImproperlyConfigured(f"{where} has kind={purpose.kind!r}; must be one of {', '.join(KINDS)}.")
    if purpose.kind == "embedding" and purpose.tools:
        raise ImproperlyConfigured(f"{where}: an embedding purpose cannot request a tool-call probe.")
    return purpose


def load_purposes() -> dict[str, Purpose]:
    """The validated purposes registry, in declaration order."""
    configured = getattr(settings, "LLMHUB_PURPOSES", None)
    if configured is None:
        configured = DEFAULT_PURPOSES
    if not isinstance(configured, dict):
        raise ImproperlyConfigured(
            f"LLMHUB_PURPOSES must be a dict of purpose name → spec, got {type(configured).__name__}."
        )
    purposes = {name: _purpose(name, spec) for name, spec in configured.items()}
    if "chat_default" not in purposes:
        raise ImproperlyConfigured(
            "LLMHUB_PURPOSES must define 'chat_default' — the chat drawer falls back to it."
        )
    if purposes["chat_default"].kind != "chat":
        raise ImproperlyConfigured("LLMHUB_PURPOSES['chat_default'] must have kind='chat'.")
    return purposes


def load_external_purposes() -> list[ExternalPurpose]:
    """Read-only topology rows for purposes other services own."""
    configured = getattr(settings, "LLMHUB_EXTERNAL_PURPOSES", []) or []
    if not isinstance(configured, (list, tuple)):
        raise ImproperlyConfigured("LLMHUB_EXTERNAL_PURPOSES must be a list of dicts.")
    rows = []
    for i, spec in enumerate(configured):
        where = f"LLMHUB_EXTERNAL_PURPOSES[{i}]"
        if not isinstance(spec, dict):
            raise ImproperlyConfigured(f"{where} must be a dict.")
        try:
            row = ExternalPurpose(**spec)
        except TypeError as exc:
            raise ImproperlyConfigured(f"{where} is not a valid external purpose: {exc}") from exc
        for field in ("name", "label", "route", "owner"):
            if not getattr(row, field):
                raise ImproperlyConfigured(f"{where} is missing a non-empty {field}.")
        rows.append(row)
    return rows
