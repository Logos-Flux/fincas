"""Admin → Integrations: the external-service inventory and its connection tests.

Read side renders ``core.integration_registry`` (rows, credential presence as
bools, last persisted check per test). ``IntegrationTestView`` is a staff POST
that runs one registered connection test and writes one append-only
``core.IntegrationCheck`` row. Server-level keys stay deployment-managed.

The **Connected accounts** section (docs/crm/phase6-adapters.md 2.7) lists
the caller's own company's provider accounts only; its write views live in
``core.integrations.views``. A credential appears as its kind, subject and
dates, never a value.
"""

from __future__ import annotations

import json

from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from core import integration_checks, integration_registry

from .account import StaffRequiredMixin
from .mixins import ERPViewMixin


def check_anchor(target: str) -> str:
    return "check-" + target.replace(":", "-").replace("_", "-")


class IntegrationsView(ERPViewMixin, StaffRequiredMixin, TemplateView):
    template_name = "erp/integrations.html"
    current_section = "integrations"
    page_title = "Integrations"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        tests = integration_registry.connection_tests()
        last = integration_checks.last_results()
        rows = integration_registry.rows()
        for row in rows:
            row["tests"] = [
                {
                    "name": t,
                    "label": tests[t].label if t in tests else t,
                    "registered": t in tests,
                    "anchor": check_anchor(t),
                    "last": last.get(t),
                }
                for t in row["connection_tests"]
            ]
        ctx.update(connected_accounts_context(self.request))
        from webhooks.erp_views import subscriptions_context

        ctx.update(subscriptions_context(self.request))
        ctx.update(
            rows=rows,
            summary=integration_registry.summary(),
            statuses=integration_registry.STATUSES,
            key_rotation=integration_registry.KEY_ROTATION,
            local_app=integration_registry.VOCABULARY["local_app"],
        )
        return ctx


class IntegrationTestView(StaffRequiredMixin, View):
    """POST: run one connection test. JSON callers get the result; forms redirect back."""

    login_url = "/erp/login/"
    http_method_names = ["post"]

    def post(self, request, target):
        if target not in integration_registry.available_targets():
            return JsonResponse(
                {"ok": False, "error_class": "integration_test_unknown",
                 "what": f"No connection test named {target[:64]!r}."},
                status=404,
            )
        result = integration_checks.run_check(target, user=request.user)
        wants_json = (request.headers.get("Accept", "").startswith("application/json")
                      or request.content_type == "application/json")
        if wants_json:
            return JsonResponse(result, status=200)
        return redirect(reverse("erp-integrations") + "#" + check_anchor(target))


def connected_accounts_context(request) -> dict:
    """The Connected accounts section: this tenant's accounts, never another's."""
    from core.integrations.accounts import installed_adapters
    from core.models import IntegrationAccount, IntegrationCredential
    from core.views import request_company

    company = request_company(request)
    adapters = installed_adapters()
    if company is None:
        return {"connected": {"company": None, "accounts": [], "available": [], "can_change": False}}
    accounts = list(IntegrationAccount.objects.filter(company=company).order_by("provider"))
    last = integration_checks.last_account_results(accounts)
    profile = getattr(request.user, "userprofile", None)
    creds = IntegrationCredential.objects.filter(account__in=accounts, revoked_at__isnull=True)
    by_account: dict = {}
    for cred in creds.select_related("owner"):
        by_account.setdefault(cred.account_id, []).append(cred)
    out = []
    for account in accounts:
        cls = adapters.get(account.provider)
        rows = by_account.get(account.pk, [])
        out.append({
            "account": account,
            "installed": cls is not None,
            "capabilities": sorted(c.value for c in (cls.capabilities if cls else ())),
            "oauth": bool(cls and "oauth_user" in {c.value for c in cls.capabilities}),
            "credential_kinds": sorted(k for k in (cls.credential_kinds if cls else ())
                                       if k not in ("oauth_refresh", "oauth_access")),
            "company_credentials": [c for c in rows if c.owner_id is None],
            "grants": [c for c in rows if c.owner_id is not None and c.kind == "oauth_refresh"],
            "my_grant": next((c for c in rows if profile is not None and c.owner_id == profile.pk
                              and c.kind == "oauth_refresh"), None),
            "settings_json": json.dumps(account.settings, indent=2, sort_keys=True),
            "webhook_path": reverse("integration-webhook", args=[account.provider, account.pk]),
            "last_check": last.get(str(account.pk)),
            "paused": account.is_paused(),
        })
    taken = {a.provider for a in accounts}
    return {"connected": {
        "company": company, "accounts": out,
        "available": sorted(p for p in adapters if p not in taken),
        "can_change": request.user.has_perm("core.change_integrationaccount"),
    }}
