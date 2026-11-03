"""Connected accounts on /erp/integrations/ (docs/crm/phase6-adapters.md 2.7).

Every view is staff-only and works inside the caller's own company
(``core.views.request_company``): an account of another tenant is a 404,
exactly like an id that does not exist. Mutations need
``core.change_integrationaccount``; a staff user connects and disconnects
their **own** OAuth grant without it.

Secrets enter through :class:`CredentialSetView` only, as a write-only
field: nothing renders a secret back, and the success message names the
kind, never the value.

OAuth (``OAUTH_USER`` adapters): ``connect/`` starts an authorization-code
flow with PKCE; the ``state`` is bound to the session, the user and the
account, single use, 10 minutes. ``callback/`` exchanges the code through
the adapter's transport and stores the grant as credentials owned by the
caller's profile, which must belong to the account's company.
``disconnect/`` revokes the grant locally at once and at the provider
through the outbox.
"""

from __future__ import annotations

import json
import logging
import secrets as _secrets
import time
import uuid

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.debug import sensitive_post_parameters

from core.integration_checks import run_account_check
from core.models import IntegrationAccount, IntegrationCredential
from core.plugins.payroll import AdapterError
from core.views import request_company
from core.views.account import StaffRequiredMixin

from . import accounts as services

logger = logging.getLogger("golden_erp.integrations")

SESSION_KEY = "golden_integration_oauth"
FLOW_TTL_S = 600
MAX_PENDING_FLOWS = 5
CHANGE_PERM = "core.change_integrationaccount"


def page_url(anchor: str = "connected-accounts") -> str:
    return reverse("erp-integrations") + "#" + anchor


def tenant_account(request, pk) -> IntegrationAccount:
    company = request_company(request)
    if company is None:
        raise Http404
    account = IntegrationAccount.objects.filter(pk=pk, company=company).first()
    if account is None:
        raise Http404
    return account


def _require_change(request) -> None:
    if not request.user.has_perm(CHANGE_PERM):
        raise PermissionDenied


def _errors(exc: ValidationError) -> str:
    if hasattr(exc, "message_dict"):
        return "; ".join(f"{k}: {' '.join(v)}" for k, v in exc.message_dict.items())
    return " ".join(exc.messages)


class _AccountPost(StaffRequiredMixin, View):
    login_url = "/erp/login/"
    http_method_names = ["post"]


class AccountCreateView(_AccountPost):
    def post(self, request):
        _require_change(request)
        company = request_company(request)
        if company is None:
            raise Http404
        provider = (request.POST.get("provider") or "").strip()
        try:
            account = services.create_account(company, provider, user=request.user)
        except ValidationError as exc:
            messages.error(request, _errors(exc))
            return redirect(page_url())
        messages.success(request, f"{provider} account created (disabled). Add its credentials, then enable it.")
        return redirect(page_url(f"account-{account.pk}"))


class AccountUpdateView(_AccountPost):
    """Settings (a JSON object), enable / disable, and resume a paused account."""

    def post(self, request, pk):
        _require_change(request)
        account = tenant_account(request, pk)
        settings = None
        if "settings" in request.POST:
            try:
                settings = json.loads(request.POST.get("settings") or "{}")
            except ValueError:
                messages.error(request, "Settings must be valid JSON.")
                return redirect(page_url(f"account-{account.pk}"))
        enabled = None
        if "enabled" in request.POST:
            enabled = request.POST.get("enabled") in ("1", "true", "on")
        try:
            services.update_account(account, settings=settings, enabled=enabled,
                                    resume=request.POST.get("resume") == "1", user=request.user)
        except ValidationError as exc:
            messages.error(request, _errors(exc))
        else:
            messages.success(request, f"{account.provider} account saved.")
        return redirect(page_url(f"account-{account.pk}"))


class CredentialSetView(_AccountPost):
    """Write-only: store one company credential encrypted. Nothing is echoed."""

    @method_decorator(sensitive_post_parameters("secret"))
    def post(self, request, pk):
        _require_change(request)
        account = tenant_account(request, pk)
        kind = (request.POST.get("kind") or "").strip()
        if kind in (IntegrationCredential.Kind.OAUTH_REFRESH, IntegrationCredential.Kind.OAUTH_ACCESS):
            messages.error(request, "User grants are created by Connect, not entered by hand.")
            return redirect(page_url(f"account-{account.pk}"))
        try:
            services.set_credential(account, kind, request.POST.get("secret") or "",
                                    subject=request.POST.get("subject") or "", user=request.user)
        except ValidationError as exc:
            messages.error(request, _errors(exc))
        except Exception as exc:  # ImproperlyConfigured: no GOLDEN_SECRET_KEYS
            logger.error("[integrations] credential not stored: %s", type(exc).__name__)
            messages.error(request, "The credential was not stored: this server has no encryption key "
                                    "(GOLDEN_SECRET_KEYS). See docs/crm/adapters.md#keys.")
        else:
            messages.success(request, f"{kind} stored (encrypted).")
        return redirect(page_url(f"account-{account.pk}"))


class AccountTestView(_AccountPost):
    """Runs provider calls and spends rate-limit tokens: the change permission, like every write."""

    def post(self, request, pk):
        _require_change(request)
        account = tenant_account(request, pk)
        result = run_account_check(account, user=request.user)
        wants_json = (request.headers.get("Accept", "").startswith("application/json")
                      or request.content_type == "application/json")
        if wants_json:
            return JsonResponse(result, status=200)
        return redirect(page_url(f"account-{account.pk}"))


# -- OAuth -----------------------------------------------------------------


