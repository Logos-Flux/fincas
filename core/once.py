"""Refuse a repeated form submission (F-033, double submit).

A back-office form that moves money or sends mail carries a hidden
``_once`` token (``{% once_field %}`` in ``erp_tags``), minted on every
render. The view claims the token in the shared cache when it accepts the
POST; the same token posted again within ``ONCE_TTL`` seconds is a repeat
(a double click, a retried request, a second tab's resubmit) and is
answered with "already sent" and a redirect, creating nothing. A fresh
render mints a fresh token, so a deliberate second send still works.

The browser-side guard (``includes/submit_guard.html``) disables the
submit control while a request is in flight; this is the backstop for
anything that guard cannot see. A POST without a token is not guarded:
API and script callers keep their own idempotency keys.
"""

from __future__ import annotations

import re
import uuid

from . import cache_guard

ONCE_FIELD = "_once"
ONCE_TTL = 60
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}")


def mint() -> str:
    return uuid.uuid4().hex


def token_from(data) -> str:
    """The submission's token from a POST QueryDict or a JSON body, or ``""``."""
    token = str((data.get(ONCE_FIELD) if hasattr(data, "get") else None) or "").strip()
    return token if _TOKEN.fullmatch(token) else ""


def _key(request, action, obj, token):
    user_id = getattr(getattr(request, "user", None), "pk", None) or "anon"
    return f"once:{user_id}:{obj._meta.label_lower}:{obj.pk}:{action}:{token}"


def claim(request, action, obj, token=None) -> bool:
    """True when this submission is the first with its token: do the work.

    False when the same (user, object, action, token) was accepted within
    ``ONCE_TTL`` seconds: answer "already done" and change nothing.
    ``token`` defaults to ``request.POST["_once"]``; a JSON view passes the
    body's token itself.
    """
    if token is None:
        token = token_from(request.POST)
    if not token:
        return True
    # The key is built outside the guard: a bad ``obj`` is a bug, not an outage.
    key = _key(request, action, obj, token)
    # With the cache down (F-086) the claim is remembered in this process
    # only: a repeat on this worker is still refused, one on another worker
    # is not. Degraded and logged, never blind (a refusal would block every
    # send and payment for the outage).
    return cache_guard.cache_add(key, 1, ONCE_TTL, what="once guard")


def release(request, action, obj, token=None) -> None:
    """Forget a claim whose work failed, so the same form can be retried."""
    if token is None:
        token = token_from(request.POST)
    if token:
        cache_guard.cache_delete(_key(request, action, obj, token), what="once guard")
