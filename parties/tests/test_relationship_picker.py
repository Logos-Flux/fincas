"""Adding a relationship: the other party is searched (email or name), and the
customer profile reaches the same form through its Relationships card.

Two tenants throughout: the search never returns, and the form never accepts,
the other tenant's parties.
"""

from __future__ import annotations

from urllib.parse import quote

from django.urls import reverse
from django.utils import timezone

from customers.models import Customer
from parties.models import Party, PartyEmail, PartyRelationship

from .test_erp_views import PartyViewTestBase, organisation, person

SEARCH = reverse("erp-party-search")


def email(party, address, primary=True):
    return PartyEmail.objects.create(party=party, email=address, purpose="work", is_primary=primary)


class RelationshipSearchTests(PartyViewTestBase):
    def search(self, q, **params):
        response = self.client.get(SEARCH, {"q": q, **params})
        self.assertEqual(response.status_code, 200)
        return response.json()["results"]

    def ids(self, q, **params):
        return [r["id"] for r in self.search(q, **params)]

    def test_by_any_email_the_party_holds(self):
        ada = person(self.company)
        email(ada, "ada@lovelace.example")
        email(ada, "accounts@engines.example", primary=False)
        [hit] = self.search("accounts@engines.example")
        self.assertEqual(hit["id"], str(ada.pk))
        self.assertEqual(hit["email"], "accounts@engines.example")  # the address that matched
        self.assertEqual(self.ids("ACCOUNTS@eng"), [str(ada.pk)])

    def test_by_name_words_in_any_order(self):
        ada = person(self.company)
        org = organisation(self.company, "Analytical Engines", legal_name="Babbage Holdings Ltd")
        self.assertEqual(self.ids("lovelace ada"), [str(ada.pk)])
        self.assertEqual(self.ids("Lovel"), [str(ada.pk)])
        self.assertEqual(self.ids("babbage holdings"), [str(org.pk)])  # legal name
        self.assertEqual(self.ids("ada hopper"), [])  # every word must match

    def test_leaves_out_the_excluded_party(self):
        ada = person(self.company)
        twin = person(self.company, "Ada", "Byron")
        self.assertEqual(set(self.ids("ada")), {str(ada.pk), str(twin.pk)})
        self.assertEqual(self.ids("ada", exclude=ada.pk), [str(twin.pk)])
        self.assertEqual(len(self.ids("ada", exclude="not-a-uuid")), 2)

    def test_never_another_tenants_parties(self):
        ada = person(self.company)
        rival = person(self.other_company, "Ada", "Rival")
        email(rival, "ada@rival.example", primary=False)
        self.assertEqual(self.ids("ada"), [str(ada.pk)])
        self.assertEqual(self.ids("ada@rival.example"), [])
        self.assertEqual(self.ids("rival ada", company=self.other_company.pk), [])

    def test_skips_merged_and_erased_parties(self):
        ada = person(self.company)
        gone = person(self.company, "Ada", "Gone")
        merged = person(self.company, "Ada", "Twin")
        Party.objects.filter(pk=gone.pk).update(erased_at=timezone.now())
        Party.objects.filter(pk=merged.pk).update(merged_into=ada, merged_at=timezone.now())
        self.assertEqual(self.ids("ada"), [str(ada.pk)])


class RelationshipFormPickerTests(PartyViewTestBase):
    def test_form_renders_the_party_search(self):
        ada = person(self.company)
        response = self.client.get(reverse("erp-party-relationship-create", args=[ada.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-party-search="%s"' % SEARCH)
        self.assertContains(response, 'data-party-company="%s"' % self.company.pk)
        self.assertContains(response, 'data-party-exclude="%s"' % ada.pk)
        self.assertContains(response, "input[data-party-search]")  # the picker script is loaded
        self.assertContains(response, "Search by email or name")

    def test_post_refuses_another_tenants_party_id(self):
        ada = person(self.company)
        rival_org = organisation(self.other_company, "Rival Works")
        response = self.client.post(reverse("erp-party-relationship-create", args=[ada.pk]),
                                    {"kind_side": "employment:from", "other_party": str(rival_org.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertIn("other_party", response.context["form"].errors)
        self.assertFalse(PartyRelationship.objects.exists())

    def test_post_refuses_the_party_itself(self):
        ada = person(self.company)
        response = self.client.post(reverse("erp-party-relationship-create", args=[ada.pk]),
                                    {"kind_side": "referral:from", "other_party": str(ada.pk)})
        self.assertIn("other_party", response.context["form"].errors)


class CustomerRelationshipsTests(PartyViewTestBase):
    def setUp(self):
        super().setUp()
        self.customer = Customer.objects.create(company=self.company, first_name="Cy", last_name="Buyer",
                                                email="cy@example.com")
        self.party = self.customer.party
        self.detail = reverse("erp-customer-detail", args=[self.customer.pk])
        self.back = self.detail + "#relationships"
        self.create = reverse("erp-party-relationship-create", args=[self.party.pk])

    def test_customer_page_shows_the_relationships_card(self):
        org = organisation(self.company)
        PartyRelationship.objects.create(from_party=self.party, to_party=org, kind="employment",
                                         title="Buyer", source="erp")
        response = self.client.get(self.detail)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="relationships"')
        self.assertContains(response, reverse("erp-party-detail", args=[self.party.pk]))
        self.assertContains(response, f"{self.create}?next={quote(self.back, safe='/')}")
        self.assertContains(response, "Analytical Engines")
        rel = PartyRelationship.objects.get()
        self.assertContains(response, reverse("erp-party-relationship-edit", args=[rel.pk]))

    def test_add_from_the_customer_page_returns_to_it(self):
        org = organisation(self.company)
        email(org, "hq@engines.example", primary=False)
        url = f"{self.create}?next={quote(self.back, safe='/')}"
        page = self.client.get(url)
        self.assertContains(page, f'name="next" value="{self.back}"')
        response = self.client.post(url, {"kind_side": "employment:from", "other_party": str(org.pk),
                                          "title": "Buyer", "next": self.back})
        self.assertRedirects(response, self.back, fetch_redirect_response=False)
        rel = PartyRelationship.objects.get()
        self.assertEqual((rel.from_party, rel.to_party, rel.kind, rel.source),
                         (self.party, org, "employment", "erp"))
        # And it is listed on the customer page.
        self.assertContains(self.client.get(self.detail), "Analytical Engines")

    def test_edit_from_the_customer_page_returns_to_it(self):
        org = organisation(self.company)
        rel = PartyRelationship.objects.create(from_party=self.party, to_party=org, kind="employment",
                                               source="erp")
        url = reverse("erp-party-relationship-edit", args=[rel.pk]) + f"?party={self.party.pk}"
        response = self.client.post(url, {"title": "Head buyer", "next": self.back})
        self.assertRedirects(response, self.back, fetch_redirect_response=False)
        rel.refresh_from_db()
        self.assertEqual(rel.title, "Head buyer")

    def test_offsite_next_is_ignored(self):
        org = organisation(self.company)
        response = self.client.post(self.create, {"kind_side": "employment:from", "other_party": str(org.pk),
                                                  "next": "https://evil.example/"})
        self.assertRedirects(response, reverse("erp-party-detail", args=[self.party.pk]) + "#relationships",
                             fetch_redirect_response=False)
