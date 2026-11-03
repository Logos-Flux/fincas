"""Webhook subscriptions from the Integrations page (release harness R10.08,
``R10-no-webhook-ui``) and the delivery id on pings (R10.09 observation)."""

from __future__ import annotations

import uuid
from unittest import mock

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from webhooks.models import WebhookDeliveryLog, WebhookSubscription

PAGE = reverse("erp-integrations")
CREATE = reverse("erp-webhook-subscription-create")


def _ok_post():
    resp = mock.MagicMock()
    resp.status_code = 200
    resp.text = "ok"
    return resp


class WebhookScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other = Company.objects.create(name="Tenant B")
        cls.admin = make_staff_user("hooks-admin", password="pw")
        cls.admin.user_permissions.add(Permission.objects.get(codename="change_integrationaccount"))
        UserProfile.objects.update_or_create(user=cls.admin, defaults={"company": cls.company})
        cls.reader = make_staff_user("hooks-reader", password="pw")
        UserProfile.objects.update_or_create(user=cls.reader, defaults={"company": cls.company})

    def setUp(self):
        self.client.force_login(self.admin)

    def test_the_page_offers_the_form_and_lists_this_tenants_subscriptions(self):
        mine = WebhookSubscription.objects.create(target_url="https://a.example.com/h", company=self.company)
        theirs = WebhookSubscription.objects.create(target_url="https://b.example.com/h", company=self.other)
        response = self.client.get(PAGE)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="webhook-create-form"')
        self.assertContains(response, f'id="webhook-{mine.pk}"')
        self.assertNotContains(response, f'id="webhook-{theirs.pk}"')
        self.assertContains(response, reverse("erp-webhook-subscription-ping", args=[mine.pk]))

    def test_create_from_the_screen_writes_a_row_and_shows_the_secret_once(self):
        response = self.client.post(CREATE, {
            "target_url": "https://receiver.example.com/golden", "description": "Release harness",
            "events": ["party.created", "order.placed@1"],
        }, follow=True)
        sub = WebhookSubscription.objects.get(target_url="https://receiver.example.com/golden")
        self.assertEqual(sub.company, self.company)
        self.assertEqual(sub.subscribed_events, ["party.created", "order.placed@1"])
        self.assertTrue(sub.is_active)
        self.assertTrue(sub.secret)
        self.assertContains(response, "shown once")
        self.assertContains(response, sub.secret)
        # The page itself never prints the secret again.
        self.assertNotContains(self.client.get(PAGE), sub.secret)

    def test_invalid_events_are_refused_with_the_api_serializers_message(self):
        self.client.post(CREATE, {"target_url": "https://r.example.com/", "events": ["no.such.event@1"]})  # a pin on an unknown event
        self.assertFalse(WebhookSubscription.objects.exists())

    @mock.patch("httpx.Client.post")
    def test_test_delivery_logs_a_row_whose_delivery_id_was_sent_as_the_header(self, post):
        post.return_value = _ok_post()
        sub = WebhookSubscription.objects.create(target_url="https://r.example.com/h", company=self.company)
        response = self.client.post(reverse("erp-webhook-subscription-ping", args=[sub.pk]), follow=True)
        log = WebhookDeliveryLog.objects.get(subscription=sub)
        self.assertEqual(log.event_type, "webhook.ping")
        self.assertTrue(log.success)
        self.assertIsInstance(log.delivery_id, uuid.UUID)
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["X-Golden-Delivery-Id"], str(log.delivery_id))
        self.assertContains(response, str(log.delivery_id))

    @mock.patch("httpx.Client.post")
    def test_the_api_ping_carries_the_header_too(self, post):
        post.return_value = _ok_post()
        sub = WebhookSubscription.objects.create(target_url="https://r.example.com/h", company=self.company)
        api = APIClient()
        api.force_authenticate(user=self.admin)
        response = api.post(f"/api/v1/webhooks/subscriptions/{sub.pk}/ping/")
        self.assertEqual(response.status_code, 200)
        log = WebhookDeliveryLog.objects.get(subscription=sub)
        self.assertEqual(response.data["delivery_id"], str(log.delivery_id))
        self.assertEqual(post.call_args.kwargs["headers"]["X-Golden-Delivery-Id"], str(log.delivery_id))

    def test_toggle_disables_and_enables(self):
        sub = WebhookSubscription.objects.create(target_url="https://r.example.com/h", company=self.company)
        url = reverse("erp-webhook-subscription-toggle", args=[sub.pk])
        self.client.post(url)
        sub.refresh_from_db()
        self.assertFalse(sub.is_active)
        self.client.post(url)
        sub.refresh_from_db()
        self.assertTrue(sub.is_active)

    def test_another_tenants_subscription_is_a_404_and_a_reader_cannot_write(self):
        theirs = WebhookSubscription.objects.create(target_url="https://b.example.com/h", company=self.other)
        self.assertEqual(self.client.post(reverse("erp-webhook-subscription-toggle", args=[theirs.pk])).status_code, 404)
        self.client.force_login(self.reader)
        page = self.client.get(PAGE)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'id="webhook-create-form"')
        self.assertEqual(self.client.post(CREATE, {"target_url": "https://r.example.com/", "events": ["*"]}).status_code, 403)
        self.assertEqual(WebhookSubscription.objects.filter(company=self.company).count(), 0)

    def test_a_superuser_without_the_staff_flag_manages_its_own_tenants_subscriptions(self):
        # is_superuser is independent of is_staff (docs/roles.md); the screens
        # use the one staff gate and the actor's company, not request_company
        # (None for every superuser).
        from django.contrib.auth.models import User

        su = User.objects.create_superuser("hooks-su", "su@example.com", "pw")
        User.objects.filter(pk=su.pk).update(is_staff=False)
        UserProfile.objects.update_or_create(user=su, defaults={"company": self.company})
        theirs = WebhookSubscription.objects.create(target_url="https://b.example.com/h", company=self.other)
        self.client.force_login(User.objects.get(pk=su.pk))

        self.client.post(CREATE, {"target_url": "https://su.example.com/h", "events": ["party.created"]})
        mine = WebhookSubscription.objects.get(target_url="https://su.example.com/h")
        self.assertEqual(mine.company, self.company)
        page = self.client.get(PAGE)
        self.assertContains(page, f'id="webhook-{mine.pk}"')
        self.assertNotContains(page, f'id="webhook-{theirs.pk}"')
        toggle = self.client.post(reverse("erp-webhook-subscription-toggle", args=[mine.pk]))
        self.assertEqual(toggle.status_code, 302)
        mine.refresh_from_db()
        self.assertFalse(mine.is_active)
        other_toggle = self.client.post(reverse("erp-webhook-subscription-toggle", args=[theirs.pk]))
        self.assertEqual(other_toggle.status_code, 404)

    def test_staff_with_no_company_is_refused_when_several_companies_exist(self):
        nobody = make_staff_user("hooks-nocompany", password="pw")
        nobody.user_permissions.add(Permission.objects.get(codename="change_integrationaccount"))
        UserProfile.objects.filter(user=nobody).delete()
        self.client.force_login(nobody)
        response = self.client.post(CREATE, {"target_url": "https://x.example.com/h", "events": ["party.created"]})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(WebhookSubscription.objects.exists())
