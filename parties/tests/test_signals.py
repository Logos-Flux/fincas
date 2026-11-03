"""CRM Phase 2 step 10: the party lifecycle signals (phase2-party.md 5.1).

party_created / party_updated fire from ``Party.save()`` after commit, once
per save; the backfill (``parties.0002_backfill`` and ``backfill_parties``)
writes in bulk and sends no lifecycle signal at all.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json

from django.apps import apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import transaction
from django.dispatch import Signal
from django.test import TestCase, override_settings

from core.models import Company
from core.plugins import signals
from customers.models import Customer
from customers.pre_tighten import PreTightenSchemaMixin
from parties.models import Party
from webhooks import events

from .factories import legacy_address, legacy_contact, legacy_customer, legacy_lead

MIGRATION = importlib.import_module("parties.migrations.0002_backfill")

LIFECYCLE = {name: sig for name, sig in vars(signals).items() if isinstance(sig, Signal)}


class Counter:
    """Counts every lifecycle signal sent while connected."""

    def __init__(self, test, *names):
        self.calls: list[tuple[str, dict]] = []
        for name in names or LIFECYCLE:
            receiver = self._receiver(name)
            LIFECYCLE[name].connect(receiver, weak=False, dispatch_uid=f"counter.{name}.{id(self)}")
            test.addCleanup(LIFECYCLE[name].disconnect, dispatch_uid=f"counter.{name}.{id(self)}")

    def _receiver(self, name):
        def receive(sender, signal=None, **kwargs):
            self.calls.append((name, kwargs))
        return receive

    def names(self):
        return [name for name, _ in self.calls]


class _Base(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")

    def legacy_world(self):
        user = User.objects.create_user("p", "p@example.com")
        c = legacy_customer(self.company, user=user, bc_id="1", accepts_marketing=True, phone="2125550100")
        legacy_address(c, is_default=True)
        b = legacy_customer(self.company, customer_type="business", company_name="Org", email="o@org.example")
        legacy_contact(b, email="o@org.example")
        legacy_lead(self.company, email="lead@org.example")


class BackfillSendsNoSignalsTests(PreTightenSchemaMixin, _Base):
    def test_the_data_migration_sends_no_lifecycle_signal(self):
        self.legacy_world()
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True), contextlib.redirect_stdout(io.StringIO()):
            for op in MIGRATION.Migration.operations:
                op.code(apps, None)
        self.assertGreater(Party.objects.count(), 0)
        self.assertEqual(counter.calls, [])

    def test_backfill_parties_command_sends_no_lifecycle_signal(self):
        self.legacy_world()
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True):
            call_command("backfill_parties", "--step", "all", stdout=io.StringIO())
        self.assertGreater(Party.objects.count(), 0)
        self.assertEqual(counter.calls, [])

    def test_counter_does_see_a_live_create(self):
        # Guards the two tests above against a counter that never fires.
        counter = Counter(self, "party_created", "customer_registered")
        with self.captureOnCommitCallbacks(execute=True):
            Customer.objects.create(company=self.company, first_name="A", last_name="B", email="a@example.com")
        self.assertEqual(sorted(set(counter.names())), ["customer_registered", "party_created"])


class PartySignalTests(_Base):
    def party(self, **kw):
        kw.setdefault("kind", "person")
        kw.setdefault("given_name", "Grace")
        kw.setdefault("family_name", "Hopper")
        kw.setdefault("origin", "erp")
        return Party(company=self.company, **kw)

    def test_created_fires_once_after_commit(self):
        counter = Counter(self, "party_created", "party_updated")
        party = self.party()
        with self.captureOnCommitCallbacks(execute=False) as pending:
            party.save()
            self.assertEqual(counter.calls, [])  # not before commit
        for callback in pending:
            callback()
        self.assertEqual(counter.names(), ["party_created"])
        self.assertIs(counter.calls[0][1]["party"], party)

    def test_updated_fires_once_per_save_with_the_written_fields(self):
        party = self.party()
        party.save()
        counter = Counter(self, "party_created", "party_updated")
        with self.captureOnCommitCallbacks(execute=True):
            party.is_active = False
            party.save(update_fields=["is_active"])
        with self.captureOnCommitCallbacks(execute=True):
            party.family_name = "Murray Hopper"
            party.save()
        self.assertEqual(counter.names(), ["party_updated", "party_updated"])
        first, second = (kw["update_fields"] for _, kw in counter.calls)
        self.assertIn("is_active", first)
        self.assertNotIn("family_name", first)
        self.assertIn("family_name", second)
        self.assertEqual(list(first), sorted(first))

    def test_empty_update_fields_sends_nothing(self):
        party = self.party()
        party.save()
        counter = Counter(self, "party_updated")
        with self.captureOnCommitCallbacks(execute=True):
            party.save(update_fields=[])
        self.assertEqual(counter.calls, [])

    def test_rolled_back_save_sends_nothing(self):
        counter = Counter(self, "party_created")
        with self.captureOnCommitCallbacks(execute=True):
            with contextlib.suppress(RuntimeError), transaction.atomic():
                self.party().save()
                raise RuntimeError("abort")
        self.assertEqual(counter.calls, [])

    def test_queryset_update_sends_nothing(self):
        party = self.party()
        party.save()
        counter = Counter(self, "party_updated")
        with self.captureOnCommitCallbacks(execute=True):
            Party.objects.filter(pk=party.pk).update(is_active=False)
        self.assertEqual(counter.calls, [])

    @override_settings(WEBHOOKS_DELIVER_ON_SIGNALS=False)
    def test_payload_builders_are_json_serialisable(self):
        survivor = self.party()
        survivor.save()
        loser = self.party(given_name="G.")
        loser.save()
        customer = Customer.objects.create(company=self.company, first_name="A", last_name="B",
                                           email="b@example.com")
        customer.refresh_from_db()
        payloads = [
            events.customer_registered_payload(customer),
            events.party_payload(survivor),
            events.party_updated_payload(survivor, ("display_name", "is_active")),
            events.party_merge_payload(survivor, [loser], "merge-1", others_key="merged_party_ids"),
            events.party_merge_payload(survivor, [loser], "merge-1", others_key="restored_party_ids"),
            events.party_erased_payload(loser.pk, loser.company_id),
        ]
        for payload in payloads:
            self.assertEqual(json.loads(json.dumps(payload)), payload)
