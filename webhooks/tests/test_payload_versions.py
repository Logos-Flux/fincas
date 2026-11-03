"""CRM Phase 2 step 10: webhook payload versions and the party events
(docs/crm/phase2-party.md 5.1, docs/webhooks.md 1.1)."""

from __future__ import annotations

import datetime as dt
import json
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Company
from core.outbox.testing import drain
from core.plugins import signals
from core.plugins.dispatch import emit
from customers.models import Customer
from parties.models import Party
from webhooks import events
from webhooks.models import WebhookSubscription
from webhooks.serializers import WebhookSubscriptionSerializer

V1_KEYS = ["customer_id", "email", "name", "customer_type"]
V2_KEYS = V1_KEYS + ["schema_version", "party_id", "account_holder_party_id", "company_id"]


def _v1_payload_before_step_10(customer) -> dict:
    """The receiver body webhooks/signals.py built before step 10, verbatim."""
    return {
        "customer_id": str(getattr(customer, "id", "")),
        "email": getattr(customer, "email", ""),
        "name": getattr(customer, "full_name", ""),
        "customer_type": getattr(customer, "customer_type", "individual"),
    }


class _Base(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")
        self.delivered = []
        patcher = mock.patch("webhooks.services.deliver_webhook", side_effect=self._deliver)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _deliver(self, sub, event_type, payload, **kwargs):
        self.delivered.append((sub, event_type, payload))
        return mock.Mock()

    def sub(self, events_, *, created_at=None, company=None):
        sub = WebhookSubscription.objects.create(
            target_url="https://hooks.example.com/x", subscribed_events=events_,
            company=company or self.company,
        )
        if created_at is not None:
            WebhookSubscription.objects.filter(pk=sub.pk).update(created_at=created_at)
            sub.refresh_from_db()
        return sub

    def data_for(self, sub, event_type):
        drain()  # signal deliveries run from the outbox
        rows = [p for s, e, p in self.delivered if s.pk == sub.pk and e == event_type]
        self.assertEqual(len(rows), 1, f"{event_type} reached {sub} {len(rows)} times")
        return rows[0]

    def register(self, **kw):
        kw.setdefault("first_name", "Ada")
        kw.setdefault("last_name", "Lovelace")
        kw.setdefault("email", "ada@example.com")
        kw.setdefault("company", self.company)
        with self.captureOnCommitCallbacks(execute=True):
            customer = Customer.objects.create(**kw)
        drain()  # inside the caller's patches and assertLogs
        return customer


@override_settings(WEBHOOKS_DELIVER_ON_SIGNALS=True)
class CustomerRegisteredVersionTests(_Base):
    def test_v1_pinned_subscriber_receives_exactly_the_v1_keys_and_bytes(self):
        pinned = self.sub(["customer.registered@1"])
        customer = self.register()
        data = self.data_for(pinned, "customer.registered")
        self.assertEqual(list(data), V1_KEYS)
        self.assertEqual(
            json.dumps(data, default=str), json.dumps(_v1_payload_before_step_10(customer), default=str),
        )

    def test_v2_includes_party_ids_and_company(self):
        latest = self.sub(["customer.registered"])
        customer = self.register()
        customer.refresh_from_db()
        data = self.data_for(latest, "customer.registered")
        self.assertEqual(list(data), V2_KEYS)
        self.assertEqual(data["schema_version"], 2)
        self.assertIsNotNone(customer.party_id)
        self.assertEqual(data["party_id"], str(customer.party_id))
        self.assertEqual(data["account_holder_party_id"], None)  # no login
        self.assertEqual(data["company_id"], str(self.company.pk))
        json.dumps(data)  # no default=str needed

    def test_explicit_v2_pin_and_wildcards_get_latest(self):
        subs = [self.sub(["customer.registered@2"]), self.sub(["*"]), self.sub(["customer.*"])]
        self.register()
        for sub in subs:
            self.assertEqual(self.data_for(sub, "customer.registered")["schema_version"], 2)

    def test_pin_beats_wildcard_on_the_same_subscription(self):
        sub = self.sub(["*", "customer.registered@1"])
        self.register()
        self.assertEqual(list(self.data_for(sub, "customer.registered")), V1_KEYS)

    def test_existing_subscription_keeps_v1_until_it_pins(self):
        before = events.versioning_cutoff() - dt.timedelta(days=1)
        old_bare = self.sub(["customer.registered"], created_at=before)
        old_wild = self.sub(["*"], created_at=before)
        old_pinned = self.sub(["customer.registered@2"], created_at=before)
        self.register()
        self.assertEqual(list(self.data_for(old_bare, "customer.registered")), V1_KEYS)
        self.assertEqual(list(self.data_for(old_wild, "customer.registered")), V1_KEYS)
        self.assertEqual(list(self.data_for(old_pinned, "customer.registered")), V2_KEYS)

    def test_cutoff_setting_overrides_the_default(self):
        sub = self.sub(["customer.registered"])
        with override_settings(WEBHOOKS_VERSIONING_CUTOFF=(timezone.now() + dt.timedelta(days=1)).isoformat()):
            self.register()
        self.assertEqual(list(self.data_for(sub, "customer.registered")), V1_KEYS)

    def test_deprecated_version_still_delivers_and_logs(self):
        spec = events.EVENTS["customer.registered"]
        sub = self.sub(["customer.registered@1"])
        deprecated = events.EventSchema(
            latest=spec.latest, versions=spec.versions, deprecated=frozenset({1}),
        )
        with mock.patch.dict(events.EVENTS, {"customer.registered": deprecated}), \
                self.assertLogs("golden_erp.webhooks", "WARNING") as logs:
            self.register()
        self.assertEqual(list(self.data_for(sub, "customer.registered")), V1_KEYS)
        self.assertIn("deprecated", logs.output[0])

    def test_one_corrupt_subscription_does_not_stop_the_others(self):
        good = self.sub(["customer.registered"])
        bad = self.sub(["customer.registered"])
        real = WebhookSubscription.event_version

        def flaky(sub, event_name):
            if sub.pk == bad.pk:
                raise ValueError("corrupt")
            return real(sub, event_name)

        with mock.patch.object(WebhookSubscription, "event_version", flaky), \
                self.assertLogs("golden_erp.webhooks", "ERROR"):
            self.register()
        self.data_for(good, "customer.registered")

    @override_settings(GOLDEN_PARTY_SYNC_STRICT=False)
    def test_failed_party_sync_never_names_a_rolled_back_party(self):
        sub = self.sub(["customer.registered"])
        calls = []

        def receiver(sender, **kw):
            calls.append(kw)

        signals.customer_registered.connect(receiver, weak=False)
        self.addCleanup(signals.customer_registered.disconnect, receiver)
        # Step 11: the parties are resolved before the INSERT (unguarded, and
        # committed with the row). The guarded sync after the INSERT fails
        # once it has re-pointed the instance; its savepoint rolls back, and
        # the signal must name the committed party, not a rolled-back one.
        from parties import sync as party_sync

        real, seen = party_sync._sync_refs, []

        def second_call_fails(*args, **kwargs):
            seen.append(1)
            if len(seen) > 1:
                raise RuntimeError("boom")
            return real(*args, **kwargs)

        with mock.patch("parties.sync._sync_refs", side_effect=second_call_fails), \
                self.assertLogs("parties.sync", "ERROR"):
            customer = self.register()
        committed = Customer.objects.get(pk=customer.pk).party_id
        self.assertTrue(Party.objects.filter(pk=committed).exists())
        self.assertEqual(calls[0]["party"].pk, committed)
        self.assertEqual(self.data_for(sub, "customer.registered")["party_id"], str(committed))

    def test_failed_pre_insert_sync_registers_nothing(self):
        sub = self.sub(["customer.registered"])
        with mock.patch("parties.sync._sync_refs", side_effect=RuntimeError("boom")), \
                self.assertRaises(RuntimeError):
            self.register()
        self.assertFalse(Customer.objects.exists())
        self.assertFalse(sub.deliveries.exists())

    def test_signal_carries_party_keywords_and_schema_version(self):
        calls = []

        def receiver(sender, **kw):
            calls.append(kw)

        signals.customer_registered.connect(receiver, weak=False)
        self.addCleanup(signals.customer_registered.disconnect, receiver)
        customer = self.register()
        self.assertEqual(len(calls), 1)
        kw = calls[0]
        self.assertEqual(kw["schema_version"], 2)
        self.assertEqual(kw["customer"].pk, customer.pk)
        self.assertEqual(kw["party"].pk, Customer.objects.get(pk=customer.pk).party_id)
        self.assertIsNone(kw["account_holder"])


class SubscriptionMatchingTests(TestCase):
    def test_matches_event_understands_version_suffixes(self):
        sub = WebhookSubscription(subscribed_events=["customer.registered@1"], is_active=True)
        self.assertTrue(sub.matches_event("customer.registered"))
        self.assertEqual(sub.event_version("customer.registered"), 1)
        self.assertFalse(sub.matches_event("customer.registered@1"))
        self.assertFalse(sub.matches_event("order.placed"))

    def test_unsupported_pin_is_ignored(self):
        sub = WebhookSubscription(subscribed_events=["customer.registered@9"], is_active=True)
        self.assertIsNone(sub.event_version("customer.registered"))

    def test_unsupported_pin_next_to_a_wildcard_falls_back_to_the_default(self):
        sub = WebhookSubscription(subscribed_events=["*", "customer.registered@9"], is_active=True)
        self.assertEqual(sub.event_version("customer.registered"), 2)

    def test_non_ascii_digit_pin_is_malformed_not_a_crash(self):
        self.assertIsNone(events.parse_entry("order.placed@\u00b2"))
        sub = WebhookSubscription(subscribed_events=["order.placed@\u00b2", "order.*"], is_active=True)
        self.assertEqual(sub.event_version("order.placed"), 1)
        s = WebhookSubscriptionSerializer(data={"target_url": "https://x.example.com",
                                                "subscribed_events": ["order.placed@\u00b2"]})
        self.assertFalse(s.is_valid())

    def test_inactive_subscription_matches_nothing(self):
        sub = WebhookSubscription(subscribed_events=["customer.registered@1", "*"], is_active=False)
        self.assertIsNone(sub.event_version("customer.registered"))

    @override_settings(WEBHOOKS_VERSIONING_CUTOFF="not a date")
    def test_bad_cutoff_is_improperly_configured(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            events.versioning_cutoff()

    def test_unversioned_events_are_version_one(self):
        sub = WebhookSubscription(subscribed_events=["order.*", "party.created@1"], is_active=True)
        self.assertEqual(sub.event_version("order.placed"), 1)
        self.assertEqual(sub.event_version("party.created"), 1)

    def test_serializer_refuses_bad_pins(self):
        for bad in (["customer.registered@3"], ["customer.*@1"], ["nope.event@1"],
                    ["customer.registered@x"], ["customer.registered@1", "customer.registered@2"], [1]):
            s = WebhookSubscriptionSerializer(data={"target_url": "https://x.example.com", "subscribed_events": bad})
            self.assertFalse(s.is_valid(), bad)
            self.assertIn("subscribed_events", s.errors)
        ok = WebhookSubscriptionSerializer(data={
            "target_url": "https://x.example.com",
            "subscribed_events": ["customer.registered@1", "order.*", "custom.thing"],
        })
        self.assertTrue(ok.is_valid(), ok.errors)


@override_settings(WEBHOOKS_DELIVER_ON_SIGNALS=True)
class PartyEventTests(_Base):
    def test_party_events_reach_subscribers_with_json_payloads(self):
        sub = self.sub(["party.*"])
        party = Party(company=self.company, kind="person", given_name="Grace", family_name="Hopper",
                      origin="erp")
        with self.captureOnCommitCallbacks(execute=True):
            party.save()
        with self.captureOnCommitCallbacks(execute=True):
            party.is_active = False
            party.save(update_fields=["is_active"])
        created = self.data_for(sub, "party.created")
        updated = self.data_for(sub, "party.updated")
        self.assertEqual(created, {
            "schema_version": 1, "party_id": str(party.pk), "company_id": str(self.company.pk),
            "kind": "person", "display_name": "Grace Hopper", "is_active": True, "merged_into_party_id": None,
            "updated_at": created["updated_at"],
        })
        self.assertTrue(created["updated_at"])
        self.assertFalse(updated["is_active"])
        self.assertIn("is_active", updated["updated_fields"])
        json.dumps(created), json.dumps(updated)

    def test_merge_unmerge_erase_events_are_registered(self):
        sub = self.sub(["party.merged", "party.unmerged", "party.erased"])
        survivor = Party.objects.create(company=self.company, kind="person", display_name="A", origin="erp")
        loser = Party.objects.create(company=self.company, kind="person", display_name="B", origin="erp")
        with self.captureOnCommitCallbacks(execute=True):
            emit(signals.party_merged, Party, survivor, survivor=survivor, merged=[loser], merge_id="m-1")
            emit(signals.party_unmerged, Party, survivor, survivor=survivor, restored=[loser], merge_id="m-1")
            emit(signals.party_erased, Party, loser, party_id=loser.pk, company_id=loser.company_id)
        merged = self.data_for(sub, "party.merged")
        self.assertEqual(merged["merged_party_ids"], [str(loser.pk)])
        self.assertEqual(self.data_for(sub, "party.unmerged")["restored_party_ids"], [str(loser.pk)])
        self.assertEqual(self.data_for(sub, "party.erased"),
                         {"schema_version": 1, "party_id": str(loser.pk), "company_id": str(self.company.pk)})
        for _, _, payload in self.delivered:
            json.dumps(payload)

    def test_other_tenants_never_receive_party_events(self):
        other = Company.objects.create(name="Other")
        theirs = self.sub(["party.created"], company=other)
        ours = self.sub(["party.created"])
        with self.captureOnCommitCallbacks(execute=True):
            Party.objects.create(company=self.company, kind="person", display_name="A", origin="erp")
        self.assertEqual([d for d in self.delivered if d[0].pk == theirs.pk], [])
        self.data_for(ours, "party.created")  # positive control

    def test_erased_without_company_id_falls_back_to_the_row(self):
        sub = self.sub(["party.erased"])
        party = Party.objects.create(company=self.company, kind="person", display_name="A", origin="erp")
        with self.captureOnCommitCallbacks(execute=True):
            emit(signals.party_erased, Party, party, party_id=party.pk)
        self.assertEqual(self.data_for(sub, "party.erased")["company_id"], str(self.company.pk))
