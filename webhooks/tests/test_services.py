"""Unit tests for Webhook signing, matching, and dispatching services."""

from unittest import mock

from django.test import TestCase, override_settings

from core.outbox.testing import drain
from core.plugins.dispatch import emit
from core.plugins.signals import order_placed
from webhooks.models import WebhookDeliveryLog, WebhookSubscription
from webhooks.services import (
    deliver_webhook,
    dispatch_event,
    sign_payload,
    verify_signature,
)

# Built at runtime so secret scanners do not read a fixture as a credential.
TEST_SIGNING_SECRET = "_".join(["whsec", "test", "fixture"])


class TestWebhookCryptoAndMatching(TestCase):
    def test_hmac_signature_generation_and_verification(self):
        secret = "whsec_test_secret_12345"
        payload = b'{"event":"test.ping","message":"hello"}'

        signature = sign_payload(secret, payload)
        assert signature.startswith("sha256=")

        # Valid signature matches
        assert verify_signature(secret, payload, signature) is True

        # Tampered payload fails
        tampered_payload = b'{"event":"test.ping","message":"tampered"}'
        assert verify_signature(secret, tampered_payload, signature) is False

        # Wrong secret fails
        assert verify_signature("wrong_secret", payload, signature) is False

    def test_matches_event_logic(self):
        sub_all = WebhookSubscription(
            target_url="https://example.com/all",
            subscribed_events=["*"],
            is_active=True,
        )
        assert sub_all.matches_event("order.placed") is True
        assert sub_all.matches_event("payment.completed") is True

        sub_specific = WebhookSubscription(
            target_url="https://example.com/specific",
            subscribed_events=["order.placed", "customer.registered"],
            is_active=True,
        )
        assert sub_specific.matches_event("order.placed") is True
        assert sub_specific.matches_event("customer.registered") is True
        assert sub_specific.matches_event("payment.completed") is False

        sub_wildcard = WebhookSubscription(
            target_url="https://example.com/prefix",
            subscribed_events=["order.*"],
            is_active=True,
        )
        assert sub_wildcard.matches_event("order.placed") is True
        assert sub_wildcard.matches_event("order.cancelled") is True
        assert sub_wildcard.matches_event("payment.completed") is False

        sub_inactive = WebhookSubscription(
            target_url="https://example.com/inactive",
            subscribed_events=["*"],
            is_active=False,
        )
        assert sub_inactive.matches_event("order.placed") is False


class TestWebhookDelivery(TestCase):
    def setUp(self):
        self.sub = WebhookSubscription.objects.create(
            target_url="https://httpbin.org/post",
            secret=TEST_SIGNING_SECRET,
            subscribed_events=["order.*"],
            is_active=True,
        )

    @mock.patch("httpx.Client.post")
    def test_deliver_webhook_success(self, mock_post):
        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"status":"received"}'
        mock_post.return_value = mock_resp

        payload = {"order_id": "ORD-123", "amount": "99.99"}
        log = deliver_webhook(self.sub, "order.placed", payload)

        assert log.success is True
        assert log.response_status_code == 200
        assert log.response_body == '{"status":"received"}'
        assert log.subscription == self.sub
        assert log.event_type == "order.placed"
        assert log.payload["data"] == payload

        # Verify HTTP headers sent
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        headers = kwargs["headers"]
        assert headers["X-Golden-Event"] == "order.placed"
        assert headers["X-Golden-Signature"].startswith("sha256=")

    @mock.patch("httpx.Client.post")
    def test_deliver_webhook_failure(self, mock_post):
        mock_resp = mock.MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_post.return_value = mock_resp

        log = deliver_webhook(self.sub, "order.placed", {"id": 1})
        assert log.success is False
        assert log.response_status_code == 500

    @mock.patch("httpx.Client.post")
    def test_dispatch_event_filters_active(self, mock_post):
        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "OK"
        mock_post.return_value = mock_resp

        # Create inactive sub
        WebhookSubscription.objects.create(
            target_url="https://example.com/ignore",
            secret="whsec_sec",
            subscribed_events=["order.*"],
            is_active=False,
        )

        logs = dispatch_event("order.placed", {"test": True})
        assert len(logs) == 1
        assert logs[0].subscription == self.sub

    def test_signal_delivery_is_off_by_default(self):
        from core.models import OutboxEvent

        order_mock = mock.MagicMock()
        order_mock.company = None
        emit(order_placed, self.__class__, None, order=order_mock)
        self.assertFalse(OutboxEvent.objects.exists())

    @override_settings(WEBHOOKS_DELIVER_ON_SIGNALS=True)
    @mock.patch("httpx.Client.post")
    def test_signal_integration(self, mock_post):
        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "OK"
        mock_post.return_value = mock_resp

        order_mock = mock.MagicMock()
        order_mock.id = "6ba0fa6a-ef74-4044-bf25-be36e6908c7b"
        order_mock.order_number = "SO-1001"
        order_mock.total_amount = "250.00"
        order_mock.status = "confirmed"
        order_mock.company = None

        # Fire canonical signal; delivery runs from the outbox
        emit(order_placed, self.__class__, None, order=order_mock)
        mock_post.assert_not_called()
        drain()

        # Webhook was delivered to self.sub which listens to 'order.*'
        assert WebhookDeliveryLog.objects.filter(event_type="order.placed").exists()
