"""REST API tests for Webhook subscription management and test pings."""

from unittest import mock

from rest_framework import status
from rest_framework.test import APITestCase

from core.tests.helpers import make_staff_user
from webhooks.models import WebhookSubscription


class TestWebhookAPI(APITestCase):
    def setUp(self):
        from core.models import Company, UserProfile

        self.user = make_staff_user(username="api_user", password="password123")
        # Company-bound: unlinked users fail closed (see test_company_scoping).
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.user.userprofile = UserProfile.objects.create(
            company=self.company
        )
        self.client.force_authenticate(user=self.user)

    def test_create_and_list_webhook_subscriptions(self):
        url = "/api/v1/webhooks/subscriptions/"
        data = {
            "target_url": "https://api.thirdparty.com/webhook",
            "description": "Integration test endpoint",
            "subscribed_events": ["order.placed", "payment.completed"],
            "is_active": True,
        }
        resp = self.client.post(url, data, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        assert resp.data["target_url"] == "https://api.thirdparty.com/webhook"
        assert "secret" in resp.data
        assert resp.data["secret"].startswith("whsec_")

        # List
        list_resp = self.client.get(url)
        assert list_resp.status_code == status.HTTP_200_OK
        assert len(list_resp.data["results"]) == 1

    @mock.patch("httpx.Client.post")
    def test_ping_webhook_endpoint(self, mock_post):
        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "PONG"
        mock_post.return_value = mock_resp

        sub = WebhookSubscription.objects.create(
            target_url="https://api.thirdparty.com/test",
            secret="whsec_123",
            subscribed_events=["*"],
            company=self.company,
        )

        ping_url = f"/api/v1/webhooks/subscriptions/{sub.id}/ping/"
        resp = self.client.post(ping_url)
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data["success"] is True
        assert resp.data["status_code"] == 200
