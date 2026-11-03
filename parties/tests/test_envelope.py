"""The match envelope, one test per row of docs/crm/phase2-party.md 2.7."""

from __future__ import annotations

from django.test import TestCase

from core.models import Company
from customers.models import Customer
from parties.envelope import build_envelope
from parties.models import Party, PartyEmail, PartyPhone


def person(company, given, family, email=None, phone=None):
    party = Party.objects.create(company=company, kind="person", given_name=given, family_name=family,
                                 display_name=f"{given} {family}", origin="erp")
    if email:
        PartyEmail.objects.create(party=party, email=email, purpose="personal", is_primary=True)
    if phone:
        PartyPhone.objects.create(party=party, number_raw=phone, purpose="personal", is_primary=True)
    return party


class PartyEnvelopeRowTests(TestCase):
    def setUp(self):
        self.a = Company.objects.create(name="A")
        self.b = Company.objects.create(name="B")

    def env(self, companies=None, **kw):
        return build_envelope([self.a.pk] if companies is None else companies, **kw)

    def test_row1_one_exact_email(self):
        ada = person(self.a, "Ada", "Lovelace", email="ada@example.com")
        env = self.env(query="ADA@Example.com")
        self.assertEqual(env["match_quality"], "exact")
        self.assertEqual(env["item"]["id"], str(ada.pk))
        self.assertEqual(env["item"]["rule"], "R1")

    def test_row1_one_exact_phone(self):
        ada = person(self.a, "Ada", "Lovelace", phone="+1 212 555 0143")
        env = self.env(query="(212) 555-0143")
        self.assertEqual((env["match_quality"], env["item"]["id"]), ("exact", str(ada.pk)))
        self.assertEqual((env["item"]["rule"], env["item"]["confidence"]), ("R3", 0.60))
        self.assertEqual(env["item"]["primary_phone_last4"], "0143")

    def test_row2_two_exact_in_one_tenant(self):
        person(self.a, "Ada", "One", email="shared@example.com")
        person(self.a, "Ada", "Two", email="shared@example.com")
        env = self.env(query="shared@example.com")
        self.assertEqual((env["match_quality"], env["item"]), ("ambiguous", None))
        self.assertEqual(len(env["candidates"]), 2)
        self.assertIn("ask the user", env["message"])

    def test_row2_service_key_spanning_two_tenants(self):
        person(self.a, "Ada", "One", email="shared@example.com")
        person(self.b, "Ada", "Two", email="shared@example.com")
        self.assertEqual(self.env(query="shared@example.com")["match_quality"], "exact")
        env = build_envelope(None, query="shared@example.com")
        self.assertEqual((env["match_quality"], env["item"]), ("ambiguous", None))
        self.assertEqual({c["company_name"] for c in env["candidates"]}, {"A", "B"})

    def test_row3_one_search_hit(self):
        ada = person(self.a, "Ada", "Lovelace")
        env = self.env(query="Lovelace")
        self.assertEqual((env["match_quality"], env["item"]["id"]), ("fuzzy", str(ada.pk)))
        self.assertEqual((env["item"]["rule"], env["item"]["confidence"]), ("search", None))
        self.assertIn("Confirm", env["message"])

    def test_row4_several_search_hits(self):
        person(self.a, "Ada", "Lovelace")
        person(self.a, "Ada", "Byron")
        env = self.env(query="Ada")
        self.assertEqual((env["match_quality"], env["item"]), ("ambiguous", None))

    def test_ten_fuzzy_hits_never_return_an_item(self):
        for i in range(12):
            person(self.a, "Sam", f"Smith{i:02d}")
        env = self.env(query="Sam")
        self.assertEqual((env["match_quality"], env["item"]), ("ambiguous", None))
        self.assertEqual(len(env["candidates"]), 10)
        self.assertIn("narrow", env["message"])

    def test_row5_nothing(self):
        env = self.env(query="nobody@example.com")
        self.assertEqual((env["match_quality"], env["item"], env["candidates"]), ("none", None, []))
        self.assertIn("offer to create", env["message"])

    def test_exact_key_params_never_fall_back_to_search(self):
        person(self.a, "Ada", "Lovelace", email="ada@example.com")
        self.assertEqual(self.env(email="ada@example.org")["match_quality"], "none")

    def test_merged_and_erased_parties_never_match(self):
        survivor = person(self.a, "Ada", "One", email="x@example.com")
        loser = person(self.a, "Ada", "Two", email="x@example.com")
        Party.objects.filter(pk=loser.pk).update(merged_into=survivor)
        self.assertEqual(self.env(query="x@example.com")["match_quality"], "exact")

    def test_empty_tenant_set_fails_closed(self):
        person(self.a, "Ada", "Lovelace", email="ada@example.com")
        self.assertEqual(build_envelope([], query="ada@example.com")["match_quality"], "none")


class CustomerEnvelopeTests(TestCase):
    def setUp(self):
        self.a = Company.objects.create(name="A")
        self.b = Company.objects.create(name="B")

    def customer(self, company, email, **kw):
        kw.setdefault("first_name", "Ada")
        kw.setdefault("last_name", "Lovelace")
        return Customer.objects.create(email=email, company=company, **kw)

    def test_exact_customer_email_is_normalised(self):
        c = self.customer(self.a, "Ada@Example.com")
        env = build_envelope([self.a.pk], target="customer", query="ada@EXAMPLE.com")
        self.assertEqual((env["match_quality"], env["item"]["id"]), ("exact", str(c.pk)))
        self.assertEqual(env["item"]["type"], "customer")
        self.assertEqual(env["item"]["party_id"], str(Customer.objects.get(pk=c.pk).party_id))
        self.assertIn("customer", env["item"]["roles"])

    def test_exact_customer_phone_through_its_party(self):
        c = self.customer(self.a, "ada@example.com", phone="212-555-0143")
        env = build_envelope([self.a.pk], target="customer", query="+12125550143")
        self.assertEqual((env["match_quality"], env["item"]["id"]), ("exact", str(c.pk)))

    def test_same_email_in_two_tenants_is_ambiguous_for_a_service_key(self):
        # Customer.email is still globally unique as stored (D13 makes it
        # case-insensitive per tenant); a case variant is the same address.
        self.customer(self.a, "shared@example.com")
        self.customer(self.b, "Shared@Example.com")
        env = build_envelope(None, target="customer", query="shared@example.com")
        self.assertEqual((env["match_quality"], env["item"]), ("ambiguous", None))

    def test_name_search_one_and_many(self):
        self.customer(self.a, "a1@example.com", first_name="Grace", last_name="Hopper")
        self.customer(self.a, "a2@example.com", first_name="Grace", last_name="Kelly")
        self.assertEqual(
            build_envelope([self.a.pk], target="customer", query="grace hopper")["match_quality"], "fuzzy")
        self.assertEqual(
            build_envelope([self.a.pk], target="customer", query="Grace")["match_quality"], "ambiguous")
