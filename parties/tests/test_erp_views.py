"""Back-office party pages (docs/crm/phase2-party.md 6.1, 6.2; step 9)."""

from __future__ import annotations

import datetime
import hashlib
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core import nav_registry
from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from customers.models import Customer
from parties.models import (
    ConsentRecord,
    Party,
    PartyAddress,
    PartyEmail,
    PartyPhone,
    PartyRelationship,
    PartyRole,
    Suppression,
)


def person(company, given="Ada", family="Lovelace", **kw):
    return Party.objects.create(company=company, kind="person", given_name=given, family_name=family,
                                display_name=f"{given} {family}", origin="erp", **kw)


def organisation(company, name="Analytical Engines", **kw):
    return Party.objects.create(company=company, kind="organisation", display_name=name, origin="erp", **kw)


class PartyViewTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other_company = Company.objects.create(name="Tenant B")
        cls.staff = make_staff_user("staff-a", password="pw")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={
            "company": cls.company, "display_name": "Sam Staff"})
        cls.other_staff = make_staff_user("staff-b", password="pw")
        UserProfile.objects.update_or_create(user=cls.other_staff, defaults={"company": cls.other_company})

    def setUp(self):
        self.client.force_login(self.staff)


class PartyListTests(PartyViewTestBase):
    def _seed(self, n, company=None):
        company = company or self.company
        org = organisation(company, "Zeta Works")
        out = []
        for i in range(n):
            p = person(company, f"Given{i:02d}", f"Family{i:02d}")
            PartyEmail.objects.create(party=p, email=f"p{i}@example.com", purpose="work", is_primary=True)
            PartyPhone.objects.create(party=p, number_raw=f"(217) 555-{100 + i:04d}", purpose="work",
                                      is_primary=True)
            PartyRole.objects.create(party=p, role="partner", valid_from=datetime.date(2026, 1, 1))
            PartyRole.objects.create(party=p, role="referral_source", valid_from=datetime.date(2026, 1, 1))
            PartyRelationship.objects.create(from_party=p, to_party=org, kind="employment", is_primary=True)
            out.append(p)
        return org, out

    def test_columns_render(self):
        org, people = self._seed(2)
        response = self.client.get(reverse("erp-party-list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Given00 Family00")
        self.assertContains(response, "p0@example.com")
        self.assertContains(response, "(217) 555-0100")  # national format
        self.assertContains(response, "Zeta Works")  # current primary employer
        self.assertContains(response, "Partner")
        self.assertContains(response, "Referral source")

    def test_query_count_is_constant(self):
        self._seed(3)
        url = reverse("erp-party-list")
        self.client.get(url)  # warm session/user caches
        with CaptureQueriesContext(connection) as small:
            self.client.get(url)
        self._seed(30)
        with CaptureQueriesContext(connection) as large:
            response = self.client.get(url)
        self.assertEqual(len(response.context["object_list"]), 35)
        self.assertEqual(len(small), len(large))

    def test_query_budget(self):
        self._seed(10)
        url = reverse("erp-party-list")
        self.client.get(url)
        # 8 shared by every /erp/ page (session, user, staff-role check, ERP
        # settings, profile + company, chat-model settings x2) + the page's 5:
        # count, rows, open roles, current employment, owner choices, saved
        # filters (phase4-fields.md 5.4; list preferences ride on the ERP
        # settings row already loaded), team choices (step 6's Team filter);
        # + 2 for the permission cache (user and group permissions, loaded once
        # per request) behind the Duplicates button's parties.merge_party gate
        # (R8.03, 2026-10-17).
        with self.assertNumQueries(17):
            self.client.get(url)

    def test_only_own_tenant_and_live_parties(self):
        mine = person(self.company, "Mine", "Party")
        person(self.other_company, "Theirs", "Party")
        survivor = person(self.company, "Survivor", "Party")
        merged = person(self.company, "Merged", "Away")
        merged.merged_into = survivor
        merged.save()
        response = self.client.get(reverse("erp-party-list"))
        names = [p.display_name for p in response.context["object_list"]]
        self.assertIn(mine.display_name, names)
        self.assertNotIn("Theirs Party", names)
        self.assertNotIn("Merged Away", names)
        response = self.client.get(reverse("erp-party-list") + "?show=all")
        self.assertIn("Merged Away", [p.display_name for p in response.context["object_list"]])

    def test_search_name_prefix_email_and_phone(self):
        ada = person(self.company, "Ada", "Lovelace")
        PartyEmail.objects.create(party=ada, email="Ada@Example.COM", purpose="work", is_primary=True)
        grace = person(self.company, "Grace", "Hopper")
        PartyPhone.objects.create(party=grace, number_raw="+1 217 555 0142", purpose="work")
        url = reverse("erp-party-list")

        def names(q):
            return [p.display_name for p in self.client.get(url, {"q": q}).context["object_list"]]

        self.assertEqual(names("ada"), ["Ada Lovelace"])
        self.assertEqual(names("hop"), ["Grace Hopper"])  # family-name prefix
        self.assertEqual(names("ada@example.com"), ["Ada Lovelace"])  # normalised email
        self.assertEqual(names("(217) 555-0142"), ["Grace Hopper"])  # normalised phone
        self.assertEqual(names("love@"), [])

    def test_filters(self):
        org = organisation(self.company, "Org One")
        ada = person(self.company, "Ada", "Lovelace", marketable=True)
        grace = person(self.company, "Grace", "Hopper")
        PartyRole.objects.create(party=grace, role="partner", valid_from=datetime.date(2026, 1, 1))
        ConsentRecord.objects.create(party=ada, channel="email", purpose="marketing", state="granted",
                                     occurred_at=timezone.now(), source="erp")
        url = reverse("erp-party-list")

        def names(**params):
            return sorted(p.display_name for p in self.client.get(url, params).context["object_list"])

        self.assertEqual(names(kind="organisation"), [org.display_name])
        self.assertEqual(names(role="partner"), ["Grace Hopper"])
        self.assertEqual(names(marketable="yes"), ["Ada Lovelace"])
        self.assertEqual(names(consent="granted"), ["Ada Lovelace"])
        self.assertEqual(names(consent="unknown", kind="person"), ["Grace Hopper"])
        self.assertEqual(names(customer="yes"), [])

    def test_pagination_is_fifty(self):
        for i in range(55):
            Party.objects.create(company=self.company, kind="organisation", display_name=f"Org {i:02d}",
                                 origin="erp")
        response = self.client.get(reverse("erp-party-list"))
        self.assertEqual(len(response.context["object_list"]), 50)
        self.assertTrue(response.context["is_paginated"])

    def test_non_staff_is_refused(self):
        from django.contrib.auth.models import User

        self.client.force_login(User.objects.create_user("shopper"))
        self.assertEqual(self.client.get(reverse("erp-party-list")).status_code, 403)


class PartyDetailTests(PartyViewTestBase):
    def test_sections_render(self):
        org = organisation(self.company)
        ada = person(self.company)
        PartyEmail.objects.create(party=ada, email="ada@example.com", purpose="work", is_primary=True)
        PartyPhone.objects.create(party=ada, number_raw="(217) 555-0100", purpose="work", is_primary=True)
        PartyAddress.objects.create(party=ada, purpose="mailing", address_line_1="1 Main St",
                                    city="Springfield", state_province="IL", postal_code="62701", country="us")
        PartyRelationship.objects.create(from_party=ada, to_party=org, kind="employment", title="Analyst")
        PartyRole.objects.create(party=ada, role="partner", valid_from=datetime.date(2026, 1, 1))
        ConsentRecord.objects.create(party=ada, channel="email", purpose="marketing", state="withdrawn",
                                     occurred_at=timezone.now(), source="erp")
        Suppression.objects.create(
            company=self.company, channel="email",
            address_hash=hashlib.sha256(b"ada@example.com").hexdigest(), reason="opt_out", source="erp",
        )
        response = self.client.get(reverse("erp-party-detail", args=[ada.pk]))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        for text in ("ada@example.com", "(217) 555-0100", "1 Main St", "works at", "Analytical Engines",
                     "Partner", "Email marketing: Withdrawn", "Suppressed", "a***@example.com"):
            self.assertIn(text, body)
        # Contact points come before relationships and consent (6.2 ranking).
        self.assertLess(body.index('id="contact-points"'), body.index('id="relationships"'))
        self.assertLess(body.index('id="relationships"'), body.index('id="consent"'))
        org_page = self.client.get(reverse("erp-party-detail", args=[org.pk]))
        self.assertContains(org_page, "employs")  # reverse label from the other side

    def test_other_tenant_is_404(self):
        theirs = person(self.other_company)
        email = PartyEmail.objects.create(party=theirs, email="x@example.com", purpose="work")
        for url in (
            reverse("erp-party-detail", args=[theirs.pk]),
            reverse("erp-party-edit", args=[theirs.pk]),
            reverse("erp-party-email-create", args=[theirs.pk]),
            reverse("erp-party-email-edit", args=[email.pk]),
            reverse("erp-party-role-create", args=[theirs.pk]),
            reverse("erp-party-relationship-create", args=[theirs.pk]),
            reverse("erp-party-consent-create", args=[theirs.pk]),
        ):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertEqual(
            self.client.post(reverse("erp-party-email-delete", args=[email.pk])).status_code, 404,
        )
        self.assertTrue(PartyEmail.objects.filter(pk=email.pk).exists())

    def test_customer_mirror_is_read_only(self):
        customer = Customer.objects.create(company=self.company, first_name="Cy", last_name="Buyer",
                                           email="cy@example.com")
        party = Party.objects.get(customer_accounts=customer)
        mirrored = party.emails.get()
        response = self.client.get(reverse("erp-party-detail", args=[party.pk]))
        self.assertContains(response, "account")
        self.assertNotContains(response, reverse("erp-party-email-edit", args=[mirrored.pk]))
        self.assertEqual(self.client.get(reverse("erp-party-email-edit", args=[mirrored.pk])).status_code, 403)
        # The commerce section links the account.
        self.assertContains(response, reverse("erp-customer-detail", args=[customer.pk]))
        # Mirrored names are disabled on the edit form.
        edit = self.client.get(reverse("erp-party-edit", args=[party.pk]))
        self.assertTrue(edit.context["form"].fields["given_name"].disabled)


class PartyFormTests(PartyViewTestBase):
    def test_create_person_in_own_tenant(self):
        response = self.client.post(reverse("erp-party-create"), {
            "kind": "person", "given_name": "Edsger", "family_name": "Dijkstra", "tags_text": "vip, VIP, b",
        })
        party = Party.objects.get(given_name="Edsger")
        self.assertRedirects(response, reverse("erp-party-detail", args=[party.pk]))
        self.assertEqual(party.company, self.company)
        self.assertEqual(party.display_name, "Edsger Dijkstra")
        self.assertEqual(party.origin, "erp")
        self.assertEqual(party.tags, ["vip", "b"])

    def test_organisation_refuses_person_names(self):
        response = self.client.post(reverse("erp-party-create"), {
            "kind": "organisation", "display_name": "Acme", "given_name": "Wile",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Party.objects.filter(display_name="Acme").exists())

    def test_owner_choices_are_own_tenant(self):
        theirs = UserProfile.objects.get(user=self.other_staff)
        response = self.client.post(reverse("erp-party-create"), {
            "kind": "organisation", "display_name": "Acme", "owner": theirs.pk,
        })
        self.assertIn("owner", response.context["form"].errors)

    def test_edit_kind_is_immutable(self):
        ada = person(self.company)
        response = self.client.post(reverse("erp-party-edit", args=[ada.pk]), {
            "kind": "organisation", "given_name": "Ada", "family_name": "King", "display_name": "",
        })
        self.assertEqual(response.status_code, 302)
        ada.refresh_from_db()
        self.assertEqual((ada.kind, ada.family_name, ada.display_name), ("person", "King", "Ada King"))

    def test_email_is_normalised_and_first_is_primary(self):
        ada = person(self.company)
        url = reverse("erp-party-email-create", args=[ada.pk])
        self.client.post(url, {"email": " Ada@EXAMPLE.com ", "purpose": "work", "status": "active"})
        row = ada.emails.get()
        self.assertEqual(row.email_normalized, "ada@example.com")
        self.assertTrue(row.is_primary)
        ada.refresh_from_db()
        self.assertEqual(ada.primary_email_normalized, "ada@example.com")
        # Same address in another case: refused.
        response = self.client.post(url, {"email": "ADA@example.com", "purpose": "work", "status": "active"})
        self.assertIn("email", response.context["form"].errors)
        response = self.client.post(url, {"email": "not-an-address", "purpose": "work", "status": "active"})
        self.assertIn("email", response.context["form"].errors)

    def test_primary_flag_moves(self):
        ada = person(self.company)
        url = reverse("erp-party-email-create", args=[ada.pk])
        self.client.post(url, {"email": "one@example.com", "purpose": "work", "status": "active"})
        self.client.post(url, {"email": "two@example.com", "purpose": "work", "status": "active",
                               "is_primary": "on"})
        self.assertEqual(list(ada.emails.filter(is_primary=True).values_list("email", flat=True)),
                         ["two@example.com"])
        ada.refresh_from_db()
        self.assertEqual(ada.primary_email_normalized, "two@example.com")

    def test_phone_unparseable_cannot_be_primary(self):
        ada = person(self.company)
        url = reverse("erp-party-phone-create", args=[ada.pk])
        response = self.client.post(url, {"number_raw": "555-0143", "purpose": "work", "line_type": "unknown",
                                          "is_primary": "on"})
        self.assertIn("is_primary", response.context["form"].errors)
        self.client.post(url, {"number_raw": "(217) 555-0100", "purpose": "work", "line_type": "unknown"})
        phone = ada.phones.get()
        self.assertEqual(phone.number_e164, "+12175550100")
        self.assertTrue(phone.is_primary)
        response = self.client.post(url, {"number_raw": "+1 217 555 0100", "purpose": "work",
                                          "line_type": "unknown"})
        self.assertIn("number_raw", response.context["form"].errors)

    def test_address_primary_and_country(self):
        ada = person(self.company)
        url = reverse("erp-party-address-create", args=[ada.pk])
        base = {"purpose": "mailing", "address_line_1": "1 Main St", "city": "X", "state_province": "IL",
                "postal_code": "62701"}
        response = self.client.post(url, {**base, "country": "USA"})
        self.assertIn("country", response.context["form"].errors)
        self.client.post(url, {**base, "country": "us", "is_primary": "on"})
        self.client.post(url, {**base, "address_line_1": "2 Main St", "country": "US", "is_primary": "on"})
        self.assertEqual(list(ada.addresses.filter(is_primary=True).values_list("address_line_1", flat=True)),
                         ["2 Main St"])

    def test_role_form_refuses_system_and_duplicate_roles(self):
        ada = person(self.company)
        url = reverse("erp-party-role-create", args=[ada.pk])
        response = self.client.post(url, {"role": "customer", "valid_from": "2026-01-01"})
        self.assertIn("role", response.context["form"].errors)
        self.client.post(url, {"role": "partner", "valid_from": "2026-01-01"})
        self.assertTrue(ada.roles.filter(role="partner", valid_to__isnull=True).exists())
        response = self.client.post(url, {"role": "partner", "valid_from": "2026-02-01"})
        self.assertIn("role", response.context["form"].errors)
        role = ada.roles.get(role="partner")
        self.client.post(reverse("erp-party-role-edit", args=[role.pk]),
                         {"valid_from": "2026-01-01", "valid_to": "2026-03-01"})
        role.refresh_from_db()
        self.assertEqual(role.valid_to, datetime.date(2026, 3, 1))

    def test_system_role_cannot_be_edited(self):
        ada = person(self.company)
        role = PartyRole.objects.create(party=ada, role="customer", valid_from=datetime.date(2026, 1, 1))
        self.assertEqual(self.client.get(reverse("erp-party-role-edit", args=[role.pk])).status_code, 403)

    def test_relationship_by_email_and_direction(self):
        org = organisation(self.company)
        PartyEmail.objects.create(party=org, email="hq@engines.example", purpose="work")
        ada = person(self.company)
        url = reverse("erp-party-relationship-create", args=[org.pk])
        # From the organisation's side, "employs" Ada: stored person -> organisation.
        response = self.client.post(url, {"kind_side": "employment:to", "other_party": str(ada.pk),
                                           "title": "Analyst"})
        self.assertEqual(response.status_code, 302)
        rel = PartyRelationship.objects.get()
        self.assertEqual((rel.from_party, rel.to_party, rel.kind), (ada, org, "employment"))
        # The other way round is refused by kind compatibility.
        grace = person(self.company, "Grace", "Hopper")
        response = self.client.post(reverse("erp-party-relationship-create", args=[grace.pk]),
                                    {"kind_side": "employment:to", "other_party": "hq@engines.example"})
        self.assertIn("kind_side", response.context["form"].errors)

    def test_relationship_refuses_ambiguous_email_and_other_tenant(self):
        ada = person(self.company)
        for name in ("One", "Two"):
            p = person(self.company, name, "Shared")
            PartyEmail.objects.create(party=p, email="shared@example.com", purpose="work")
        url = reverse("erp-party-relationship-create", args=[ada.pk])
        response = self.client.post(url, {"kind_side": "referral:from", "other_party": "shared@example.com"})
        self.assertIn("other_party", response.context["form"].errors)
        theirs = person(self.other_company)
        response = self.client.post(url, {"kind_side": "referral:from", "other_party": str(theirs.pk)})
        self.assertIn("other_party", response.context["form"].errors)
        self.assertFalse(PartyRelationship.objects.exists())

    def test_symmetric_relationship_is_canonical(self):
        a, b = person(self.company, "A", "One"), person(self.company, "B", "Two")
        hi, lo = (a, b) if a.pk > b.pk else (b, a)
        response = self.client.post(reverse("erp-party-relationship-create", args=[hi.pk]),
                                    {"kind_side": "spouse:from", "other_party": str(lo.pk)})
        self.assertEqual(response.status_code, 302)
        rel = PartyRelationship.objects.get()
        self.assertEqual((rel.from_party, rel.to_party), (lo, hi))

    def test_consent_record_is_appended(self):
        ada = person(self.company)
        url = reverse("erp-party-consent-create", args=[ada.pk])
        data = {"channel": "email", "purpose": "marketing", "state": "granted",
                "occurred_at": "2026-10-01T09:30", "address": "Ada@Example.com"}
        self.client.post(url, data)
        self.client.post(url, {**data, "state": "withdrawn", "occurred_at": "2026-10-02T09:30"})
        records = list(ada.consents.order_by("occurred_at"))
        self.assertEqual([r.state for r in records], ["granted", "withdrawn"])
        self.assertEqual(records[0].address_normalized, "ada@example.com")
        self.assertEqual(records[0].source, "erp")
        self.assertEqual(records[0].recorded_by, self.staff)
        response = self.client.post(url, {**data, "channel": "sms", "address": "555"})
        self.assertIn("address", response.context["form"].errors)

    def test_delete_contact_point_promotes_next_primary(self):
        ada = person(self.company)
        first = PartyEmail.objects.create(party=ada, email="a@example.com", purpose="work", is_primary=True)
        PartyEmail.objects.create(party=ada, email="b@example.com", purpose="work")
        self.assertEqual(self.client.get(reverse("erp-party-email-delete", args=[first.pk])).status_code, 405)
        self.client.post(reverse("erp-party-email-delete", args=[first.pk]))
        ada.refresh_from_db()
        self.assertEqual(ada.primary_email_normalized, "b@example.com")


    def test_unsetting_primary_phone_promotes_another(self):
        ada = person(self.company)
        first = PartyPhone.objects.create(party=ada, number_raw="(217) 555-0100", purpose="work", is_primary=True)
        PartyPhone.objects.create(party=ada, number_raw="(217) 555-0101", purpose="work")
        self.client.post(reverse("erp-party-phone-edit", args=[first.pk]),
                         {"number_raw": "(217) 555-0100", "purpose": "work", "line_type": "unknown"})
        ada.refresh_from_db()
        self.assertEqual(ada.primary_phone_e164, "+12175550101")

    def test_delete_promotes_an_active_email(self):
        ada = person(self.company)
        first = PartyEmail.objects.create(party=ada, email="a@example.com", purpose="work", is_primary=True)
        PartyEmail.objects.create(party=ada, email="old@example.com", purpose="work", status="bounced")
        PartyEmail.objects.create(party=ada, email="new@example.com", purpose="work")
        self.client.post(reverse("erp-party-email-delete", args=[first.pk]))
        ada.refresh_from_db()
        self.assertEqual(ada.primary_email_normalized, "new@example.com")

    def test_mirrored_email_cannot_be_deleted(self):
        customer = Customer.objects.create(company=self.company, first_name="Cy", last_name="Buyer",
                                           email="cy@example.com")
        mirrored = Party.objects.get(customer_accounts=customer).emails.get()
        self.assertEqual(self.client.post(reverse("erp-party-email-delete", args=[mirrored.pk])).status_code, 403)
        self.assertTrue(PartyEmail.objects.filter(pk=mirrored.pk).exists())

    def test_one_primary_employer(self):
        ada = person(self.company)
        one, two = organisation(self.company, "One"), organisation(self.company, "Two")
        url = reverse("erp-party-relationship-create", args=[ada.pk])
        for org in (one, two):
            self.client.post(url, {"kind_side": "employment:from", "other_party": str(org.pk), "is_primary": "on"})
        primaries = PartyRelationship.objects.filter(from_party=ada, is_primary=True)
        self.assertEqual([r.to_party for r in primaries], [two])

    def test_csv_export_neutralises_formulas(self):
        organisation(self.company, "=HYPERLINK(1)")
        response = self.client.get(reverse("erp-party-list") + "?export=csv")
        self.assertIn("'=HYPERLINK(1)", b"".join(response.streaming_content).decode())


class PartyNavTests(TestCase):
    def test_nav_entry_under_crm(self):
        items = nav_registry.NAV
        labels = [i.label for i in items]
        crm = labels.index("CRM")
        entry = next(i for i in items if i.url_name == "erp-party-list")
        self.assertEqual(entry.section, "parties")
        self.assertEqual(entry.label_key, "parties.party")
        self.assertGreater(items.index(entry), crm)
        next_header = next(i for i in items[crm + 1:] if i.type == "header")
        self.assertLess(items.index(entry), items.index(next_header))

    @override_settings(GOLDEN_LABEL_OVERRIDES={"parties.party": {"singular": "Contact"}})
    def test_nav_and_title_follow_label(self):
        company = Company.objects.create(name="T")
        staff = make_staff_user("staff-l")
        UserProfile.objects.update_or_create(user=staff, defaults={"company": company})
        self.client.force_login(staff)
        response = self.client.get(reverse("erp-party-list"))
        self.assertContains(response, "Contacts")


class SeedPartiesTests(TestCase):
    def test_seed_is_idempotent_top_up(self):
        company = Company.objects.create(name="Seed Co")
        call_command("seed_parties", count=12, stdout=StringIO())
        self.assertEqual(Party.objects.filter(company=company).count(), 12)
        call_command("seed_parties", count=12, stdout=StringIO())
        self.assertEqual(Party.objects.filter(company=company).count(), 12)
        call_command("seed_parties", count=15, stdout=StringIO())
        self.assertEqual(Party.objects.filter(company=company).count(), 15)
        person_party = Party.objects.filter(company=company, kind="person").first()
        email = person_party.emails.get()
        self.assertEqual(person_party.primary_email_normalized, email.email_normalized)
        self.assertEqual(email.company_id, company.pk)
        self.assertTrue(PartyRelationship.objects.filter(company=company, kind="employment").exists())
        out = StringIO()
        call_command("check_tenancy", "--app", "parties", stdout=out)
