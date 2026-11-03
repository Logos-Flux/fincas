"""OIDC session login for ``/erp/`` (identity-unification §2.5, step 6).

``/erp/oidc/<provider>/start/`` records state, nonce and a PKCE verifier in
the session and redirects to the issuer. ``/erp/oidc/<provider>/callback/``
checks the state, exchanges the code, verifies the ID token and its nonce,
resolves the staff user (``core.identity.oidc.resolve_erp_login``) and
starts a Django session. Failures re-render the login page with a message;
details go to the log only.

Both routes are exempt from ``StaffOnlyERPMiddleware`` (like the login page).
``start`` is throttled per client IP: each call stores a pending flow in the
session and sends the browser to the issuer.
"""

from __future__ import annotations

import logging

from django.contrib.auth import login
from django.core.cache import cache
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from . import cache_guard
from .client_ip import client_ip
from .identity.oidc import OIDC, LoginRefused, OIDCError, complete_login, take_flow
from .identity.providers import get_provider
from .throttling import ThrottledLoginView

logger = logging.getLogger(__name__)

START_ATTEMPTS_PER_MINUTE = 20
_START_WINDOW_S = 60
DEFAULT_NEXT = "/erp/"
# The session login uses Django's ModelBackend to restore the user on later
# requests (it checks is_active); the OIDC provider only vouches for sign-in.
SESSION_BACKEND = "django.contrib.auth.backends.ModelBackend"


def erp_provider(name: str) -> OIDC:
    """The configured OIDC provider ``name`` serving ``erp``, or 404."""
    provider = get_provider(name)
    if (not isinstance(provider, OIDC) or "erp" not in provider.config.entry_points
            or not provider.config.trusted):
        raise Http404("No such sign-in provider")
    return provider


def erp_login_providers() -> list[dict]:
    """Buttons for the login page: every OIDC provider with the erp entry point."""
    from .identity.providers import PROVIDERS

    return [
        {"name": p.config.name, "label": p.label}
        for p in PROVIDERS.values()
        if isinstance(p, OIDC) and "erp" in p.config.entry_points and p.config.trusted
    ]


def _safe_next(request, candidate) -> str:
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return candidate
    return DEFAULT_NEXT


def _throttled(request) -> bool:
    """Raises ``cache_guard.CacheUnavailable`` when the counter cannot be read
    (F-086) unless ``GOLDEN_CACHE_DOWN_FAIL_OPEN`` lets attempts through."""
    key = f"oidc-start-throttle:{client_ip(request)}"
    if cache_guard.is_down():
        if cache_guard.fail_open():
            return False
        raise cache_guard.CacheUnavailable
    try:
        if cache.add(key, 1, _START_WINDOW_S):  # atomic: first call in the window
            cache_guard.ok()
            return False
        try:
            count = cache.incr(key)
        except ValueError:  # expired between add and incr
            cache.add(key, 1, _START_WINDOW_S)
            cache_guard.ok()
            return False
        if count == 1:  # recreated by INCR with no TTL (see ThrottledLoginView)
            cache.touch(key, _START_WINDOW_S)
    except Exception as exc:  # noqa: BLE001 - the cache backend's own error
        cache_guard.failed("OIDC start throttle: cache unavailable; refusing the attempt", logger)
        if cache_guard.fail_open():
            return False
        raise cache_guard.CacheUnavailable from exc
    cache_guard.ok()
    return count > START_ATTEMPTS_PER_MINUTE


def _refuse(request, message: str, status: int = 403):
    """The login page again, with ``message``; never a redirect loop."""
    response = ThrottledLoginView.as_view(
        template_name="registration/login.html", extra_context={"oidc_error": message},
    )(request)
    response.status_code = status
    return response


def _redirect_uri(request, provider: OIDC) -> str:
    configured = provider.option("redirect_uri")
    if configured:
        return str(configured)
    return request.build_absolute_uri(reverse("erp-oidc-callback", args=[provider.config.name]))


@never_cache
@require_GET
def oidc_start(request, provider_name: str):
    provider = erp_provider(provider_name)
    try:
        throttled = _throttled(request)
    except cache_guard.CacheUnavailable:
        # The login view's own 503: unbound form, no demo-hint query, Retry-After.
        view = ThrottledLoginView(template_name="registration/login.html")
        view.setup(request)
        return view._cache_down_response(request)
    if throttled:
        return HttpResponse("Too many sign-in attempts. Try again later.", status=429)
    next_url = _safe_next(request, request.GET.get("next"))
    try:
        url = provider.begin(request.session, _redirect_uri(request, provider), next_url)
    except OIDCError as exc:
        logger.warning("OIDC: %s", exc)
        return _refuse(request, "The sign-in provider could not be reached. Try again.", 502)
    return HttpResponseRedirect(url)


@never_cache
@require_GET
def oidc_callback(request, provider_name: str):
    provider = erp_provider(provider_name)
    state = request.GET.get("state", "")
    try:
        flow = take_flow(request.session, provider.config.name, state)
    except LoginRefused as exc:
        return _refuse(request, str(exc), 400)
    if request.GET.get("error"):
        logger.info("OIDC: %s returned error %r", provider.config.name, request.GET["error"])
        return _refuse(request, "Sign-in was canceled or refused by the provider.", 403)
    code = request.GET.get("code", "")
    if not code:
        return _refuse(request, "The sign-in provider sent no authorization code.", 400)
    try:
        user = complete_login(provider, flow, code)
    except LoginRefused as exc:
        return _refuse(request, str(exc), 403)
    login(request, user, backend=SESSION_BACKEND)  # cycles the session key
    return HttpResponseRedirect(_safe_next(request, flow.get("next")))
