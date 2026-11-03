"""Staff REST API for parties (docs/crm/phase2-party.md 5.2, step 8)."""

from __future__ import annotations

import secrets
import uuid

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from customers.models import Customer, CustomerAddress
from parties.models import (
    ConsentRecord,
    Party,
    PartyAddress,
    PartyEmail,
    PartyPhone,
    PartyRelationship,
    PartyRole,
)

BASE = "/api/v1/parties/"
API_KEY = "parties-api-" + secrets.token_hex(8)


def staff_client(company, username=None):
    user = make_staff_user(username or f"staff-{uuid.uuid4().hex[:6]}", password="pw")
    UserProfile.objects.update_or_create(user=user, defaults={"company": company})
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def person(company, given="Ada", family="Lovelace", email=None, **kw):
    party = Party.objects.create(
        company=company, kind="person", given_name=given, family_name=family,
        display_name=f"{given} {family}", origin="erp", **kw,
    )
    if email:
        PartyEmail.objects.create(party=party, email=email, purpose="personal", is_primary=True)
    return party


def org(company, name="Acme Ltd", **kw):
    return Party.objects.create(company=company, kind="organisation", display_name=name, origin="erp", **kw)


class _Base(TestCase):
    def setUp(self):
        self.a = Company.objects.create(name="Tenant A")
        self.b = Company.objects.create(name="Tenant B")
        self.client, self.user = staff_client(self.a)


