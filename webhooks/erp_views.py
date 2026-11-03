"""Outbound webhook subscriptions on the Integrations page (release harness
R10.08, ``R10-no-webhook-ui``; docs/webhooks.md).

Until 2026-10-17 subscriptions existed only in the API and the Django admin.
The Integrations page now lists this tenant's subscriptions and offers: add
one (the secret is shown once, in the flash message, never again), enable or
disable one, and "Send test delivery" (the API's ping). Writes need
``core.change_integrationaccount``, the page's change permission, like every
other write there; the subscription is pinned to the requester's company,
else the instance's only one (``core.views.staff_create_company``, which
refuses when neither resolves; a superuser acts as their profile's company).

Validation is the API serializer's (``WebhookSubscriptionSerializer``), so
the screen and the API accept the same entries (``event@n`` pins a payload
version). Every ping, from here or the API, carries ``X-Golden-Delivery-Id``
(``services.ping``), so a receiver can dedupe a repeated test.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View

from core.permissions import is_staff_principal
from core.views import staff_create_company

from .events import EVENTS
from .models import WebhookSubscription
from .serializers import WebhookSubscriptionSerializer
from .services import ping

PERMISSION = "core.change_integrationaccount"


def page_url(anchor: str = "webhooks") -> str:
    return reverse("erp-integrations") + f"#{anchor}"


def subscriptions_context(request) -> dict:
    """The Integrations page's webhook section: this tenant's subscriptions."""
    company = staff_create_company(request.user)
    if company is None:
        return {"webhooks": {"company": None, "subscriptions": [], "events": [], "can_change": False}}
    subscriptions = list(
        WebhookSubscription.objects.filter(company=company).order_by("-created_at")
        .prefetch_related("deliveries")
    )
    rows = []
    for sub in subscriptions:
        last = max(sub.deliveries.all(), key=lambda log: log.created_at, default=None)
        rows.append({"subscription": sub, "last": last})
    return {"webhooks": {
        "company": company, "subscriptions": rows, "events": sorted(EVENTS),
        "can_change": request.user.has_perm(PERMISSION),
    }}


class _SubscriptionPost(View):
    http_method_names = ["post"]

    def dispatch(self, request, *args, **kwargs):
        # The one staff gate: a superuser passes without the is_staff flag.
        if not is_staff_principal(request.user):
            raise PermissionDenied
        if not request.user.has_perm(PERMISSION):
            raise PermissionDenied(f"Changing webhook subscriptions needs {PERMISSION}.")
        # request_company() is None for every superuser; a back-office write
        # lands in the actor's own company, else the only one (E070).
        self.company = staff_create_company(request.user)
        if self.company is None:
            raise PermissionDenied("Your account resolves to no company, so there is no tenant to subscribe for.")
        return super().dispatch(request, *args, **kwargs)

    def _subscription(self, pk):
        return get_object_or_404(WebhookSubscription, pk=pk, company=self.company)


class SubscriptionCreateView(_SubscriptionPost):
    def post(self, request):
        events = [e.strip() for e in request.POST.getlist("events") if e.strip()]
        data = {
            "target_url": request.POST.get("target_url", "").strip(),
            "description": request.POST.get("description", "").strip(),
            "subscribed_events": events,
            "is_active": True,
            "company": self.company.pk,
        }
        secret = request.POST.get("secret", "").strip()
        if secret:
            data["secret"] = secret
        serializer = WebhookSubscriptionSerializer(data=data)
        if not serializer.is_valid():
            for field, errors in serializer.errors.items():
                messages.error(request, f"{field}: {'; '.join(str(e) for e in errors)}")
            return redirect(page_url())
        subscription = serializer.save()
        messages.success(
            request,
            f"Webhook subscription added for {subscription.target_url}. Its signing secret, shown once: "
            f"{subscription.secret}",
        )
        return redirect(page_url(f"webhook-{subscription.pk}"))


class SubscriptionToggleView(_SubscriptionPost):
    def post(self, request, pk):
        subscription = self._subscription(pk)
        subscription.is_active = not subscription.is_active
        subscription.save(update_fields=["is_active", "updated_at"])
        state = "enabled" if subscription.is_active else "disabled"
        messages.success(request, f"Webhook subscription {state}: {subscription.target_url}.")
        return redirect(page_url(f"webhook-{subscription.pk}"))


class SubscriptionPingView(_SubscriptionPost):
    def post(self, request, pk):
        subscription = self._subscription(pk)
        log = ping(subscription, triggered_by=request.user.username)
        if log.success:
            messages.success(
                request,
                f"Test delivery {log.delivery_id} reached {subscription.target_url} "
                f"({log.response_status_code} in {log.duration_ms} ms).",
            )
        else:
            messages.error(
                request,
                f"Test delivery {log.delivery_id} to {subscription.target_url} failed: "
                f"{log.error_message or log.response_status_code}.",
            )
        return redirect(page_url(f"webhook-{subscription.pk}"))
