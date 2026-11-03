"""Audit trail writes for LLM settings changes.

Two writers, deliberately:

1. **Views** write POST-shaped actions (``key_rotated``, ``lane_changed``, …)
   through :func:`record`, inside ``transaction.on_commit`` — a rolled-back
   save never leaves a phantom row, and a create is distinguishable from a
   rotation. Views wrap their saves in :func:`view_write` so the backstop
   below stays quiet.
2. The **signal backstop** (``signals.py``) emits ``field_changed`` rows for
   writes that bypass the views (Django admin, shell, fixtures). It can say
   what changed, never why.

The actor comes from a contextvar set and *reset* around each request by
``LLMHubActorMiddleware`` — a contextvar, not a thread-local, because under
ASGI coroutines share threads, and an unreset value would stamp one admin's
identity onto another's save. Shell writes have no request: actor is null.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from django.db import transaction

_actor: ContextVar[Any] = ContextVar("llmhub_actor", default=None)
_view_write: ContextVar[bool] = ContextVar("llmhub_view_write", default=False)


def current_actor():
    user = _actor.get()
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return user


def set_actor(user):
    return _actor.set(user)


def reset_actor(token) -> None:
    _actor.reset(token)


def in_view_write() -> bool:
    return _view_write.get()


@contextmanager
def view_write() -> Iterator[None]:
    """Mark the enclosed saves as view-authored (the view records the audit row)."""
    token = _view_write.set(True)
    try:
        yield
    finally:
        _view_write.reset(token)


def mask(value: Any, *, secret: bool = False) -> str:
    """Render a value for the audit table. Secrets never appear, even partially."""
    if value in (None, ""):
        return "(empty)"
    if secret:
        return "(set)"
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= 200 else text[:197] + "…"


def record(
    action: str,
    *,
    actor=None,
    field: str = "",
    old: Any = None,
    new: Any = None,
    detail: str = "",
    secret: bool = False,
    old_masked: str | None = None,
    new_masked: str | None = None,
) -> None:
    """Queue one audit row for when the surrounding transaction commits."""
    from .models import LLMSettingsAudit

    user = actor if actor is not None else current_actor()
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    row = dict(
        actor=user,
        action=action,
        field=field,
        old_masked=old_masked if old_masked is not None else ("" if old is None and not secret else mask(old, secret=secret)),
        new_masked=new_masked if new_masked is not None else ("" if new is None and not secret else mask(new, secret=secret)),
        detail=detail[:300],
    )
    transaction.on_commit(lambda: LLMSettingsAudit.objects.create(**row))