class PartyEndpointTests(_Base):
    def test_create_person_with_first_email_and_phone(self):
        r = self.client.post(f"{BASE}parties/", {
            "kind": "person", "given_name": "Grace", "family_name": "Hopper",
            "email": " Grace@Example.COM ", "phone": "(212) 555-0143",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        party = Party.objects.get(pk=r.json()["id"])
        self.assertEqual((party.company, party.origin, party.display_name), (self.a, "api", "Grace Hopper"))
        self.assertEqual(party.primary_email_normalized, "grace@example.com")
        self.assertEqual(party.primary_phone_e164, "+12125550143")

    def test_company_is_not_writable(self):
        r = self.client.post(f"{BASE}parties/", {
            "kind": "organisation", "display_name": "Other", "company": str(self.b.pk),
        }, format="json")
        self.assertEqual(r.status_code, 400)
        party = org(self.a)
        r = self.client.patch(f"{BASE}parties/{party.pk}/", {"company": str(self.b.pk)}, format="json")
        self.assertEqual(r.status_code, 200)
        party.refresh_from_db()
        self.assertEqual(party.company, self.a)

    def test_read_only_fields_ignored(self):
        other = User.objects.create_user("someone")
        party = person(self.a)
        r = self.client.patch(f"{BASE}parties/{party.pk}/", {
            "user": other.pk, "is_self": True, "origin": "import", "merged_into": str(org(self.a).pk),
            "primary_email_normalized": "x@example.com",
        }, format="json")
        self.assertEqual(r.status_code, 200)
        party.refresh_from_db()
        self.assertEqual((party.user_id, party.is_self, party.origin, party.merged_into_id),
                         (None, False, "erp", None))
        self.assertEqual(party.primary_email_normalized, "")

    def test_kind_is_immutable(self):
        party = person(self.a)
        r = self.client.patch(f"{BASE}parties/{party.pk}/", {"kind": "organisation"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("kind", r.json())

    def test_organisation_cannot_have_person_names(self):
        r = self.client.post(f"{BASE}parties/", {
            "kind": "organisation", "display_name": "Acme", "given_name": "Ada",
        }, format="json")
        self.assertEqual(r.status_code, 400)

    def test_invalid_email_on_create_is_400(self):
        r = self.client.post(f"{BASE}parties/", {
            "kind": "person", "given_name": "X", "email": "not-an-email",
        }, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("email", r.json())
        self.assertFalse(Party.objects.exists())

    def test_marketable_change_is_stamped(self):
        party = person(self.a)
        r = self.client.patch(f"{BASE}parties/{party.pk}/", {"marketable": True}, format="json")
        self.assertEqual(r.status_code, 200)
        party.refresh_from_db()
        self.assertTrue(party.marketable)
        self.assertEqual(party.marketable_source, "api")
        self.assertIsNotNone(party.marketable_changed_at)

    def test_no_put_or_delete(self):
        party = person(self.a)
        self.assertEqual(self.client.put(f"{BASE}parties/{party.pk}/", {}, format="json").status_code, 405)
        self.assertEqual(self.client.delete(f"{BASE}parties/{party.pk}/").status_code, 405)

    def test_list_is_company_scoped_and_hides_merged(self):
        mine, theirs = person(self.a, "Mine", "One"), person(self.b, "Theirs", "Two")
        survivor = person(self.a, "Sur", "Vivor")
        loser = person(self.a, "Lo", "Ser")
        Party.objects.filter(pk=loser.pk).update(merged_into=survivor)
        ids = {row["id"] for row in self.client.get(f"{BASE}parties/").json()["results"]}
        self.assertEqual(ids, {str(mine.pk), str(survivor.pk)})
        self.assertNotIn(str(theirs.pk), ids)
        ids = {row["id"] for row in self.client.get(f"{BASE}parties/?include_merged=true").json()["results"]}
        self.assertIn(str(loser.pk), ids)
        self.assertEqual(self.client.get(f"{BASE}parties/{theirs.pk}/").status_code, 404)

    def test_merged_id_redirects_301_to_survivor(self):
        survivor = person(self.a, "Sur", "Vivor")
        loser = person(self.a, "Lo", "Ser")
        Party.objects.filter(pk=loser.pk).update(merged_into=survivor)
        r = self.client.get(f"{BASE}parties/{loser.pk}/")
        self.assertEqual(r.status_code, 301)
        self.assertTrue(r["Location"].endswith(f"{BASE}parties/{survivor.pk}/"))

    def test_filters(self):
        ada = person(self.a, email="ada@example.com")
        acme = org(self.a)
        PartyRole.objects.create(party=acme, role="supplier", valid_from="2026-01-01")
        PartyPhone.objects.create(party=ada, number_raw="+44 20 7946 0958", purpose="work", is_primary=True)
        Customer.objects.create(first_name="Cy", last_name="Cust", email="cy@example.com", company=self.a)

        def ids(query):
            return {row["display_name"] for row in self.client.get(f"{BASE}parties/?{query}").json()["results"]}

        self.assertEqual(ids("kind=organisation"), {"Acme Ltd"})
        self.assertEqual(ids("role=supplier"), {"Acme Ltd"})
        self.assertEqual(ids("has_customer=true"), {"Cy Cust"})
        self.assertNotIn("Cy Cust", ids("has_customer=false"))
        self.assertEqual(ids("search=ada"), {"Ada Lovelace"})
        self.assertEqual(ids("search=ADA@example.com"), {"Ada Lovelace"})
        self.assertEqual(ids("search=%2B442079460958"), {"Ada Lovelace"})

    def test_mirrored_fields_refused_while_customer_exists(self):
        customer = Customer.objects.create(first_name="Cy", last_name="Cust", email="cy@example.com", company=self.a)
        customer.refresh_from_db()
        r = self.client.patch(f"{BASE}parties/{customer.party_id}/", {"given_name": "Changed"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn(str(customer.pk), r.json()["given_name"][0])
        self.assertIn("customers.Customer", r.json()["given_name"][0])
        # Non-mirrored fields stay editable, and a no-op on a mirrored one is fine.
        r = self.client.patch(f"{BASE}parties/{customer.party_id}/",
                              {"tags": ["vip"], "given_name": "Cy"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        detail = self.client.get(f"{BASE}parties/{customer.party_id}/").json()
        self.assertEqual(detail["owned_by"], {"model": "customers.Customer", "id": str(customer.pk)})
        self.assertIn("given_name", detail["mirrored_fields"])


class ContactPointEndpointTests(_Base):
    def test_email_is_normalised_and_company_copied(self):
        party = person(self.a)
        r = self.client.post(f"{BASE}emails/", {
            "party": str(party.pk), "email": "  Ada.L@EXAMPLE.com ", "purpose": "work",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["email_normalized"], "ada.l@example.com")
        self.assertEqual(r.json()["company"], str(self.a.pk))
        self.assertEqual(r.json()["source"], "api")

    def test_email_company_not_writable(self):
        party = person(self.a)
        r = self.client.post(f"{BASE}emails/", {
            "party": str(party.pk), "email": "a@example.com", "purpose": "work", "company": str(self.b.pk),
        }, format="json")
        self.assertEqual(r.status_code, 400)  # naming another tenant is refused outright

    def test_invalid_and_duplicate_email_are_400(self):
        party = person(self.a, email="ada@example.com")
        r = self.client.post(f"{BASE}emails/", {"party": str(party.pk), "email": "a@@b", "purpose": "work"},
                             format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f"{BASE}emails/", {"party": str(party.pk), "email": "ADA@example.com",
                                                "purpose": "work"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_another_tenants_party_is_refused(self):
        theirs = person(self.b)
        r = self.client.post(f"{BASE}emails/", {"party": str(theirs.pk), "email": "x@example.com",
                                                "purpose": "work"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(PartyEmail.objects.filter(party=theirs).exists())

    def test_new_primary_demotes_the_old_one(self):
        party = person(self.a, email="old@example.com")
        r = self.client.post(f"{BASE}emails/", {"party": str(party.pk), "email": "new@example.com",
                                                "purpose": "work", "is_primary": True}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        party.refresh_from_db()
        self.assertEqual(party.primary_email_normalized, "new@example.com")
        self.assertEqual(PartyEmail.objects.filter(party=party, is_primary=True).count(), 1)

    def test_phone_normalised_on_write(self):
        party = person(self.a)
        r = self.client.post(f"{BASE}phones/", {"party": str(party.pk), "number_raw": "020 7946 0958",
                                                "region": "gb", "purpose": "work"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["number_e164"], "+442079460958")
        self.assertNotIn("region", r.json())

    def test_mirrored_rows_refuse_patch_and_delete(self):
        customer = Customer.objects.create(first_name="Cy", last_name="Cust", email="cy@example.com",
                                           phone="2125550143", company=self.a)
        customer.refresh_from_db()
        email = PartyEmail.objects.get(party=customer.party)
        r = self.client.patch(f"{BASE}emails/{email.pk}/", {"purpose": "other"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("customers.Customer", str(r.json()))
        self.assertEqual(self.client.delete(f"{BASE}emails/{email.pk}/").status_code, 400)
        phone = PartyPhone.objects.get(party=customer.party)
        self.assertEqual(self.client.delete(f"{BASE}phones/{phone.pk}/").status_code, 400)
        CustomerAddress.objects.create(customer=customer, address_line_1="1 Main", city="X",
                                       state_province="NY", postal_code="10001", country="US")
        address = PartyAddress.objects.get(party=customer.party)
        r = self.client.patch(f"{BASE}addresses/{address.pk}/", {"city": "Y"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(PartyEmail.objects.filter(pk=email.pk).exists())

    def test_delete_primary_promotes_the_next(self):
        party = person(self.a, email="one@example.com")
        PartyEmail.objects.create(party=party, email="two@example.com", purpose="work")
        first = PartyEmail.objects.get(party=party, is_primary=True)
        self.assertEqual(self.client.delete(f"{BASE}emails/{first.pk}/").status_code, 204)
        party.refresh_from_db()
        self.assertEqual(party.primary_email_normalized, "two@example.com")

    def test_address_country_upper_and_validated(self):
        party = org(self.a)
        payload = {"party": str(party.pk), "purpose": "registered", "address_line_1": "1 Way",
                   "city": "Town", "state_province": "ST", "postal_code": "1", "country": "gb"}
        r = self.client.post(f"{BASE}addresses/", payload, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["country"], "GB")
        payload["country"] = "GBR"
        self.assertEqual(self.client.post(f"{BASE}addresses/", payload, format="json").status_code, 400)


class RoleEndpointTests(_Base):
    def test_staff_role_opens_and_closes(self):
        acme = org(self.a)
        r = self.client.post(f"{BASE}roles/", {"party": str(acme.pk), "role": "supplier"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["source"], "api")
        r = self.client.patch(f"{BASE}roles/{r.json()['id']}/", {"valid_to": "2099-01-01"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def test_system_roles_refused(self):
        ada = person(self.a)
        for role in ("customer", "staff", "lead"):
            r = self.client.post(f"{BASE}roles/", {"party": str(ada.pk), "role": role}, format="json")
            self.assertEqual(r.status_code, 400, role)
        customer = Customer.objects.create(first_name="Cy", last_name="Cust", email="cy@example.com", company=self.a)
        role = PartyRole.objects.get(party_id=customer.party_id, role="customer")
        r = self.client.patch(f"{BASE}roles/{role.pk}/", {"valid_to": "2099-01-01"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_patch_only_valid_to(self):
        acme = org(self.a)
        role = PartyRole.objects.create(party=acme, role="partner", valid_from="2026-01-01")
        r = self.client.patch(f"{BASE}roles/{role.pk}/", {"role": "supplier"}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.patch(f"{BASE}roles/{role.pk}/", {"valid_to": "2020-01-01"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_duplicate_open_role_and_kind_check(self):
        ada = person(self.a)
        PartyRole.objects.create(party=ada, role="partner", valid_from="2026-01-01")
        r = self.client.post(f"{BASE}roles/", {"party": str(ada.pk), "role": "partner"}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f"{BASE}roles/", {"party": str(ada.pk), "role": "nope"}, format="json")
        self.assertEqual(r.status_code, 400)


class RelationshipEndpointTests(_Base):
    def test_create_and_filter_either_side(self):
        ada, acme = person(self.a), org(self.a)
        r = self.client.post(f"{BASE}relationships/", {
            "from_party": str(ada.pk), "to_party": str(acme.pk), "kind": "employment", "title": "CTO",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        for pk in (ada.pk, acme.pk):
            rows = self.client.get(f"{BASE}relationships/?party={pk}&current=true").json()["results"]
            self.assertEqual(len(rows), 1)

    def test_symmetric_kind_stored_canonically(self):
        x, y = person(self.a, "X", "One"), person(self.a, "Y", "Two")
        lo, hi = sorted((x, y), key=lambda p: p.pk)
        r = self.client.post(f"{BASE}relationships/", {
            "from_party": str(hi.pk), "to_party": str(lo.pk), "kind": "spouse",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        rel = PartyRelationship.objects.get()
        self.assertEqual((rel.from_party_id, rel.to_party_id, rel.is_symmetric), (lo.pk, hi.pk, True))

    def test_kind_rules_and_tenancy(self):
        ada, acme = person(self.a), org(self.a)
        r = self.client.post(f"{BASE}relationships/", {
            "from_party": str(acme.pk), "to_party": str(ada.pk), "kind": "employment",
        }, format="json")
        self.assertEqual(r.status_code, 400)
        theirs = org(self.b)
        r = self.client.post(f"{BASE}relationships/", {
            "from_party": str(ada.pk), "to_party": str(theirs.pk), "kind": "employment",
        }, format="json")
        self.assertEqual(r.status_code, 400)

    def test_sync_owned_relationship_refuses_patch(self):
        customer = Customer.objects.create(first_name="Cy", last_name="Cust", email="cy@example.com",
                                           company_name="CyCo", customer_type="business", company=self.a)
        rel = PartyRelationship.objects.get(source="customer-sync")
        r = self.client.patch(f"{BASE}relationships/{rel.pk}/", {"title": "Boss"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn(str(customer.pk), str(r.json()))

    def test_ends_are_immutable(self):
        ada, acme, other = person(self.a), org(self.a), org(self.a, "Other")
        rel = PartyRelationship.objects.create(from_party=ada, to_party=acme, kind="employment")
        r = self.client.patch(f"{BASE}relationships/{rel.pk}/", {"to_party": str(other.pk)}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.patch(f"{BASE}relationships/{rel.pk}/", {"valid_to": "2099-01-01"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)


class ConsentEndpointTests(_Base):
    def test_append_only(self):
        ada = person(self.a)
        r = self.client.post(f"{BASE}consents/", {
            "party": str(ada.pk), "channel": "email", "purpose": "marketing", "state": "granted",
            "address_normalized": " Ada@Example.com ",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual(body["address_normalized"], "ada@example.com")
        self.assertEqual(body["recorded_by"], self.user.pk)
        self.assertEqual(body["source"], "api")
        url = f"{BASE}consents/{body['id']}/"
        self.assertEqual(self.client.patch(url, {"state": "withdrawn"}, format="json").status_code, 405)
        self.assertEqual(self.client.delete(url).status_code, 405)
        self.assertEqual(ConsentRecord.objects.get().state, "granted")

    def test_system_sources_refused(self):
        ada = person(self.a)
        r = self.client.post(f"{BASE}consents/", {
            "party": str(ada.pk), "channel": "email", "purpose": "marketing", "state": "granted",
            "source": "double_opt_in",
        }, format="json")
        self.assertEqual(r.status_code, 400)


class MatchEndpointTests(_Base):
    def test_exact_email_carries_dedupe_rule(self):
        ada = person(self.a, email="ada@example.com")
        r = self.client.post(f"{BASE}match/", {"email": "ADA@example.com"}, format="json")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["match_quality"], "exact")
        self.assertEqual(body["item"]["id"], str(ada.pk))
        self.assertEqual((body["item"]["rule"], body["item"]["confidence"]), ("R1", 0.95))

    def test_get_with_query_params(self):
        person(self.a, email="ada@example.com")
        r = self.client.get(f"{BASE}match/?email=ada@example.com")
        self.assertEqual(r.json()["match_quality"], "exact")

    def test_tenant_user_never_sees_other_tenant(self):
        person(self.b, email="ada@example.com")
        r = self.client.post(f"{BASE}match/", {"email": "ada@example.com"}, format="json")
        self.assertEqual(r.json()["match_quality"], "none")
        r = self.client.post(f"{BASE}match/", {"email": "ada@example.com", "company": str(self.b.pk)},
                             format="json")
        self.assertEqual(r.status_code, 400)

    def test_never_creates(self):
        before = Party.objects.count()
        self.client.post(f"{BASE}match/", {"q": "Nobody Here"}, format="json")
        self.assertEqual(Party.objects.count(), before)


@override_settings(API_KEY=API_KEY)
class PartiesAuthMatrixTests(TestCase):
    """5.2: staff pass; storefront identity and customer-only users get 403 everywhere."""

    ROUTES = ["parties/", "emails/", "phones/", "addresses/", "roles/", "relationships/",
              "consents/", "match/"]

    def setUp(self):
        self.company = Company.objects.create(name="Solo")
        self.party = person(self.company, email="ada@example.com")

    def _detail_routes(self):
        email = PartyEmail.objects.get(party=self.party)
        return [f"parties/{self.party.pk}/", f"emails/{email.pk}/"]

    def _assert_all(self, client, expected):
        for route in self.ROUTES:
            with self.subTest(route=route, method="get"):
                self.assertEqual(client.get(BASE + route).status_code, expected, route)
            with self.subTest(route=route, method="post"):
                self.assertEqual(client.post(BASE + route, {}, format="json").status_code, expected, route)
        for route in self._detail_routes():
            with self.subTest(route=route):
                self.assertEqual(client.get(BASE + route).status_code, expected, route)
                self.assertEqual(client.patch(BASE + route, {}, format="json").status_code, expected, route)

    def test_storefront_identity_gets_403_on_every_route(self):
        from core.authentication import issue_api_jwt

        token = issue_api_jwt(sub="storefront")
        self._assert_all(APIClient(HTTP_AUTHORIZATION=f"Bearer {token}"), 403)

    def test_customer_only_user_gets_403_on_every_route(self):
        user = User.objects.create_user("shopper", "shopper@example.com", "pw")
        Customer.objects.create(first_name="S", last_name="Hopper", email="shopper@example.com",
                                company=self.company, user=user)
        client = APIClient()
        client.force_login(user)
        self._assert_all(client, 403)

    def test_anonymous_gets_401(self):
        client = APIClient()
        for route in self.ROUTES:
            self.assertEqual(client.get(BASE + route).status_code, 401, route)

    def test_staff_reads_every_route(self):
        client, _ = staff_client(self.company)
        for route in self.ROUTES + self._detail_routes():
            with self.subTest(route=route):
                self.assertEqual(client.get(BASE + route).status_code, 200, route)

    def test_service_key_reads_across_tenants(self):
        client = APIClient(HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")
        other = Company.objects.create(name="Other")
        person(other, "Bo", "Other")
        names = {r["display_name"] for r in client.get(f"{BASE}parties/").json()["results"]}
        self.assertEqual(names, {"Ada Lovelace", "Bo Other"})


class SerializerFieldTests(TestCase):
    """No serializer lets a client write ``company`` (or the system-owned links)."""

    def test_company_read_only_everywhere(self):
        from parties import serializers as s

        for cls in (s.PartySerializer, s.PartyListSerializer, s.PartyEmailSerializer,
                    s.PartyPhoneSerializer, s.PartyAddressSerializer, s.PartyRoleSerializer,
                    s.PartyRelationshipSerializer, s.ConsentRecordSerializer):
            with self.subTest(serializer=cls.__name__):
                self.assertTrue(cls().fields["company"].read_only)

    def test_system_links_read_only_on_party(self):
        from parties.serializers import PartySerializer

        fields = PartySerializer().fields
        for name in ("user", "is_self", "origin", "merged_into", "merged_at", "erased_at",
                     "primary_email_normalized", "primary_phone_e164", "marketable_source"):
            with self.subTest(field=name):
                self.assertTrue(fields[name].read_only)

    def test_normalised_columns_read_only(self):
        from parties.serializers import ConsentRecordSerializer, PartyEmailSerializer, PartyPhoneSerializer

        self.assertTrue(PartyEmailSerializer().fields["email_normalized"].read_only)
        self.assertTrue(PartyPhoneSerializer().fields["number_e164"].read_only)
        self.assertTrue(ConsentRecordSerializer().fields["recorded_by"].read_only)

    def test_customer_writable_gains_no_party_link(self):
        from erp_assist.core._writable_fields import CUSTOMER_WRITABLE

        self.assertNotIn("party", CUSTOMER_WRITABLE)
        self.assertNotIn("account_holder", CUSTOMER_WRITABLE)


class ReviewHardeningTests(_Base):
    """Holes found in review: each must stay a 4xx, never a write or a 500."""

    def test_staff_without_company_cannot_create(self):
        user = make_staff_user("nocompany", password="pw")
        client = APIClient()
        client.force_authenticate(user=user)
        r = client.post(f"{BASE}parties/", {"kind": "organisation", "display_name": "X",
                                            "company": str(self.b.pk)}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Party.objects.exists())

    def test_cannot_demote_a_mirrored_primary(self):
        customer = Customer.objects.create(first_name="Cy", last_name="Cust", email="cy@example.com",
                                           company=self.a)
        customer.refresh_from_db()
        r = self.client.post(f"{BASE}emails/", {"party": str(customer.party_id), "email": "x@example.com",
                                                "purpose": "work", "is_primary": True}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Party.objects.get(pk=customer.party_id).primary_email_normalized, "cy@example.com")

    def test_reopening_a_role_that_is_open_again_is_400(self):
        acme = org(self.a)
        closed = PartyRole.objects.create(party=acme, role="partner", valid_from="2026-01-01",
                                          valid_to="2026-02-01")
        PartyRole.objects.create(party=acme, role="partner", valid_from="2026-03-01")
        r = self.client.patch(f"{BASE}roles/{closed.pk}/", {"valid_to": None}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_reopening_a_relationship_that_is_open_again_is_400(self):
        ada, acme = person(self.a), org(self.a)
        closed = PartyRelationship.objects.create(from_party=ada, to_party=acme, kind="employment",
                                                  valid_to="2026-01-01")
        PartyRelationship.objects.create(from_party=ada, to_party=acme, kind="employment")
        r = self.client.patch(f"{BASE}relationships/{closed.pk}/", {"valid_to": None}, format="json")
        self.assertEqual(r.status_code, 400)

    @override_settings(API_KEY=API_KEY)
    def test_service_key_owner_must_share_the_tenant(self):
        _, other_staff = staff_client(self.b)
        client = APIClient(HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")
        User.objects.create_superuser("svc", password=None)
        r = client.post(f"{BASE}parties/", {"kind": "organisation", "display_name": "X",
                                            "company": str(self.a.pk),
                                            "owner": str(other_staff.userprofile.pk)}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Party.objects.exists())


class ConditionalUniquenessTests(_Base):
    """The conditional constraints are checked by us, not by DRF's derived
    UniqueTogetherValidator (which ignores ``condition`` before DRF 3.18)."""

    def test_no_derived_unique_together_validators(self):
        from rest_framework.validators import UniqueTogetherValidator

        from parties import serializers as s

        for cls in (s.PartyEmailSerializer, s.PartyPhoneSerializer, s.PartyAddressSerializer,
                    s.PartyRoleSerializer, s.PartyRelationshipSerializer):
            with self.subTest(serializer=cls.__name__):
                self.assertFalse(any(isinstance(v, UniqueTogetherValidator) for v in cls().validators))

    def test_two_non_primary_emails_for_one_party(self):
        party = person(self.a)
        for addr in ("one@example.com", "two@example.com"):
            r = self.client.post(f"{BASE}emails/", {"party": str(party.pk), "email": addr, "purpose": "work"},
                                 format="json")
            self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(PartyEmail.objects.filter(party=party).count(), 2)

    def test_two_primaries_still_refused_by_the_database(self):
        from django.db import IntegrityError, transaction

        party = person(self.a, email="one@example.com")
        with self.assertRaises(IntegrityError), transaction.atomic():
            PartyEmail.objects.create(party=party, email="two@example.com", purpose="work", is_primary=True)

    def test_closed_role_beside_an_open_one(self):
        acme = org(self.a)
        PartyRole.objects.create(party=acme, role="partner", valid_from="2026-03-01")
        r = self.client.post(f"{BASE}roles/", {"party": str(acme.pk), "role": "partner",
                                               "valid_from": "2025-01-01", "valid_to": "2025-06-01"},
                             format="json")
        self.assertEqual(r.status_code, 201, r.content)
