"""Track C: webhooks — HMAC contract, replay guard, retry, rotation.

Covers docs/testing-plan.md §3 "Webhooks" against docs/webhooks.md:
valid / tampered / missing header signatures, replayed timestamps,
retry/backoff on receiver 5xx, delivery-log attempt records, and
secret rotation window behavior.
"""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from webhooks.models import WebhookDeliveryLog, WebhookSubscription
from webhooks.services import (
    deliver_webhook,
    is_fresh_timestamp,
    sign_payload,
    verify_signature,
)


def _sub(**kw):
    kw.setdefault("target_url", "https://example.com/hook")
    kw.setdefault("secret", "whsec_trackc_123")
    kw.setdefault("subscribed_events", ["order.placed"])
    kw.setdefault("is_active", True)
    return WebhookSubscription.objects.create(**kw)


class TestHmacContract(TestCase):
    def test_valid_signature_passes(self):
        body = b'{"event":"order.placed"}'
        sig = sign_payload("whsec_trackc_123", body)
        self.assertTrue(verify_signature("whsec_trackc_123", body, sig))

    def test_tampered_body_rejected(self):
        sig = sign_payload("whsec_trackc_123", b'{"total":"10.00"}')
        self.assertFalse(verify_signature("whsec_trackc_123", b'{"total":"99.99"}', sig))

    def test_missing_header_rejected_without_traceback(self):
        # None / empty header must be a clean False, never TypeError.
        self.assertFalse(verify_signature("whsec_trackc_123", b"{}", None))
        self.assertFalse(verify_signature("whsec_trackc_123", b"{}", ""))

    def test_malformed_prefix_rejected(self):
        self.assertFalse(verify_signature("s", b"{}", "abcdef1234"))
        self.assertFalse(verify_signature("s", b"{}", "sha256="))
        self.assertFalse(verify_signature("s", b"{}", "sha256=zzzz"))


class TestReplayGuard(TestCase):
    def test_fresh_timestamp_accepted(self):
        ts = timezone.now().isoformat()
        self.assertTrue(is_fresh_timestamp(ts))

    def test_replayed_timestamp_rejected(self):
        old = (timezone.now() - timedelta(hours=2)).isoformat()
        self.assertFalse(is_fresh_timestamp(old, max_age_s=300))

    def test_missing_or_garbled_timestamp_rejected(self):
        self.assertFalse(is_fresh_timestamp(""))
        self.assertFalse(is_fresh_timestamp(None))
        self.assertFalse(is_fresh_timestamp("not-a-timestamp"))

    def test_future_timestamp_beyond_skew_rejected(self):
        ahead = (timezone.now() + timedelta(hours=1)).isoformat()
        self.assertFalse(is_fresh_timestamp(ahead, max_age_s=300))


class TestRetryAndLogs(TestCase):
    def _resp(self, code, text=""):
        m = mock.MagicMock()
        m.status_code = code
        m.text = text
        return m

    @mock.patch("webhooks.services.time.sleep", return_value=None)
    def test_retry_on_5xx_then_success(self, _sleep):
        sub = _sub()
        with mock.patch("httpx.Client.post") as post:
            post.side_effect = [
                self._resp(500, "boom"),
                self._resp(503, "still down"),
                self._resp(200, "ok"),
            ]
            log = deliver_webhook(sub, "order.placed", {"id": 1}, max_retries=3)
        self.assertTrue(log.success)
        self.assertEqual(log.response_status_code, 200)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(log.attempts, 3)

    @mock.patch("webhooks.services.time.sleep", return_value=None)
    def test_retry_exhausted_records_failure_with_attempts(self, _sleep):
        sub = _sub()
        with mock.patch("httpx.Client.post") as post:
            post.return_value = self._resp(500, "down")
            log = deliver_webhook(sub, "order.placed", {"id": 1}, max_retries=2)
        self.assertFalse(log.success)
        self.assertEqual(log.response_status_code, 500)
        self.assertEqual(post.call_count, 3)  # initial + 2 retries
        self.assertEqual(log.attempts, 3)

    @mock.patch("webhooks.services.time.sleep", return_value=None)
    def test_no_retry_on_4xx(self, _sleep):
        sub = _sub()
        with mock.patch("httpx.Client.post") as post:
            post.return_value = self._resp(404, "gone")
            log = deliver_webhook(sub, "order.placed", {"id": 1}, max_retries=3)
        self.assertFalse(log.success)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(log.attempts, 1)

    def test_backoff_sleeps_between_attempts(self):
        sub = _sub()
        with mock.patch("httpx.Client.post") as post, mock.patch(
            "webhooks.services.time.sleep"
        ) as sleep:
            post.return_value = self._resp(500, "down")
            deliver_webhook(sub, "order.placed", {"id": 1}, max_retries=2)
        self.assertEqual(sleep.call_count, 2)
        delays = [c.args[0] for c in sleep.call_args_list]
        self.assertLess(delays[0], delays[1])  # exponential growth

    def test_delivery_log_persists_and_filters(self):
        sub = _sub()
        with mock.patch("httpx.Client.post") as post:
            post.return_value = self._resp(200, "ok")
            log = deliver_webhook(sub, "order.placed", {"id": 7})
        fetched = WebhookDeliveryLog.objects.get(pk=log.pk)
        self.assertEqual(fetched.event_type, "order.placed")
        self.assertEqual(fetched.payload["data"], {"id": 7})
        self.assertTrue(fetched.success)


class TestSecretRotation(TestCase):
    def test_old_secret_valid_during_window(self):
        sub = _sub(secret="whsec_new", previous_secret="whsec_old")
        body = b'{"event":"ping"}'
        self.assertTrue(
            verify_signature(sub.secret, body, sign_payload("whsec_old", body),
                             previous_secret=sub.previous_secret)
        )

    def test_old_secret_rejected_after_rotation(self):
        sub = _sub(secret="whsec_new", previous_secret="")
        body = b'{"event":"ping"}'
        self.assertFalse(
            verify_signature(sub.secret, body, sign_payload("whsec_old", body),
                             previous_secret=sub.previous_secret)
        )

    def test_new_secret_always_valid(self):
        sub = _sub(secret="whsec_new", previous_secret="whsec_old")
        body = b'{"event":"ping"}'
        self.assertTrue(
            verify_signature(sub.secret, body, sign_payload("whsec_new", body),
                             previous_secret=sub.previous_secret)
        )
