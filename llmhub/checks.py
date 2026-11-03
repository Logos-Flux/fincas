"""System checks for llmhub configuration."""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Warning, register

from . import crypto


@register()
def fernet_key_check(app_configs, **kwargs):
    if not crypto.key_is_derived():
        return []
    return [
        Warning(
            "LLMHUB_FERNET_KEY unset, deriving from SECRET_KEY — rotation will brick the stored token.",
            hint=(
                "Set LLMHUB_FERNET_KEY to a dedicated key (Fernet.generate_key()). Until then, a "
                "SECRET_KEY rotation makes the DB-stored gateway token undecryptable and it must be "
                "re-entered on /erp/llm/."
            ),
            id="llmhub.W001",
        )
    ]


@register()
def allowlist_check(app_configs, **kwargs):
    base = (getattr(settings, "LLMHUB_GATEWAY_BASE_URL", "") or "").rstrip("/")
    allowed = [u.rstrip("/") for u in (getattr(settings, "LLMHUB_ALLOWED_BASE_URLS", []) or [])]
    if base and allowed and base not in allowed:
        return [
            Warning(
                f"LLMHUB_GATEWAY_BASE_URL ({base}) is not in LLMHUB_ALLOWED_BASE_URLS.",
                hint="An admin could not save the env default from the settings page. Add it to the allowlist.",
                id="llmhub.W002",
            )
        ]
    return []
