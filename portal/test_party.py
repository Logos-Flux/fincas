"""Portal routes on Party: ``me/`` and ``me/consents/`` (CRM Phase 2 step 7,
docs/crm/phase2-party.md 5.3).

The party is always the requester's own (resolved from the token, never from
the body), so the tenancy tests forge ids of another customer's and another
tenant's rows and expect them to be indistinguishable from missing ones.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import Company
from core.tests.helpers import make_portal_customer
from core.tests.test_portal_auth import SECRET, make_token
from customers.models import Customer
from parties.models import ConsentRecord, Party, PartyPhone, Suppression
from parties.portal import address_hash

ME = "/api/v1/portal/customer/me/"
CONSENTS = "/api/v1/portal/customer/me/consents/"
PROFILE = "/api/v1/portal/customer/profile/"


@override_settings(SUPABASE_JWT_SECRET=SECRET)
class _Base(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Portal Tenant")
        self.customer, sub = make_portal_customer(
            "me@example.com", first_name="Mia", last_name="Me", company=self.company,
            phone="+1 312 555 0101",
        )
        self.party = self.customer.account_holder
        self.client = APIClient(HTTP_AUTHORIZATION=f"Bearer {make_token(sub=sub)}")


class MeReadTests(_Base):
    def test_shows_own_party(self):
        response = self.client.get(ME)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["id"], str(self.party.pk))
        self.assertEqual((body["kind"], body["display_name"]), ("person", "Mia Me"))
        self.assertEqual(body["emails"], [
            {"email": "me@example.com", "purpose": "billing", "is_primary": True, "verified": False},
        ])
        (phone,) = body["phones"]
        self.assertEqual((phone["number"], phone["is_primary"], phone["editable"]), ("+13125550101", True, False))
        self.assertIsNone(body["account_for"])
        for hidden in ("roles", "owner", "tags", "marketable", "metadata", "user"):
            self.assertNotIn(hidden, body)

    def test_other_phones_are_masked(self):
        PartyPhone.objects.create(party=self.party, number_raw="+1 312 555 0199", purpose="work", source="erp")
        phones = self.client.get(ME).json()["phones"]
        self.assertEqual([p["number"] for p in phones], ["+13125550101", "•••••••0199"])

    def test_business_account_shows_the_organisation_name_only(self):
        self.customer.customer_type = "business"
        self.customer.company_name = "Acme Ltd"
        self.customer.save()
        body = self.client.get(ME).json()
        self.assertEqual(body["account_for"], {"name": "Acme Ltd"})
        self.assertEqual(body["id"], str(self.party.pk))

    def test_profile_exposes_party_links(self):
        body = self.client.get(PROFILE).json()
        self.assertEqual(body["account_holder_party_id"], str(self.party.pk))
        self.assertEqual(body["party_id"], str(self.customer.party_id))

    def test_profile_party_links_are_read_only(self):
        other = Party.objects.create(company=self.company, kind="person", display_name="X", origin="erp")
        self.client.patch(PROFILE, {"party_id": str(other.pk), "account_holder_party_id": str(other.pk)},
                          format="json")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.account_holder, self.party)

    def test_anonymous_is_refused(self):
        self.assertEqual(APIClient().get(ME).status_code, 401)
        self.assertEqual(APIClient().get(CONSENTS).status_code, 401)


class MePatchTests(_Base):
    def phones(self):
        return list(PartyPhone.objects.filter(party=self.party).order_by("created_at"))

    def test_add_a_phone_normalised_not_primary(self):
        response = self.client.patch(ME, {"phones": [{"number": "(312) 555-0142", "purpose": "work"}]},
                                     format="json")
        self.assertEqual(response.status_code, 200, response.content)
        added = self.phones()[-1]
        self.assertEqual((added.number_e164, added.source, added.purpose, added.is_primary),
                         ("+13125550142", "portal", "work", False))

    def test_set_primary_keeps_one_primary(self):
        self.client.patch(ME, {"phones": [{"number": "312 555 0142", "is_primary": True}]}, format="json")
        primaries = [p for p in self.phones() if p.is_primary]
        self.assertEqual([p.number_e164 for p in primaries], ["+13125550142"])
        self.party.refresh_from_db()
        self.assertEqual(self.party.primary_phone_e164, "+13125550142")
        mirrored = self.phones()[0]
        response = self.client.patch(ME, {"phones": [{"id": str(mirrored.pk), "is_primary": True}]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([p.pk for p in self.phones() if p.is_primary], [mirrored.pk])

    def test_update_own_portal_phone(self):
        self.client.patch(ME, {"phones": [{"number": "312 555 0142"}]}, format="json")
        added = self.phones()[-1]
        response = self.client.patch(ME, {"phones": [{"id": str(added.pk), "number": "312 555 0177"}]},
                                     format="json")
        self.assertEqual(response.status_code, 200, response.content)
        added.refresh_from_db()
        self.assertEqual(added.number_e164, "+13125550177")

    def test_mirrored_phone_number_is_owned_by_the_profile(self):
        mirrored = self.phones()[0]
        response = self.client.patch(ME, {"phones": [{"id": str(mirrored.pk), "number": "312 555 0177"}]},
                                     format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("profile/", str(response.json()))

    def test_names_and_emails_are_refused(self):
        for body in ({"display_name": "Someone Else"}, {"emails": [{"email": "x@example.com"}]},
                     {"marketable": True}, {"user": 1}):
            response = self.client.patch(ME, body, format="json")
            self.assertEqual(response.status_code, 400, body)
        self.party.refresh_from_db()
        self.assertEqual(self.party.display_name, "Mia Me")

    def test_invalid_and_duplicate_numbers_are_refused(self):
        for item in ({"number": "12"}, {"number": "+1 312 555 0101"}, {}):
            response = self.client.patch(ME, {"phones": [item]}, format="json")
            self.assertEqual(response.status_code, 400, item)
        self.assertEqual(len(self.phones()), 1)

    def test_two_primaries_and_unsetting_the_primary_are_refused(self):
        response = self.client.patch(ME, {"phones": [{"number": "312 555 0142", "is_primary": True},
                                                     {"number": "312 555 0143", "is_primary": True}]},
                                     format="json")
        self.assertEqual(response.status_code, 400)
        response = self.client.patch(ME, {"phones": [{"id": str(self.phones()[0].pk), "is_primary": False}]},
                                     format="json")
        self.assertEqual(response.status_code, 400)

    def test_phone_cap(self):
        response = self.client.patch(
            ME, {"phones": [{"number": f"312 555 01{n:02d}"} for n in range(20, 31)]}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.phones()), 1)


@override_settings(SUPABASE_JWT_SECRET=SECRET)
class TenancyTests(_Base):
    """A customer of tenant A cannot read or write another party, even with forged ids."""

    def setUp(self):
        super().setUp()
        self.other_company = Company.objects.create(name="Other Tenant")
        self.foreign, _ = make_portal_customer("them@example.com", company=self.other_company,
                                               phone="+1 312 555 0300")
        self.neighbour, _ = make_portal_customer("next@example.com", company=self.company,
                                                 phone="+1 312 555 0400")
        self.foreign_phone = PartyPhone.objects.get(party=self.foreign.account_holder)
        self.neighbour_phone = PartyPhone.objects.get(party=self.neighbour.account_holder)

    def test_forged_phone_ids_are_unknown(self):
        with override_settings(GOLDEN_PORTAL_COMPANY=str(self.company.pk)):
            for phone in (self.foreign_phone, self.neighbour_phone):
                for item in ({"id": str(phone.pk), "is_primary": True},
                             {"id": str(phone.pk), "number": "312 555 0999"}):
                    response = self.client.patch(ME, {"phones": [item]}, format="json")
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("Unknown phone", str(response.json()))
                phone_before = (phone.number_raw, phone.is_primary, phone.party_id)
                phone.refresh_from_db()
                self.assertEqual((phone.number_raw, phone.is_primary, phone.party_id), phone_before)

    def test_forged_party_ids_in_the_body_are_ignored_or_refused(self):
        with override_settings(GOLDEN_PORTAL_COMPANY=str(self.company.pk)):
            response = self.client.patch(ME, {"id": str(self.foreign.account_holder_id),
                                              "party": str(self.foreign.account_holder_id)}, format="json")
            self.assertEqual(response.status_code, 400)
            response = self.client.post(CONSENTS, {"channel": "email", "state": "granted", "wording_version": "v1",
                                                   "party": str(self.foreign.account_holder_id)}, format="json")
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.data["consents"][0]["state"], "granted")
            self.assertFalse(ConsentRecord.objects.filter(party=self.foreign.account_holder).exists())
            self.assertFalse(ConsentRecord.objects.filter(party=self.neighbour.account_holder).exists())
            body = self.client.get(ME).json()
            self.assertEqual(body["id"], str(self.party.pk))
            self.assertNotIn("them@example.com", str(body))

    def test_foreign_tenant_login_cannot_reach_this_portal(self):
        """The other tenant's customer, signing in here, is not a customer of this portal."""
        from core.identity.models import UserIdentity

        sub = UserIdentity.objects.get(user=self.foreign.user).subject
        client = APIClient(HTTP_AUTHORIZATION=f"Bearer {make_token(sub=sub)}")
        with override_settings(GOLDEN_PORTAL_COMPANY=str(self.company.pk)):
            self.assertEqual(client.get(ME).status_code, 403)
            self.assertEqual(client.get(CONSENTS).status_code, 403)


