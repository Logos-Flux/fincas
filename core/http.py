"""Request helpers shared by the back-office views."""

from __future__ import annotations

from django.utils.http import url_has_allowed_host_and_scheme


def safe_next(request, default):
    """``?next=`` (POST first) when it is on this host, else ``default``.

    One check for every ``next`` the back office honours (CRM and party
    forms); the OIDC flow keeps its own with a different default.
    """
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                                   require_https=request.is_secure()):
        return target
    return default
