"""Per-endpoint throttles (F-046/047).

Deliberately NOT global defaults: DEFAULT_THROTTLE_CLASSES / RATES would
wrap every viewset and threaten the perf query-count suites. Each throttle
below is attached to exactly one endpoint via ``throttle_classes`` (DRF)
or subclassing (the Django login view).
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework.throttling import SimpleRateThrottle

from . import cache_guard
from .client_ip import client_ip

LOGIN_ATTEMPTS_PER_MINUTE = 20
_LOGIN_WINDOW_S = 60
CACHE_DOWN_MESSAGE = "Sign-in is not available right now. Try again in a moment."
_log = logging.getLogger(__name__)


def _login_throttle_key(request):
    # The client past the deployment's own proxies (F-087): keyed on
    # REMOTE_ADDR, every visitor behind Caddy shared one bucket per username.
    ip = client_ip(request)
    username = (request.POST.get("username", "") or "").strip().lower()
    return f"login-throttle:{ip}:{username}"


@method_decorator([sensitive_post_parameters(), csrf_protect, never_cache], name="dispatch")
class ThrottledLoginView(LoginView):
    """Stock LoginView with a 20/min per-IP+username throttle.

    Only POSTs count — rendering the login form never consumes budget.
    Over-limit attempts get a plain 429 (never a login re-render). With the
    cache unreachable (F-086) nobody signs in: the form comes back with its
    input and a try-again message (503), never a 500 and never an uncounted
    attempt. The decorators are the stock LoginView's, re-applied so they
    also cover that early return.
    """

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST":
            key = _login_throttle_key(request)
            if cache_guard.is_down():
                # Breaker open: never touch the backend. Refuse, or (escape
                # hatch) let the attempt through uncounted.
                if not cache_guard.fail_open():
                    return self._cache_down_response(request)
            else:
                try:
                    count = self._count_attempt(key)
                except Exception:  # noqa: BLE001 - the cache backend's own error
                    cache_guard.failed("login throttle: cache unavailable; refusing the attempt", _log)
                    if not cache_guard.fail_open():
                        return self._cache_down_response(request)
                else:
                    cache_guard.ok()  # before any refusal: a 429 must not orphan the probe
                    if count > LOGIN_ATTEMPTS_PER_MINUTE:
                        return HttpResponse(
                            "Too many login attempts. Try again later.", status=429
                        )
        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def _count_attempt(key) -> int:
        """This attempt's position in the window: add + incr, atomic, so a
        burst of concurrent POSTs cannot all read "no attempts yet"."""
        if cache.add(key, 1, _LOGIN_WINDOW_S):
            return 1
        try:
            count = cache.incr(key)
        except ValueError:  # expired between add and incr: first again
            cache.add(key, 1, _LOGIN_WINDOW_S)
            return 1
        if count == 1:
            # Django's Redis incr is EXISTS then INCR: a key that expired
            # between them is recreated with no TTL, which would lock the
            # bucket for good. Give it its window back.
            cache.touch(key, _LOGIN_WINDOW_S)
        return count

    def _cache_down_response(self, request):
        # An UNBOUND form carrying the typed username as initial: there is
        # nothing to clean, so authenticate() can never run uncounted (a
        # bound form would call it the moment a template reads its errors).
        # LoginView's own context, not this class's: no demo-hint query and
        # no provider lookup for a page an attacker can request at will.
        form = self.get_form_class()(request, initial={"username": request.POST.get("username", "")})
        context = super(ThrottledLoginView, self).get_context_data(form=form)
        context.update(cache_down_message=CACHE_DOWN_MESSAGE, show_demo_login_hint=False, oidc_providers=[])
        response = self.render_to_response(context)
        response.status_code = 503
        response["Retry-After"] = str(_LOGIN_WINDOW_S)
        return response

    def get_context_data(self, **kwargs):
        # Read per-request (not extra_context at import) so tests and
        # setting changes apply without reimporting the URLconf.
        context = super().get_context_data(**kwargs)
        # The hint advertises the demo account only — never a superuser — and
        # stays hidden unless both the flag and the demo credentials are set.
        username = settings.DEMO_LOGIN_USERNAME
        show = (
            settings.SHOW_DEMO_LOGIN_HINT
            and bool(username and settings.DEMO_LOGIN_PASSWORD)
            and username in settings.DEMO_USERNAMES
            and not get_user_model().objects.filter(username=username, is_superuser=True).exists()
        )
        context["show_demo_login_hint"] = show
        # One "Sign in with <label>" button per trusted OIDC provider serving
        # /erp/ (identity step 6, core.oidc_views).
        from .oidc_views import erp_login_providers

        context["oidc_providers"] = erp_login_providers()
        if show:
            context["demo_login_username"] = settings.DEMO_LOGIN_USERNAME
            context["demo_login_password"] = settings.DEMO_LOGIN_PASSWORD
        return context


class CacheTolerantThrottle(SimpleRateThrottle):
    """A DRF rate throttle whose counter lives in the cache (F-086).

    ``SimpleRateThrottle.allow_request`` reads and writes the cache with no
    guard, so a cache outage 500s every throttled endpoint. With the cache
    unreachable the throttle cannot count, so it refuses: 429 with
    ``Retry-After`` set to the whole window, instead of letting attempts
    through uncounted (the login view's stance; ``GOLDEN_CACHE_DOWN_FAIL_OPEN``
    flips both). While ``core.cache_guard``'s breaker is open the backend is
    not even tried, and the outage is logged by the guard, not per request.
    """

    def allow_request(self, request, view):
        if self.rate is None:
            return True
        # Outside the guard: a bug in key derivation must surface as itself,
        # never read as a cache outage.
        self.key = self.get_cache_key(request, view)
        if self.key is None:
            return True
        if cache_guard.is_down():
            return self._refuse_or_open()
        try:
            allowed = super().allow_request(request, view)
        except Exception:  # noqa: BLE001 - the cache backend's own error
            cache_guard.failed(f"{type(self).__name__}: cache unavailable; refusing the request", _log)
            return self._refuse_or_open()
        cache_guard.ok()
        return allowed

    def _refuse_or_open(self):
        if cache_guard.fail_open():
            return True
        # wait() reads these: a full history dated now makes it the window.
        self.now = self.timer()
        self.history = [self.now] * self.num_requests
        return False


class TokenObtainThrottle(CacheTolerantThrottle):
    """30/min per IP on the Api-Key token-obtain endpoint."""

    scope = "token_obtain"
    rate = "30/minute"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class PortalInviteResendThrottle(CacheTolerantThrottle):
    """10/hour per admin on the portal-invite resend (email-sending)."""

    scope = "portal_invite_resend"
    rate = "10/hour"

    def get_cache_key(self, request, view):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            ident = f"user-{user.pk}"
        else:
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class PortalSignupThrottle(CacheTolerantThrottle):
    """Per IP on portal self-registration (the resolver's create branch only).

    Each new provider subject with a confirmed address creates a User and a
    Customer, so the create path is rate-limited on top of the provider's own
    sign-up limits (identity spec §7). ``GOLDEN_PORTAL_SIGNUP_RATE``, default
    ``"20/hour"``; ``None`` disables it.
    """

    scope = "portal_signup"

    def get_rate(self):
        from django.conf import settings

        return getattr(settings, "GOLDEN_PORTAL_SIGNUP_RATE", "20/hour")

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


def is_service_request(request) -> bool:
    """True when the caller is a service identity rather than a person.

    Api-Key and bearer requests authenticate as a service account
    (``api-service``, ``svc-*`` or the storefront identity); the storefront
    user also counts however it signed in. Every shopper behind a storefront shares one of
    these, so a per-user key would put them all in one bucket.
    """
    from .authentication import APIKeyAuthentication, BearerTokenAuthentication
    from .storefront import is_storefront_user

    authenticator = getattr(request, "successful_authenticator", None)
    if isinstance(authenticator, (APIKeyAuthentication, BearerTokenAuthentication)):
        return True
    return is_storefront_user(getattr(request, "user", None))


class CouponValidateThrottle(CacheTolerantThrottle):
    """60/min on coupon validation, per shopper.

    A signed-in person is keyed by user. Service identities (Api-Key, bearer,
    the storefront identity) and anonymous callers are keyed by client IP
    (``core.client_ip``, which follows ``X-Forwarded-For`` only through
    ``TRUSTED_PROXIES``) — a storefront calling as one service user must not
    give every shopper one shared 60/min budget.
    """

    scope = "coupon_validate"
    rate = "60/minute"

    def get_cache_key(self, request, view):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and not is_service_request(request):
            ident = f"user-{user.pk}"
        else:
            ident = f"ip-{client_ip(request)}"
        return self.cache_format % {"scope": self.scope, "ident": ident}
