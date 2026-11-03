"""Styled 403, 404 and 500 pages (release-hardening-plan.md 9.7).

``golden_erp.urls`` names these as ``handler403`` / ``handler404`` /
``handler500``; ``StaffOnlyERPMiddleware`` and ``DemoGuardMiddleware`` return
:func:`forbidden` directly, so the back office never shows Django's bare
"Forbidden" even when a downstream URLconf sets no handlers.

The pages extend ``registration/auth_base.html`` (the sign-in shell): static
CSS only, no context processors, no database. That matters for the 500 page,
which must render while the database or a middleware is the thing that is
broken, so every template here is rendered with ``request=None`` and an
explicit context. Under ``/api/`` the same handlers answer JSON, so an API
client never receives an HTML page.
"""

from __future__ import annotations

from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.template import loader

API_PREFIX = "/api/"

FORBIDDEN_TEXT = "You do not have permission to open this page."
NOT_FOUND_TEXT = "There is no page at this address."
SERVER_ERROR_TEXT = "Something went wrong on the server. The error has been logged."


def _wants_json(request) -> bool:
    return request.path.startswith(API_PREFIX)


def _render(request, template_name: str, status: int, **context) -> HttpResponse:
    try:
        # Lets the sign-out form on the page post with a valid token. The
        # cookie is set by CsrfViewMiddleware's process_response on the way
        # out; if that middleware is absent the form still renders.
        csrf_token = get_token(request)
    except Exception:  # never let the error page itself fail
        csrf_token = ""
    html = loader.render_to_string(template_name, {
        "request_path": request.path,
        "csrf_token": csrf_token,
        **context,
    })
    return HttpResponse(html, status=status)


def forbidden(request, reason: str = "") -> HttpResponse:
    """The styled 403 for a request, with an optional one-line ``reason``."""
    if _wants_json(request):
        return JsonResponse({"detail": reason or FORBIDDEN_TEXT}, status=403)
    return _render(request, "403.html", 403, reason=reason)


def permission_denied(request, exception=None, template_name="403.html"):
    """``handler403``."""
    reason = str(exception) if exception is not None else ""
    return forbidden(request, reason)


def page_not_found(request, exception=None, template_name="404.html"):
    """``handler404``."""
    if _wants_json(request):
        return JsonResponse({"detail": NOT_FOUND_TEXT}, status=404)
    return _render(request, template_name, 404)


def server_error(request, template_name="500.html"):
    """``handler500``: no database, no middleware state, no context processors."""
    if _wants_json(request):
        return JsonResponse({"detail": SERVER_ERROR_TEXT}, status=500)
    return _render(request, template_name, 500)
