"""Webhooks on the outbox (docs/crm/phase6-adapters.md 1.6, step 0).

The request that makes a fact only writes outbox rows; delivery happens in
``process_outbox``, one HTTP attempt per row and run, with no sleep and no
transaction open.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITransactionTestCase

from core import outbox
from core.models import Company, OutboxEvent
from core.outbox.testing import drain
from crm import due, tasks
from customers.models import Customer
from orders.tests.test_recalculate import company_user
from parties.erasure import erase_party
from parties.models import Party
from webhooks.models import WebhookDeliveryLog, WebhookSubscription


def _response(status, text="ok"):
    resp = mock.MagicMock()
    resp.status_code = status
    resp.text = text
    return resp


class _Transport:
    """Stands in for ``httpx.Client.post``: scripted statuses, every request recorded."""

    def __init__(self, *statuses, sleep=0.0):
        self.statuses = list(statuses)
        self.sleep = sleep
        self.calls = []

    def __call__(self, url, content=None, headers=None, **kw):
        self.calls.append({"url": url, "headers": dict(headers or {}), "body": json.loads(content)})
        if self.sleep:
            time.sleep(self.sleep)
        status = self.statuses.pop(0) if self.statuses else 200
        if isinstance(status, Exception):
            raise status
        return _response(status)


@override_settings(WEBHOOKS_DELIVER_ON_SIGNALS=True)
class RequestPathTests(APITransactionTestCase):
    """Real commits, so the old on_commit receiver would have run in the request."""

    def test_a_hanging_receiver_costs_the_request_nothing(self):
        company = Company.objects.create(name="Acme")
        WebhookSubscription.objects.create(target_url="https://hang.example.com/h", subscribed_events=["*"],
                                           company=company)
        api = APIClient()
        api.force_authenticate(user=company_user(company))
        hang = _Transport(sleep=5)
        with mock.patch("httpx.Client.post", side_effect=hang):
            started = time.monotonic()
            response = api.post("/api/v1/customers/customers/",
                                {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"},
                                format="json")
            elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 201, response.data)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(hang.calls, [])  # no HTTP inside the request
        row = OutboxEvent.objects.get(consumer="webhooks", event="customer.registered")
        self.assertEqual(row.state, "pending")
        # The worker delivers it later, with no transaction open.
        fast = _Transport(200)
        with mock.patch("httpx.Client.post", side_effect=fast):
            result = outbox.loop(once=True)
        self.assertGreaterEqual(result.done, 2)  # the expansion and its delivery
        self.assertEqual([c["body"]["event"] for c in fast.calls if c["body"]["event"] == "customer.registered"],
                         ["customer.registered"])


@override_settings(WEBHOOKS_DELIVER_ON_SIGNALS=True)
class DeliveryTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")
        self.sub = WebhookSubscription.objects.create(
            target_url="https://a.example.com/h", subscribed_events=["party.*"], company=self.company)

    def party(self, name="Grace"):
        return Party.objects.create(company=self.company, kind="person", given_name=name, family_name="Hopper",
                                    origin="erp")

    def deliver_rows(self):
        return OutboxEvent.objects.filter(consumer="webhooks.deliver")

    def test_2xx_is_done_with_a_stable_delivery_id_header_and_log(self):
        party = self.party()
        transport = _Transport(200)
        with mock.patch("httpx.Client.post", side_effect=transport):
            drain()
        row = self.deliver_rows().get()
        self.assertEqual(row.state, "done")
        [call] = transport.calls
        self.assertEqual(call["headers"]["X-Golden-Delivery-Id"], str(row.pk))
        self.assertEqual(call["headers"]["X-Golden-Delivery"], str(self.sub.pk))  # unchanged
        self.assertEqual(call["body"]["data"]["party_id"], str(party.pk))
        log = WebhookDeliveryLog.objects.get()
        self.assertEqual((log.delivery_id, log.attempts, log.success), (row.pk, 1, True))

    def test_4xx_is_dead_after_one_attempt(self):
        self.party()
        transport = _Transport(410)
        with mock.patch("httpx.Client.post", side_effect=transport):
            drain()
        row = self.deliver_rows().get()
        self.assertEqual((row.state, row.attempts, row.last_error), ("dead", 1, "http_410"))
        self.assertEqual(len(transport.calls), 1)

    def test_5xx_retries_on_the_backoff_with_the_same_delivery_id(self):
        self.party()
        transport = _Transport(503, ConnectionError("refused"), 200)
        t0 = timezone.now() + dt.timedelta(minutes=1)
        with mock.patch("httpx.Client.post", side_effect=transport), \
                mock.patch("core.outbox.random.random", return_value=0.5):
            now = t0
            for expected_wait in (30, 60, None):
                with mock.patch("core.outbox.timezone.now", return_value=now):
                    drain(now=now)
                row = self.deliver_rows().get()
                if expected_wait is None:
                    break
                self.assertEqual(row.state, "pending")
                self.assertEqual(row.next_attempt_at - now, dt.timedelta(seconds=expected_wait))
                now = row.next_attempt_at
        self.assertEqual((row.state, row.attempts), ("done", 3))
        self.assertEqual({c["headers"]["X-Golden-Delivery-Id"] for c in transport.calls}, {str(row.pk)})
        self.assertEqual(WebhookDeliveryLog.objects.filter(delivery_id=row.pk).count(), 3)  # one per attempt
        self.assertEqual(sorted(r.last_error for r in [row]), [""])

    def test_a_dead_endpoint_retries_alone(self):
        other = WebhookSubscription.objects.create(
            target_url="https://b.example.com/h", subscribed_events=["party.*"], company=self.company)
        self.party()

        def by_url(url, **kw):
            return _response(500 if url.startswith("https://a.") else 200)

        with mock.patch("httpx.Client.post", side_effect=by_url):
            drain()
        states = {r.payload["subscription_id"]: r.state for r in self.deliver_rows()}
        self.assertEqual(states, {str(self.sub.pk): "pending", str(other.pk): "done"})

    def test_a_subscription_switched_off_after_the_event_is_cancelled(self):
        self.party()
        drain("webhooks")
        WebhookSubscription.objects.filter(pk=self.sub.pk).update(is_active=False)
        transport = _Transport()
        with mock.patch("httpx.Client.post", side_effect=transport):
            drain()
        self.assertEqual(self.deliver_rows().get().state, "cancelled")
        self.assertEqual(transport.calls, [])

    def test_a_subscription_moved_to_another_tenant_or_unsubscribed_gets_nothing(self):
        other = Company.objects.create(name="Other")
        second = WebhookSubscription.objects.create(
            target_url="https://b.example.com/h", subscribed_events=["party.*"], company=self.company)
        self.party()
        drain("webhooks")
        WebhookSubscription.objects.filter(pk=self.sub.pk).update(company=other)
        WebhookSubscription.objects.filter(pk=second.pk).update(subscribed_events=["order.*"])
        transport = _Transport()
        with mock.patch("httpx.Client.post", side_effect=transport):
            drain()
        self.assertEqual(sorted(self.deliver_rows().values_list("last_error", flat=True)),
                         ["subscription_tenant_changed", "subscription_unsubscribed"])
        self.assertEqual(transport.calls, [])

    def test_customer_and_party_rows_name_the_party_but_erased_does_not(self):
        customer = Customer.objects.create(company=self.company, first_name="Ada", last_name="L",
                                           email="ada@example.com")
        customer.refresh_from_db()
        rows = {r.event: r for r in OutboxEvent.objects.filter(consumer="webhooks")}
        self.assertEqual(rows["customer.registered"].party_id, customer.party_id)
        self.assertEqual(rows["party.created"].party_id, customer.party_id)
        self.assertEqual(rows["customer.registered"].object_type, "customers.customer")
        from core.plugins import signals
        from core.plugins.dispatch import emit

        emit(signals.party_erased, Party, None, party_id=customer.party_id, company_id=self.company.pk)
        erased = OutboxEvent.objects.get(consumer="webhooks", event="party.erased")
        self.assertIsNone(erased.party_id)

    def test_off_writes_no_row(self):
        with override_settings(WEBHOOKS_DELIVER_ON_SIGNALS=False):
            self.party()
        self.assertFalse(OutboxEvent.objects.exists())

    def test_ping_makes_one_attempt(self):
        api = APIClient()
        api.force_authenticate(user=company_user(self.company))
        transport = _Transport(500, 500, 500, 500)
        with mock.patch("httpx.Client.post", side_effect=transport), \
                mock.patch("webhooks.services.time.sleep") as sleep:
            response = api.post(f"/api/v1/webhooks/subscriptions/{self.sub.pk}/ping/")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(transport.calls), 1)
        sleep.assert_not_called()


@override_settings(WEBHOOKS_DELIVER_ON_SIGNALS=True)
class DurableTaskDueTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")
        self.sub = WebhookSubscription.objects.create(
            target_url="https://a.example.com/h", subscribed_events=["task.due"], company=self.company)
        self.task = tasks.create(company=self.company, title="Call back",
                                 due=timezone.now() - dt.timedelta(minutes=5))
        drain()  # task.created: no subscriber for it

    def test_a_crash_between_the_claim_and_dispatch_still_delivers(self):
        # execute=False: the in-process on_commit dispatch never runs, as if the
        # process died right after the claim's commit.
        with self.captureOnCommitCallbacks(execute=False):
            self.assertEqual(due.emit_due().sent, 1)
        row = OutboxEvent.objects.get(consumer="webhooks", event="task.due")
        self.assertEqual(row.state, "pending")
        transport = _Transport(200)
        with mock.patch("httpx.Client.post", side_effect=transport):
            drain()
            self.assertEqual(due.emit_due().sent, 0)  # the marker holds: no second event
            drain()
        self.assertEqual([c["body"]["data"]["task_id"] for c in transport.calls], [str(self.task.pk)])

    def test_a_failed_delivery_is_retried_not_lost(self):
        due.emit_due()
        with mock.patch("httpx.Client.post", side_effect=_Transport(ConnectionError("down"))):
            drain()
        row = OutboxEvent.objects.get(consumer="webhooks.deliver")
        self.assertEqual((row.state, row.last_error), ("pending", "transport_error"))
        transport = _Transport(200)
        later = row.next_attempt_at + dt.timedelta(seconds=1)
        with mock.patch("httpx.Client.post", side_effect=transport), \
                mock.patch("core.outbox.timezone.now", return_value=later):
            drain(now=later)
        self.assertEqual(OutboxEvent.objects.get(pk=row.pk).state, "done")
        self.assertEqual(len(transport.calls), 1)


@override_settings(WEBHOOKS_DELIVER_ON_SIGNALS=True)
class ErasureTests(TestCase):
    """docs/crm/phase6-adapters.md 4.2: no queued work about an erased person runs."""

    def test_pending_rows_go_finished_rows_are_scrubbed_the_erased_event_stays(self):
        company = Company.objects.create(name="Acme")
        WebhookSubscription.objects.create(target_url="https://a.example.com/h", subscribed_events=["*"],
                                           company=company)
        customer = Customer.objects.create(company=company, first_name="Ada", last_name="Lovelace",
                                           email="ada@example.com")
        customer.refresh_from_db()
        with mock.patch("httpx.Client.post", side_effect=_Transport(200)):
            drain()  # customer.registered + party.created delivered
        done_rows = set(OutboxEvent.objects.filter(party_id=customer.party_id).values_list("pk", flat=True))
        self.assertTrue(done_rows)
        party = Party.objects.get(pk=customer.party_id)
        party.save()  # a party.updated still pending at erasure time
        pending = OutboxEvent.objects.get(event="party.updated", consumer="webhooks")
        staff = User.objects.create_user("eraser")
        result = erase_party(party, staff, "GDPR request #1")
        self.assertFalse(OutboxEvent.objects.filter(pk=pending.pk).exists())
        self.assertGreaterEqual(result.counts["outbox_deleted"], 1)
        self.assertEqual(result.counts["outbox_scrubbed"], len(done_rows))
        for row in OutboxEvent.objects.filter(pk__in=done_rows):
            self.assertEqual(row.payload, {"erased": True})
        self.assertFalse(OutboxEvent.objects.filter(party_id=party.pk, state__in=["pending", "dead"]).exists())
        self.assertNotIn("ada@example.com", json.dumps(list(OutboxEvent.objects.values_list("payload", flat=True))))
        erased = OutboxEvent.objects.get(consumer="webhooks", event="party.erased")
        self.assertEqual(erased.state, "pending")
        self.assertEqual(erased.payload["data"]["party_id"], str(party.pk))