class ConsentTests(_Base):
    def state(self, channel="email"):
        rows = {row["channel"]: row for row in self.client.get(CONSENTS).json()["consents"]}
        return rows[channel]

    def test_initial_state_is_unknown_for_email_and_sms(self):
        body = self.client.get(CONSENTS).json()["consents"]
        self.assertEqual([(r["channel"], r["purpose"], r["state"], r["suppressed"]) for r in body],
                         [("email", "marketing", "unknown", False), ("sms", "marketing", "unknown", False)])

    def test_grant_then_withdraw_appends_records_with_evidence(self):
        response = self.client.post(CONSENTS, {"channel": "sms", "state": "granted", "wording_version": "sms-v2"},
                                    format="json", HTTP_USER_AGENT="Test/1.0", REMOTE_ADDR="203.0.113.9")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.state("sms")["state"], "granted")
        self.client.post(CONSENTS, {"channel": "sms", "state": "withdrawn", "wording_version": "sms-v2"},
                         format="json")
        self.assertEqual(self.state("sms")["state"], "withdrawn")
        records = list(ConsentRecord.objects.filter(party=self.party, channel="sms").order_by("recorded_at"))
        self.assertEqual([r.state for r in records], ["granted", "withdrawn"])
        first = records[0]
        self.assertEqual((first.source, first.purpose, first.address_normalized), ("portal", "marketing", ""))
        self.assertEqual(first.evidence["wording_version"], "sms-v2")
        self.assertEqual(first.evidence["user_agent"], "Test/1.0")
        self.assertIn("ip", first.evidence)
        self.assertEqual(first.recorded_by, self.customer.user)

    def test_email_marketing_keeps_accepts_marketing_in_step(self):
        self.client.post(CONSENTS, {"channel": "email", "state": "granted", "wording_version": "v1"}, format="json")
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.accepts_marketing)
        self.client.post(CONSENTS, {"channel": "email", "state": "withdrawn", "wording_version": "v1"}, format="json")
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.accepts_marketing)
        # The customer sync appended nothing contradicting the portal's records.
        states = list(ConsentRecord.objects.filter(party=self.party, channel="email")
                      .order_by("recorded_at").values_list("state", "source"))
        self.assertEqual(states, [("granted", "portal"), ("withdrawn", "portal")])
        self.customer.first_name = "Mira"
        self.customer.save()
        self.assertEqual(ConsentRecord.objects.filter(party=self.party).count(), 2)
        self.assertEqual(self.state()["state"], "withdrawn")

    def test_vocabulary_is_closed(self):
        for body in ({"channel": "phone", "state": "granted", "wording_version": "v1"},
                     {"channel": "email", "purpose": "transactional", "state": "withdrawn", "wording_version": "v1"},
                     {"channel": "email", "state": "unknown", "wording_version": "v1"},
                     {"channel": "email", "state": "granted"}):
            self.assertEqual(self.client.post(CONSENTS, body, format="json").status_code, 400, body)
        self.assertFalse(ConsentRecord.objects.filter(source="portal").exists())

    def test_suppression_shows_a_message_and_is_read_only(self):
        Suppression.objects.create(company=self.company, channel="email", address_hash=address_hash("me@example.com"),
                                   address_hint="m***@example.com", reason="hard_bounce", source="erp")
        row = self.state()
        self.assertTrue(row["suppressed"])
        self.assertIn("can't send marketing", row["message"])
        self.assertNotIn("hard_bounce", str(row))
        self.client.post(CONSENTS, {"channel": "email", "state": "granted", "wording_version": "v1"}, format="json")
        self.assertTrue(Suppression.objects.filter(lifted_at__isnull=True).exists())
        self.assertTrue(self.state()["suppressed"])
        for method in ("put", "patch", "delete"):
            self.assertEqual(getattr(self.client, method)(CONSENTS).status_code, 405)


