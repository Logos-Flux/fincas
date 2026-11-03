"""The party picker the engagement and contact forms share: ``erp-party-search``
(``parties.erp_views.PartySearchView``), ``parties.widgets.PartySearchInput``
and the "New counterparty" link that opens the party form in a new tab.

Two tenants throughout: staff never see or pick the other tenant's parties.
With two Companies a staff login does not auto-attach, so every user here
has an explicit ``UserProfile(company=...)`` (``CrmViewTestBase``).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from crm.models import Engagement, EngagementContact
from parties.models import Party, PartyEmail

from .helpers import organisation, person
from .test_erp_views import CrmViewTestBase


def email(party, address, primary=True):
    return PartyEmail.objects.create(party=party, email=address, purpose="work", is_primary=primary)


class PartySearchTests(CrmViewTestBase):
    url = reverse("erp-party-search")

    def search(self, q, **params):
        response = self.client.get(self.url, {"q": q, **params})
        self.assertEqual(response.status_code, 200)
        return response.json()["results"]

    def test_by_email_prefix_name_and_kind(self):
        email(self.ada, "ada@lovelace.example")
        email(self.org, "sales@acme.example")
        [hit] = self.search("ADA@love")
        self.assertEqual(hit, {"id": str(self.ada.pk), "label": "Ada Lovelace · ada@lovelace.example",
                               "name": "Ada Lovelace", "kind": "person", "kind_label": "Person",
                               "email": "ada@lovelace.example"})
        self.assertEqual([r["id"] for r in self.search("acme")], [str(self.org.pk)])
        self.assertEqual(self.search("acme", kind="person"), [])
        self.assertEqual(self.search("a"), [])  # too short

    def test_shows_the_matching_email_and_puts_an_exact_match_first(self):
        email(self.ada, "ada@lovelace.example")
        email(self.ada, "billing@lovelace.example", primary=False)
        zed = person(self.company, "Aaron", "Zed")
        email(zed, "billing@lovelace.example.org")
        results = self.search("billing@lovelace.example")
        self.assertEqual([r["id"] for r in results], [str(self.ada.pk), str(zed.pk)])
        self.assertEqual(results[0]["email"], "billing@lovelace.example")

    def test_skips_merged_and_erased_parties(self):
        merged = person(self.company, "Ada", "Twin")
        erased = person(self.company, "Ada", "Gone")
        Party.objects.filter(pk=merged.pk).update(merged_into=self.ada, merged_at=timezone.now())
        Party.objects.filter(pk=erased.pk).update(erased_at=timezone.now())
        self.assertEqual([r["id"] for r in self.search("Ada")], [str(self.ada.pk)])

    def test_never_another_tenants_parties(self):
        rival = person(self.other_company, "Ada", "Rival")
        email(rival, "ada@rival.example")
        self.assertEqual([r["id"] for r in self.search("ada")], [str(self.ada.pk)])
        self.assertEqual(self.search("ada@rival"), [])
        self.assertEqual(self.search("ada", company=self.other_company.pk), [])
        self.client.force_login(self.other_user)
        self.assertEqual([r["id"] for r in self.search("ada")], [str(rival.pk)])

    def test_staff_only(self):
        User.objects.create_user("shopper", password="pw")
        self.client.logout()
        self.assertEqual(self.client.get(self.url, {"q": "ada"}).status_code, 302)
        self.client.login(username="shopper", password="pw")
        self.assertEqual(self.client.get(self.url, {"q": "ada"}).status_code, 403)


class PartyPickerFormTests(CrmViewTestBase):
    def test_engagement_form_searches_and_offers_a_new_counterparty_tab(self):
        response = self.client.get(reverse("erp-crm-engagement-create"))
        self.assertContains(response, 'data-party-search="%s"' % reverse("erp-party-search"))
        self.assertContains(response, f'data-party-company="{self.company.pk}"')
        self.assertContains(response, f'href="{reverse("erp-party-create")}" target="_blank" rel="noopener"')
        self.assertContains(response, "New counterparty")

    def test_edit_shows_the_current_counterparty_by_name(self):
        email(self.org, "sales@acme.example")
        eng = self.eng()
        response = self.client.get(reverse("erp-crm-engagement-edit", args=[eng.pk]))
        self.assertContains(response, 'data-party-label="Acme Widgets · sales@acme.example"')
        # The submitted value stays the id, so resolve_party keeps working.
        self.assertContains(response, f'value="{self.org.pk}"')

    def test_a_picked_id_or_a_typed_email_saves_another_tenants_id_does_not(self):
        bob = person(self.company, "Bob", "Builder")
        email(bob, "bob@example.com")
        create = reverse("erp-crm-engagement-create")
        self.client.post(create, {"party": str(bob.pk), "currency": "USD", "title": "Picked"})
        self.client.post(create, {"party": "Bob@Example.com", "currency": "USD", "title": "Typed"})
        self.assertEqual(Engagement.objects.filter(company=self.company, party=bob).count(), 2)
        rival = organisation(self.other_company, "Rival Co")
        response = self.client.post(create, {"party": str(rival.pk), "currency": "USD"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Engagement.objects.filter(party=rival).exists())

    def test_contact_picker_searches_people(self):
        eng = self.eng()
        response = self.client.get(reverse("erp-crm-engagement-detail", args=[eng.pk]))
        self.assertContains(response, 'data-party-kind="person"')
        self.assertContains(response, f'href="{reverse("erp-party-create")}?kind=person" target="_blank"')
        add = reverse("erp-crm-engagement-contact-create", args=[eng.pk])
        self.client.post(add, {"party": str(self.ada.pk), "role": "champion"})
        self.assertTrue(EngagementContact.objects.filter(engagement=eng, party=self.ada).exists())
        self.client.post(add, {"party": str(organisation(self.company, "Other Org").pk), "role": "champion"})
        self.assertEqual(eng.contacts.count(), 1)

    def test_new_person_link_preselects_the_kind(self):
        response = self.client.get(reverse("erp-party-create"), {"kind": "person"})
        self.assertEqual(response.context["form"].initial.get("kind"), "person")