def _adapter_or_404(account, *, oauth=True):
    from core.plugins.crm_adapter import Capability

    try:
        adapter = services.adapter_for(account)
    except LookupError:
        raise Http404 from None
    if oauth and Capability.OAUTH_USER not in adapter.capabilities:
        raise Http404
    return adapter


def _callback_uri(request, account) -> str:
    return request.build_absolute_uri(reverse("erp-integration-oauth-callback", args=[account.pk]))


class OAuthConnectView(StaffRequiredMixin, View):
    login_url = "/erp/login/"
    http_method_names = ["get"]

    def get(self, request, pk):
        from core.plugins.crm_adapter import pkce_pair

        account = tenant_account(request, pk)
        if not account.enabled:
            messages.error(request, f"Enable the {account.provider} account before connecting.")
            return redirect(page_url(f"account-{account.pk}"))
        adapter = _adapter_or_404(account)
        verifier, challenge = pkce_pair()
        state = _secrets.token_urlsafe(32)
        redirect_uri = _callback_uri(request, account)
        try:
            url = adapter.authorization_url(state=state, code_challenge=challenge, redirect_uri=redirect_uri)
        except AdapterError as exc:
            messages.error(request, adapter.redact(str(exc)))
            return redirect(page_url(f"account-{account.pk}"))
        flows = {
            k: v for k, v in (request.session.get(SESSION_KEY) or {}).items()
            if isinstance(v, dict) and time.time() - v.get("created", 0) < FLOW_TTL_S
        }
        flows[state] = {"account": str(account.pk), "user": request.user.pk, "verifier": verifier,
                        "redirect_uri": redirect_uri, "created": time.time()}
        if len(flows) > MAX_PENDING_FLOWS:
            for old in sorted(flows, key=lambda k: flows[k]["created"])[:-MAX_PENDING_FLOWS]:
                del flows[old]
        request.session[SESSION_KEY] = flows
        return HttpResponseRedirect(url)


def take_flow(request, account, state: str) -> dict | None:
    """Pop the pending flow for ``state`` (single use); None when unknown, stale or crossed."""
    flows = request.session.get(SESSION_KEY) or {}
    flow = flows.pop(state, None) if isinstance(state, str) and state else None
    request.session[SESSION_KEY] = flows
    if not isinstance(flow, dict):
        return None
    if flow.get("account") != str(account.pk) or flow.get("user") != request.user.pk:
        return None
    if time.time() - flow.get("created", 0) >= FLOW_TTL_S:
        return None
    return flow


class OAuthCallbackView(StaffRequiredMixin, View):
    login_url = "/erp/login/"
    http_method_names = ["get"]

    def get(self, request, pk):
        account = tenant_account(request, pk)
        adapter = _adapter_or_404(account)
        flow = take_flow(request, account, request.GET.get("state", ""))
        if flow is None:
            messages.error(request, "That connection attempt expired or was started elsewhere. Connect again.")
            return redirect(page_url(f"account-{account.pk}"))
        profile = getattr(request.user, "userprofile", None)
        if profile is None or profile.company_id != account.company_id:
            raise PermissionDenied("A grant belongs to a profile of the account's company.")
        if request.GET.get("error"):
            messages.error(request, f"The provider did not grant access ({request.GET['error'][:40]}).")
            return redirect(page_url(f"account-{account.pk}"))
        code = request.GET.get("code", "")
        if not code:
            messages.error(request, "The provider sent no authorization code. Connect again.")
            return redirect(page_url(f"account-{account.pk}"))
        try:
            tokens = adapter.exchange_code(code=code, code_verifier=flow["verifier"],
                                           redirect_uri=flow["redirect_uri"])
            subject = adapter.oauth_subject(tokens)
            adapter.store_grant(profile, tokens, subject=subject)
        except AdapterError as exc:
            logger.warning("[integrations] %s connect failed: account=%s user=%s error=%s",
                           account.provider, account.pk, request.user.pk, type(exc).__name__)
            messages.error(request, adapter.redact(str(exc)))
            return redirect(page_url(f"account-{account.pk}"))
        services._audit(request.user, "connect_grant", account, {"subject": subject[:255]})
        messages.success(request, f"Connected {subject or 'your account'} to {account.provider}.")
        return redirect(page_url(f"account-{account.pk}"))


class OAuthDisconnectView(_AccountPost):
    """Revoke a grant: your own, or anyone's in your company with the change permission."""

    def post(self, request, pk):
        account = tenant_account(request, pk)
        _adapter_or_404(account)
        profile = getattr(request.user, "userprofile", None)
        owner_id = request.POST.get("owner") or (str(profile.pk) if profile else "")
        try:
            owner_id = str(uuid.UUID(owner_id))
        except ValueError:
            raise Http404 from None
        if profile is None or owner_id != str(profile.pk):
            _require_change(request)
        grants = list(IntegrationCredential.objects.filter(
            account=account, owner_id=owner_id or None, owner__isnull=False, revoked_at__isnull=True,
            kind__in=[IntegrationCredential.Kind.OAUTH_REFRESH, IntegrationCredential.Kind.OAUTH_ACCESS],
        ))
        if not grants:
            raise Http404
        for cred in grants:
            services.revoke_credential(cred, user=request.user)
        from core import outbox

        target = next((c for c in grants if c.kind == IntegrationCredential.Kind.OAUTH_REFRESH), grants[0])
        outbox.enqueue(
            f"{account.provider}.revoke",
            outbox.EventSpec(idempotency_key=f"revoke:{target.pk}", payload={"credential_id": str(target.pk)},
                             event="revoke",
                             company_id=account.company_id, occurred_at=timezone.now()),
        )
        messages.success(request, f"Disconnected from {account.provider}. Communications already logged stay.")
        return redirect(page_url(f"account-{account.pk}"))
