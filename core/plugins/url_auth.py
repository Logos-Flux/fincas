"""Default auth wrapper for plugin-contributed URLs.

Plugin URLs are staff-only unless the plugin class declares
``url_auth = "public"``. The wrapper mirrors what ``/erp/`` does for an
anonymous visitor (``ERPViewMixin`` → ``LoginRequiredMixin``: redirect to
``/erp/login/?next=<path>``) and, stricter than ``/erp/``, answers 403 to a
signed-in non-staff user — a plugin view that forgets its own checks must not
expose ERP data to a storefront or portal account.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, List

from asgiref.sync import sync_to_async
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.urls import URLPattern, URLResolver

#: Values a plugin may set on ``BasePlugin.url_auth``.
URL_AUTH_STAFF = "staff"
URL_AUTH_PUBLIC = "public"
URL_AUTH_CHOICES = (URL_AUTH_STAFF, URL_AUTH_PUBLIC)


def _login_url() -> str:
    from core.views.mixins import ERPViewMixin

    return ERPViewMixin.login_url


def _is_staff(user) -> bool:
    # The one staff answer (superuser, or active + staff role + is_staff).
    from core.permissions import is_staff_principal

    return is_staff_principal(user)


def _deny(request, user, allowed: bool):
    """None when ``user`` may pass, else the response/exception /erp/ gives."""
    if not user.is_authenticated:
        return redirect_to_login(request.get_full_path(), _login_url())
    if not allowed:
        raise PermissionDenied
    return None


def staff_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap one view callable in the plugin staff gate (sync or async)."""
    if inspect.iscoroutinefunction(view):

        @functools.wraps(view)
        async def async_wrapped(request, *args, **kwargs):
            user = await request.auser()
            denied = _deny(request, user, await sync_to_async(_is_staff)(user))
            if denied is not None:
                return denied
            return await view(request, *args, **kwargs)

        return async_wrapped

    @functools.wraps(view)
    def wrapped(request, *args, **kwargs):
        denied = _deny(request, request.user, _is_staff(request.user))
        if denied is not None:
            return denied
        return view(request, *args, **kwargs)

    return wrapped


def require_staff(patterns: List[Any]) -> List[Any]:
    """Return copies of ``patterns`` with every leaf view staff-gated,
    descending through ``include()`` resolvers."""
    wrapped: List[Any] = []
    for entry in patterns:
        if isinstance(entry, URLPattern):
            wrapped.append(
                URLPattern(
                    entry.pattern,
                    staff_required(entry.callback),
                    entry.default_args,
                    entry.name,
                )
            )
        elif isinstance(entry, URLResolver):
            wrapped.append(
                URLResolver(
                    entry.pattern,
                    require_staff(list(entry.url_patterns)),
                    entry.default_kwargs,
                    entry.app_name,
                    entry.namespace,
                )
            )
        else:
            raise TypeError(f"Not a URL pattern: {entry!r}")
    return wrapped
