"""Company-scoping contract for webhook subscription/log viewsets.

WebhookSubscription carries a nullable ``company`` FK; company-bound users
must only see their own company's subscriptions (plus nothing else),
never another company's endpoints and secrets. Delivery logs scope via
``subscription__company``.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from webhooks.models import WebhookSubscription
from webhooks.views import WebhookDeliveryLogViewSet, WebhookSubscriptionViewSet


def company_user(company, *, username="staff", uid="uid-staff"):
    user = make_staff_user(username=username, password="pw")
    user.userprofile = UserProfile.objects.create(company=company)
    return user


class WebhookSubscriptionScopingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.sub = WebhookSubscription.objects.create(
            target_url="https://a.example.com/hook",
            company=self.company,
            subscribed_events=["order.placed"],
        )
        self.factory = APIRequestFactory()
        self.list_view = WebhookSubscriptionViewSet.as_view({"get": "list"})
        self.detail_view = WebhookSubscriptionViewSet.as_view({"get": "retrieve"})

    def _list(self, user):
        request = self.factory.get("/api/v1/webhooks/subscriptions/")
        force_authenticate(request, user=user)
        return self.list_view(request)

    def _retrieve(self, user, pk):
        request = self.factory.get(f"/api/v1/webhooks/subscriptions/{pk}/")
        force_authenticate(request, user=user)
        return self.detail_view(request, pk=pk)

    def test_same_company_user_sees_own(self):
        response = self._list(user=company_user(self.company))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)

    def test_other_company_user_sees_nothing(self):
        other = Company.objects.create(name="Rival, Inc")
        response = self._list(
            user=company_user(other, username="rival", uid="uid-rival")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_other_company_user_cannot_guess_pk(self):
        other = Company.objects.create(name="Rival2, Inc")
        response = self._retrieve(
            user=company_user(other, username="rival2", uid="uid-rival2"),
            pk=self.sub.pk,
        )
        self.assertEqual(response.status_code, 404)

    def test_anonymous_is_fail_closed_not_500(self):
        # Prod permission semantics (explicit IsAuthenticated, F-003):
        # rejected at the auth layer — never 500, never data.
        response = self._list(user=None)
        self.assertIn(response.status_code, (401, 403))

    def test_delivery_log_scoped_via_subscription(self):
        from webhooks.models import WebhookDeliveryLog

        WebhookDeliveryLog.objects.create(
            subscription=self.sub,
            event_type="order.placed",
            payload={},
            success=True,
        )
        other = Company.objects.create(name="Rival3, Inc")
        request = self.factory.get("/api/v1/webhooks/delivery-logs/")
        force_authenticate(
            request, user=company_user(other, username="rival3", uid="uid-rival3")
        )
        response = WebhookDeliveryLogViewSet.as_view({"get": "list"})(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)


class DispatchEventCompanyScopingTests(TestCase):
    """dispatch_event never fans a company's (or a company-less) event out
    to another tenant's endpoint."""

    def setUp(self):
        from unittest import mock

        self.company = Company.objects.create(name="Example Supply, Inc")
        self.other = Company.objects.create(name="Rival Co")
        self.own = WebhookSubscription.objects.create(
            target_url="https://own.example.com/hook", company=self.company,
            subscribed_events=["*"],
        )
        self.rival = WebhookSubscription.objects.create(
            target_url="https://rival.example.com/hook", company=self.other,
            subscribed_events=["*"],
        )
        self.global_sub = WebhookSubscription.objects.create(
            target_url="https://ops.example.com/hook", subscribed_events=["*"],
        )
        patcher = mock.patch("webhooks.services.deliver_webhook", side_effect=lambda sub, *a: sub)
        self.deliver = patcher.start()
        self.addCleanup(patcher.stop)

    def _targets(self, company):
        from webhooks.services import dispatch_event

        return {sub.target_url for sub in dispatch_event("customer.registered", {}, company=company)}

    def test_company_event_reaches_own_and_global_only(self):
        self.assertEqual(
            self._targets(self.company),
            {"https://own.example.com/hook", "https://ops.example.com/hook"},
        )

    def test_companyless_event_fails_closed_to_global_only(self):
        # A portal customer has no company: their email must not reach
        # every tenant's endpoint.
        self.assertEqual(self._targets(None), {"https://ops.example.com/hook"})

    @override_settings(WEBHOOKS_DELIVER_ON_SIGNALS=True)
    def test_inventory_event_carries_the_warehouse_company(self):
        from unittest import mock

        from webhooks.outbox import build

        stock = mock.MagicMock()
        stock.warehouse.company_id = self.company.pk
        [spec] = build("inventory_adjusted", None, stock, {"warehouse_stock": stock, "delta": 1})
        self.assertEqual(spec.company_id, self.company.pk)
