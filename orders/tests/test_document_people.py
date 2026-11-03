"""People on quotes and orders (``DocumentContact``, bug batch 2026-10-15).

The people offered are the customer's own person and its contacts, each with
the title from their relationship to the customer's party.
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounting.tests.test_posting import make_posting_rules
from core.models import Company
from core.tests.helpers import make_staff_user
from customers.models import Customer, CustomerContact
from orders import people
from orders.convert import convert_quote_to_order
from orders.duplicate import duplicate_order, duplicate_quote
from orders.models import DocumentContact, Order, Quote


class PeopleTestBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="People Co")
        make_posting_rules(self.company)
        self.customer = Customer.objects.create(
            company=self.company, customer_type="business", company_name="ABC Supply",
            first_name="Bob", last_name="Buyer", email="bob@abc.example", job_title="Purchasing manager",
        )
        self.sue = CustomerContact.objects.create(
            customer=self.customer, first_name="Sue", last_name="Payable",
            email="sue@abc.example", job_title="Accounts payable",
        )
        self.jim = CustomerContact.objects.create(
            customer=self.customer, first_name="Jim", last_name="Owner",
            email="jim@abc.example", job_title="Owner",
        )
        self.quote = Quote.objects.create(company=self.company, customer=self.customer,
                                          grand_total=Decimal("10.00"))
        self.client.force_login(User.objects.create_superuser("boss", "boss@example.com", "pw"))

    def by_name(self, document):
        return {p.name: p for p in people.candidates(document)}


class CandidatesAndTitlesTests(PeopleTestBase):
    def test_the_customers_people_with_their_titles(self):
        found = self.by_name(self.quote)
        self.assertEqual(
            {name: p.title for name, p in found.items()},
            {"Bob Buyer": "Purchasing manager", "Sue Payable": "Accounts payable", "Jim Owner": "Owner"},
        )
        self.assertEqual(found["Sue Payable"].email, "sue@abc.example")

    def test_add_is_idempotent_and_remove_takes_it_off(self):
        sue = self.by_name(self.quote)["Sue Payable"]
        row = people.add(self.quote, sue.party_id)
        people.add(self.quote, str(sue.party_id))
        self.assertEqual(DocumentContact.objects.filter(quote=self.quote).count(), 1)
        self.assertEqual(row.company_id, self.company.pk)
        self.assertEqual([(p.name, p.title) for p in people.people(self.quote)],
                         [("Sue Payable", "Accounts payable")])
        self.assertTrue(people.remove(self.quote, row.pk))
        self.assertEqual(people.people(self.quote), [])

    def test_someone_elses_person_is_refused(self):
        other = Customer.objects.create(company=self.company, first_name="Zed", last_name="Else",
                                        email="zed@example.com")
        with self.assertRaises(ValidationError):
            people.add(self.quote, other.party_id)

    def test_a_document_holds_exactly_one_of_quote_or_order(self):
        from django.db import IntegrityError, transaction

        order = Order.objects.create(company=self.company, customer=self.customer)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DocumentContact.objects.create(company=self.company, quote=self.quote, order=order,
                                           party_id=self.sue.party_id)


class CarriedAlongTests(PeopleTestBase):
    def setUp(self):
        super().setUp()
        for name in ("Sue Payable", "Jim Owner"):
            people.add(self.quote, self.by_name(self.quote)[name].party_id)

    def names(self, document):
        return sorted(p.name for p in people.people(document))

    def test_quote_to_order(self):
        order, _ = convert_quote_to_order(self.quote)
        self.assertEqual(self.names(order), ["Jim Owner", "Sue Payable"])

    def test_duplicate_and_revision_copy_the_people(self):
        self.assertEqual(self.names(duplicate_quote(self.quote)), ["Jim Owner", "Sue Payable"])
        revision = duplicate_quote(self.quote, engagement=self.quote.engagement)
        self.assertEqual(self.names(revision), ["Jim Owner", "Sue Payable"])
        order, _ = convert_quote_to_order(self.quote)
        self.assertEqual(self.names(duplicate_order(order)), ["Jim Owner", "Sue Payable"])


class PartyMergeTests(PeopleTestBase):
    def test_merging_two_people_on_one_quote_keeps_one_row(self):
        from parties.merge import merge, unmerge

        found = self.by_name(self.quote)
        sue, jim = found["Sue Payable"], found["Jim Owner"]
        people.add(self.quote, sue.party_id)
        people.add(self.quote, jim.party_id)
        order = Order.objects.create(company=self.company, customer=self.customer)
        people.add(order, jim.party_id)
        from parties.models import Party

        (log,) = merge(Party.objects.get(pk=sue.party_id), [Party.objects.get(pk=jim.party_id)], actor="tester")
        self.assertEqual(list(DocumentContact.objects.filter(quote=self.quote).values_list("party_id", flat=True)),
                         [sue.party_id])
        self.assertEqual(list(DocumentContact.objects.filter(order=order).values_list("party_id", flat=True)),
                         [sue.party_id])
        unmerge(log, actor="tester")
        self.assertEqual(
            sorted(map(str, DocumentContact.objects.filter(quote=self.quote).values_list("party_id", flat=True))),
            sorted([str(sue.party_id), str(jim.party_id)]),
        )


class PeopleViewTests(PeopleTestBase):
    def url(self):
        return reverse("erp-quote-people", args=[self.quote.pk])

    def test_editor_shows_the_people_card(self):
        html = self.client.get(reverse("erp-quote-edit", args=[self.quote.pk])).content.decode()
        self.assertIn('id="doc-people"', html)
        self.assertIn('id="doc-people-form"', html)
        self.assertIn("Sue Payable — Accounts payable", html)

    def test_fetch_add_returns_the_card(self):
        sue = self.by_name(self.quote)["Sue Payable"]
        response = self.client.post(self.url(), {"party": str(sue.party_id), "add": "1"},
                                    HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Sue Payable", html)
        self.assertIn("Accounts payable", html)
        self.assertIn("data-people-remove", html)

    def test_plain_post_redirects_and_remove_works(self):
        row = people.add(self.quote, self.by_name(self.quote)["Jim Owner"].party_id)
        response = self.client.post(self.url(), {"remove": str(row.pk)})
        self.assertRedirects(response, reverse("erp-quote-edit", args=[self.quote.pk]),
                             fetch_redirect_response=False)
        self.assertFalse(DocumentContact.objects.filter(pk=row.pk).exists())

    def test_bad_input_is_an_error_not_a_crash(self):
        response = self.client.post(self.url(), {"party": "not-a-uuid", "add": "1"},
                                    HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(response.status_code, 200)
        self.assertIn("not one of this customer", response.content.decode())
        response = self.client.post(self.url(), {"remove": "junk"}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(response.status_code, 200)

    def test_another_tenants_staff_cannot_reach_it(self):
        from core.models import UserProfile

        outsider = make_staff_user("outsider", "out@example.com", "pw")
        other = Company.objects.create(name="Other Co")
        UserProfile.objects.update_or_create(user=outsider, defaults={"company": other})
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_order_editor_has_the_card_too(self):
        order = Order.objects.create(company=self.company, customer=self.customer)
        html = self.client.get(reverse("erp-order-edit", args=[order.pk])).content.decode()
        self.assertIn(reverse("erp-order-people", args=[order.pk]), html)


class WhoCountsTests(PeopleTestBase):
    def test_a_referral_is_not_one_of_the_customers_people(self):
        from parties.models import Party, PartyRelationship

        referrer = Party.objects.create(company=self.company, kind="person", given_name="Ray",
                                        family_name="Referrer")
        PartyRelationship.objects.create(from_party=referrer, to_party_id=self.customer.party_id,
                                         kind="referral")
        self.assertNotIn("Ray Referrer", self.by_name(self.quote))
        with self.assertRaises(ValidationError):
            people.add(self.quote, referrer.pk)

    def test_changing_the_customer_takes_the_old_people_off(self):
        people.add(self.quote, self.by_name(self.quote)["Sue Payable"].party_id)
        other = Customer.objects.create(company=self.company, first_name="Ola", last_name="Other",
                                        email="ola@example.com")
        Quote.objects.filter(pk=self.quote.pk).update(customer=other)
        self.quote.refresh_from_db()
        self.assertEqual(people.prune(self.quote), 1)
        self.assertEqual(people.people(self.quote), [])

    def test_copy_skips_people_who_are_not_the_targets_customers(self):
        people.add(self.quote, self.by_name(self.quote)["Sue Payable"].party_id)
        other = Customer.objects.create(company=self.company, first_name="Ola", last_name="Other",
                                        email="ola@example.com")
        target = Quote.objects.create(company=self.company, customer=other)
        people.copy(self.quote, target)
        self.assertEqual(people.people(target), [])


class EditorIntegrationTests(PeopleTestBase):
    def test_changing_the_customer_in_the_editor_prunes_the_people(self):
        from orders.models import QuoteLineItem
        from orders.tests.test_order_create_requires_line import harvest_inputs

        people.add(self.quote, self.by_name(self.quote)["Sue Payable"].party_id)
        QuoteLineItem.objects.create(quote=self.quote, sku="X", item_name="Item X", quantity=Decimal("1"),
                                     unit_price_ex_tax=Decimal("10.00"), line_total_ex_tax=Decimal("10.00"))
        other = Customer.objects.create(company=self.company, first_name="Ola", last_name="Other",
                                        email="ola@example.com")
        url = reverse("erp-quote-edit", args=[self.quote.pk])
        data = harvest_inputs(self.client.get(url).content.decode())
        data["_action"] = "save_continue"
        # The same customer leaves the people alone.
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual([p.name for p in people.people(self.quote)], ["Sue Payable"])
        data["customer"] = str(other.pk)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(people.people(self.quote), [])

    def test_the_people_endpoints_are_staff_only(self):
        self.client.logout()
        for name in ("erp-quote-people", "erp-order-people"):
            url = reverse(name, args=[self.quote.pk])
            self.assertEqual(self.client.get(url).status_code, 302)
            self.assertEqual(self.client.post(url, {"party": "x"}).status_code, 302)
        User.objects.create_user("shopper", password="pw")
        self.client.login(username="shopper", password="pw")
        url = reverse("erp-quote-people", args=[self.quote.pk])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"party": "x"}).status_code, 403)

    def test_the_card_costs_three_queries(self):
        people.add(self.quote, self.by_name(self.quote)["Sue Payable"].party_id)
        quote = Quote.objects.select_related("customer").get(pk=self.quote.pk)
        with self.assertNumQueries(3):
            on_doc, addable = people.card(quote)
        self.assertEqual([p.name for p in on_doc], ["Sue Payable"])
        self.assertEqual(sorted(p.name for p in addable), ["Bob Buyer", "Jim Owner"])
