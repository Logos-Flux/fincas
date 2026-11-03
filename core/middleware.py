"""
ERP middleware.
"""

from django.shortcuts import redirect


class APIKeyMiddleware:
    """Authenticate non-DRF ``/api/`` requests with an Api-Key header.

    Expects: Authorization: Api-Key <key> (or the legacy `X-API-Key`, SEC-109
    back-compat for one release). Resolution is the same
    ``core.service_accounts.resolve_api_key`` that ``APIKeyAuthentication``
    uses: a ``gk_`` key sets ``request.user`` to its ``svc-*`` service
    account, the legacy ``API_KEY`` to the dedicated ``api-service`` account
    (never "the first superuser"). A revoked, expired or unknown key leaves
    the request anonymous; DRF views then answer 401 through the class.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/api/") and not request.user.is_authenticated:
            from .authentication import _stash_scope, extract_api_key
            from .service_accounts import ApiKeyRejected, resolve_api_key

            token = extract_api_key(request)
            if token is not None:
                try:
                    resolved = resolve_api_key(token, request)
                except ApiKeyRejected:
                    resolved = None
                # Re-used by APIKeyAuthentication for the same request, so a
                # key is resolved (and the legacy key logged) once.
                request._golden_api_key_resolution = (token, resolved)
                if resolved is not None:
                    request.user = resolved.user
                    _stash_scope(request, resolved.scope)

        return self.get_response(request)


class ForcePasswordChangeMiddleware:
    """Redirect users with must_change_password to the change-password page."""

    EXEMPT_PATHS = (
        "/erp/login/",
        "/erp/logout/",
        "/erp/account/change-password/",
        "/erp/verify-email/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.path.startswith("/erp/")
            and request.user.is_authenticated
            and not any(request.path.startswith(p) for p in self.EXEMPT_PATHS)
            and getattr(
                getattr(request.user, "erp_settings", None),
                "must_change_password",
                False,
            )
        ):
            return redirect("erp-account-change-password")

        return self.get_response(request)


class StaffOnlyERPMiddleware:
    """`/erp/` is the staff back office; a signed-in non-staff account gets 403.

    `ERPViewMixin` is only `LoginRequiredMixin`, so without this any
    authenticated account (a portal customer with a Django session, a future
    tenant user) could browse the whole back office. Anonymous requests are
    left to the views, which redirect to the login page. The login, logout and
    password-reset routes stay open so a non-staff account can still sign out.
    Plugin URLs are staff-gated separately by the plugin engine; this covers
    every core route under the prefix, including bare `LoginRequiredMixin`
    views. The 403 matches what `UserPassesTestMixin` views already return.
    """

    PREFIX = "/erp/"
    EXEMPT_PREFIXES = (
        "/erp/login/",
        "/erp/logout/",
        "/erp/password-reset/",
        "/erp/oidc/",  # OIDC sign-in start/callback (core.oidc_views)
        "/erp/verify-email/",  # signed-token email verification, public (core.erp_urls)
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if path.startswith(self.PREFIX) and not path.startswith(self.EXEMPT_PREFIXES):
            user = request.user
            # Same answer as the staff API (core.permissions): superuser, or
            # an active account holding a staff role AND the derived flag. A
            # flag without a role (a renamed group, a row written around the
            # ORM) is refused here too instead of waiting for the next save.
            from .permissions import is_staff_principal

            if user.is_authenticated and not is_staff_principal(user):
                # The styled page directly (not PermissionDenied): it holds
                # even when a downstream URLconf sets no handler403.
                from .error_views import forbidden

                return forbidden(request, "A staff account is required for the back office.")
        return self.get_response(request)


class DemoGuardMiddleware:
    """Keep a publicly advertised demo login from doing harm.

    Applies only to usernames in ``settings.DEMO_USERNAMES`` (empty by default,
    so it is inert everywhere except a public demo). For those users:

    * ``/admin/`` and the LLM settings pages are off-limits entirely (the LLM
      connection form and webhook pings make the server call a URL of the
      caller's choosing);
    * writes to ``/api/`` and to account/user-admin/email-sending views are
      refused with a notice, so the account can't lock others out, change its
      own password, send mail, or register webhooks;
    * the chat assistant is read-only: its tools run through the REST API as
      the API-key superuser (they can send email), and every message spends
      LLM credit;
    * CMS articles are read-only (their HTML renders unescaped), and the
      storefront staff SSO hand-off is blocked.

    Outbound mail is handled in settings, not here: prod.py swaps in the
    console email backend whenever DEMO_USERNAMES is set, because order
    confirmations fire from ordinary order edits.

    Everything else — creating orders, quotes, products — stays usable; the
    staging box restores a database snapshot nightly.
    """

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
    BLOCKED_PATHS = ("/admin/", "/erp/llm/")
    # Blocked for every method — the storefront SSO hand-off is a GET.
    BLOCKED_URL_NAMES = {"erp-sso-storefront"}
    # Any write under these prefixes is refused (reads stay allowed). CMS
    # articles render their HTML unescaped (content/detail.html), so letting
    # a public login author them is stored XSS against whoever reads them.
    READONLY_PATHS = ("/api/", "/erp/chat/", "/erp/content/")
    READONLY_URL_NAMES = {
        "erp-account-edit",
        "erp-account-change-password",
        "erp-user-create",
        "erp-user-toggle-active",
        "erp-user-roles",
        "erp-settings-labels",
        "erp-accounting-import",
        # Runs a provider ping with this server's real keys and writes a row.
        "erp-integration-test",
        # Outbound webhooks: a public demo login must not point them anywhere.
        "erp-webhook-subscription-create",
        "erp-webhook-subscription-toggle",
        "erp-webhook-subscription-ping",
    }
    MESSAGE = "That action is turned off in the public demo."

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings

        user = getattr(request, "user", None)
        if not (user and user.is_authenticated and user.username in settings.DEMO_USERNAMES):
            return self.get_response(request)

        path = request.path_info
        name = self._url_name(path)
        if path.startswith(self.BLOCKED_PATHS) or name in self.BLOCKED_URL_NAMES:
            return self._refuse(request)
        if request.method in self.SAFE_METHODS:
            return self.get_response(request)
        if path.startswith(self.READONLY_PATHS):
            from django.http import JsonResponse

            return JsonResponse({"detail": self.MESSAGE}, status=403)
        if name in self.READONLY_URL_NAMES or (name and "send" in name):
            return self._refuse(request)
        return self.get_response(request)

    @staticmethod
    def _url_name(path):
        from django.urls import Resolver404, resolve

        try:
            return resolve(path).url_name
        except Resolver404:
            return None

    def _refuse(self, request):
        from django.contrib import messages
        from django.utils.http import url_has_allowed_host_and_scheme

        if request.method in self.SAFE_METHODS and not request.headers.get("referer"):
            from .error_views import forbidden

            return forbidden(request, self.MESSAGE)
        messages.warning(request, self.MESSAGE)
        from urllib.parse import urlsplit

        back = request.headers.get("referer", "")
        # Never bounce back onto a refused page: that would redirect forever.
        if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}) or urlsplit(
            back
        ).path.startswith(self.BLOCKED_PATHS):
            back = "/erp/"
        return redirect(back)
