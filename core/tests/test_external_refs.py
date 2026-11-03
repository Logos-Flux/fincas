"""core.ExternalRef, core.provenance.track and check_external_refs (phase2-party.md 1.7)."""

from __future__ import annotations

import uuid
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase

from core.models import Company, ExternalRef
from core.provenance import track, tracked_models
from parties.models import Party


class ExternalRefTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other_company = Company.objects.create(name="Tenant B")
        cls.party_ct = ContentType.objects.get_for_model(Party)

    def party(self, company=None, name="Acme"):
        return Party.objects.create(company=company or self.company, kind="organisation",
                                    display_name=name, origin="import")

    def ref(self, target, source_id="42", company=None, **kwargs):
        kwargs.setdefault("source", "hubspot:123")
        kwargs.setdefault("object_type", "company")
        return ExternalRef.objects.create(
            company=company or self.company, source_id=source_id,
            content_type=ContentType.objects.get_for_model(target), object_id=target.pk,
            **kwargs,
        )


class ExternalRefModelTests(ExternalRefTestCase):
    def test_constraint_uniq_extref_source_key(self):
        acme = self.party()
        self.ref(acme)
        # Another type, id or tenant is another source record.
        self.ref(acme, object_type="contact")
        self.ref(acme, source_id="43")
        self.ref(self.party(self.other_company), company=self.other_company)
        with self.assertRaises(IntegrityError) as ctx, transaction.atomic():
            self.ref(self.party(name="Other"))  # same key, different target
        if connection.vendor == "postgresql":
            self.assertIn("uniq_extref_source_key", str(ctx.exception))

    def test_generic_target_resolves(self):
        acme = self.party()
        self.assertEqual(self.ref(acme).target, acme)


class TrackTests(ExternalRefTestCase):
    def test_party_is_tracked(self):
        self.assertIn("parties.party", tracked_models())

    def test_hard_delete_removes_only_that_rows_refs(self):
        acme, other = self.party(), self.party(name="Other")
        self.ref(acme)
        self.ref(acme, source="bigcommerce", object_type="customer")
        kept = self.ref(other, source_id="99")
        acme.delete()
        self.assertEqual(list(ExternalRef.objects.all()), [kept])

    def test_track_is_idempotent_and_returns_the_model(self):
        self.assertIs(track(Party), Party)
        acme = self.party()
        self.ref(acme)
        acme.delete()
        self.assertFalse(ExternalRef.objects.exists())

    def test_track_refuses_a_non_uuid_primary_key(self):
        with self.assertRaises(TypeError):
            track(get_user_model())


class CheckExternalRefsTests(ExternalRefTestCase):
    def run_check(self):
        out = StringIO()
        call_command("check_external_refs", stdout=out)
        return out.getvalue()

    def test_resolving_refs_pass(self):
        self.ref(self.party())
        self.assertIn("1 external ref(s) checked, 0 dangling.", self.run_check())

    def test_dangling_refs_reported_with_exit_1(self):
        self.ref(self.party())
        ExternalRef.objects.create(company=self.company, source="csv:1", object_type="contact",
                                   source_id="7", content_type=self.party_ct,
                                   object_id=uuid.uuid4())
        gone = ContentType.objects.create(app_label="uninstalled_plugin", model="thing")
        ExternalRef.objects.create(company=self.company, source="csv:1", object_type="thing",
                                   source_id="8", content_type=gone, object_id=uuid.uuid4())
        out = StringIO()
        with self.assertRaises(CommandError) as ctx:
            call_command("check_external_refs", stdout=out)
        self.assertEqual(ctx.exception.returncode, 1)
        report = out.getvalue()
        self.assertIn("parties.party: 1 dangling ref(s) (target row missing)", report)
        self.assertIn("csv:1:contact:7", report)
        self.assertIn("uninstalled_plugin.thing: 1 dangling ref(s) (model is not installed)",
                      report)
        self.assertIn("3 external ref(s) checked, 2 dangling.", report)
