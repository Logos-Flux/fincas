"""Shared validation for the parties settings registries."""

from __future__ import annotations

import re
from typing import Any

from django.core.exceptions import ImproperlyConfigured

from .constants import PARTY_KINDS

# PartyRole.role and PartyRelationship.kind are CharField(32).
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def check_key(where: str, value: Any) -> None:
    if not isinstance(value, str) or not KEY_RE.fullmatch(value):
        raise ImproperlyConfigured(
            f"{where} has name={value!r}; use a lower-case key "
            "([a-z][a-z0-9_]*, at most 32 characters)."
        )


def check_label(where: str, field: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ImproperlyConfigured(f"{where} is missing a non-empty {field}.")


def party_kinds(where: str, field: str, value: Any) -> tuple[str, ...]:
    """A non-empty list of party kinds, returned as a tuple in canonical order."""
    if isinstance(value, str) or not isinstance(value, (list, tuple, frozenset, set)):
        raise ImproperlyConfigured(
            f"{where} {field} must be a list of party kinds ({', '.join(PARTY_KINDS)})."
        )
    unknown = [k for k in value if k not in PARTY_KINDS]
    if unknown or not value:
        raise ImproperlyConfigured(
            f"{where} {field}={list(value)!r}; must be a non-empty list drawn from "
            f"{', '.join(PARTY_KINDS)}."
        )
    if len(set(value)) != len(value):
        raise ImproperlyConfigured(f"{where} {field} lists a party kind twice.")
    return tuple(k for k in PARTY_KINDS if k in value)


def configured_list(setting_name: str, configured: Any) -> list | tuple:
    configured = configured or []
    if not isinstance(configured, (list, tuple)):
        raise ImproperlyConfigured(
            f"{setting_name} must be a list of dicts, got {type(configured).__name__}."
        )
    return configured