class DriftTests(_Base):
    def test_account_holder_on_someone_elses_party_is_never_shown(self):
        """A drifted account holder carrying another login: me/ refuses, writes nothing."""
        from django.contrib.auth.models import User

        stranger = Party.objects.create(company=self.company, kind="person", display_name="Stranger",
                                        origin="erp", user=User.objects.create_user("stranger"))
        PartyPhone.objects.create(party=stranger, number_raw="+1 312 555 0500", purpose="work")
        Customer.objects.filter(pk=self.customer.pk).update(account_holder=stranger, party=stranger)
        Party.objects.filter(pk=self.party.pk).update(user=None)
        for response in (self.client.get(ME), self.client.get(CONSENTS),
                         self.client.patch(ME, {"phones": [{"number": "312 555 0142"}]}, format="json"),
                         self.client.post(CONSENTS, {"channel": "sms", "state": "granted", "wording_version": "v1"},
                                          format="json")):
            self.assertEqual(response.status_code, 404, response.content)
            self.assertNotIn("Stranger", str(response.content))
        self.assertEqual(PartyPhone.objects.filter(party=stranger).count(), 1)
        self.assertFalse(ConsentRecord.objects.filter(party=stranger).exists())

    def test_consent_after_a_merge_writes_no_contradiction_on_the_merged_party(self):
        from django.utils import timezone

        survivor = Party.objects.create(company=self.company, kind="person", display_name="Mia Me",
                                        origin="erp")
        Party.objects.filter(pk=self.party.pk).update(merged_into=survivor, merged_at=timezone.now(), user=None)
        Party.objects.filter(pk=survivor.pk).update(user=self.customer.user)
        response = self.client.post(CONSENTS, {"channel": "email", "state": "granted", "wording_version": "v1"},
                                    format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(list(ConsentRecord.objects.values_list("party_id", "state")), [(survivor.pk, "granted")])
        # The flag still mirrors onto the merged party: it is left alone, so no
        # later customer save appends an evidence-less record there.
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.accepts_marketing)
        self.customer.first_name = "Mira"
        self.customer.save()
        self.assertEqual(list(ConsentRecord.objects.values_list("party_id", "state")), [(survivor.pk, "granted")])

    def test_holder_party_without_a_login_is_not_shown(self):
        other = Party.objects.create(company=self.company, kind="person", display_name="Nobody", origin="erp")
        Customer.objects.filter(pk=self.customer.pk).update(account_holder=other, party=other)
        Party.objects.filter(pk=self.party.pk).update(user=None)
        self.assertEqual(self.client.get(ME).status_code, 404)
