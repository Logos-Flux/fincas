"""Sortable list headers are keyboard links with aria-sort (Phase 4 step 0).

They were ``<th onclick>``: not focusable, no state for assistive tech, and
dead without JavaScript.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import reverse

from core.models import Company, UserProfile
from core.views.list_view import ERPListView
from customers.models import Customer


def _thead(page):
    start = page.index('id="erp-list-table"')
    return page[start : page.index("</thead>", start)]


class SortHeaderMarkupTests(TestCase):
    def setUp(self):
        company = Company.objects.create(name="Acme Headers")
        user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        UserProfile.objects.create(user=user, company=company)
        Customer.objects.create(first_name="A", last_name="B", email="a@example.com",
                                company=company)
        self.client.force_login(user)

    def test_headers_are_links_with_aria_sort_on_the_sorted_column(self):
        page = self.client.get(reverse("erp-customer-list"),
                               {"q": "B", "sort": "email", "dir": "desc", "page": "1"})
        thead = _thead(page.content.decode())
        self.assertNotIn("onclick", thead)
        ths = re.findall(r"<th [^>]*>.*?</th>", thead, re.S)
        sorted_ths = [th for th in ths if "aria-sort=" in th]
        self.assertEqual(len(sorted_ths), 1)
        self.assertIn('aria-sort="descending"', sorted_ths[0])
        self.assertIn('data-col-id="email"', sorted_ths[0])  # the sort id, not the cell's path
        for th in ths:
            self.assertIn('scope="col"', th)

        links = {
            parse_qs(html.unescape(href).lstrip("?"))["sort"][0]: parse_qs(html.unescape(href).lstrip("?"))
            for href in re.findall(r'<a href="([^"]*)" class="text-reset erp-sort-link"', thead)
        }
        self.assertEqual(set(links), {"full_name", "email", "company_name", "customer_type",
                                      "pricing_tier", "is_active"})
        self.assertEqual(links["email"]["dir"], ["asc"])  # toggles
        self.assertEqual(links["full_name"]["dir"], ["asc"])
        self.assertEqual(links["email"]["q"], ["B"])  # keeps the search
        self.assertNotIn("page", links["email"])  # a new sort starts at page 1
        # The icon is decoration; the state is in aria-sort.
        self.assertNotRegex(thead, r'<i class="fas fa-sort[^"]*"(?![^>]*aria-hidden)')

    def test_no_aria_sort_without_a_sort(self):
        thead = _thead(self.client.get(reverse("erp-customer-list")).content.decode())
        self.assertNotIn("aria-sort", thead)

    def test_unsortable_column_renders_plain_text(self):
        class V(ERPListView):
            model = Customer
            list_columns = [{"field": "full_name", "label": "Name"},
                            {"field": "email", "label": "Email"}]

        view = V()
        view.request = RequestFactory().get("/", {"sort": "email"})
        name, email = view.get_header_columns("email", "asc")
        self.assertFalse(name["sortable"])
        self.assertNotIn("sort_query", name)
        self.assertEqual(email["aria_sort"], "ascending")
        self.assertEqual(parse_qs(email["sort_query"])["dir"], ["desc"])

    def test_party_list_headers_carry_aria_sort(self):
        page = self.client.get(reverse("erp-party-list"), {"sort": "updated_at", "dir": "desc"})
        content = page.content.decode()
        start = content.index('id="party-list-table"')
        thead = content[start : content.index("</thead>", start)]
        self.assertEqual(thead.count("aria-sort="), 1)
        self.assertIn('aria-sort="descending"', thead)
