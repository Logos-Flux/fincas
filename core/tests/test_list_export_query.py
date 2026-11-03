"""The list Export link carries the whole query string (CRM Phase 4 step 0).

It used to carry the filters but drop the search term and the sort, so the
CSV did not match the screen. The pager links had the same hand-built query
string, unencoded (a search containing ``&`` broke them).
"""

from __future__ import annotations

import csv
import html
import io
import re
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import Company, UserProfile
from customers.models import Customer


def _href(page, marker):
    """The unescaped href of the first <a> whose first child mentions ``marker``."""
    match = re.search(r'<a [^>]*?href="([^"]*)"[^>]*>\s*<i class="[^"]*' + re.escape(marker), page)
    if not match:
        raise AssertionError(f"no link with {marker!r}")
    return html.unescape(match.group(1))


class ExportQueryTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme Export")
        user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        UserProfile.objects.create(user=user, company=self.company)
        b2b = Customer.CustomerType.choices[0][0]
        other = Customer.CustomerType.choices[1][0]
        self.rows = [
            ("Ada", "Lovelace", "ada@example.com", b2b),
            ("Adam", "Lovell", "adam@example.com", b2b),
            ("Ada", "Lovegood", "lg@example.com", other),
            ("Grace", "Hopper", "grace@example.com", b2b),
        ]
        for first, last, email, kind in self.rows:
            Customer.objects.create(first_name=first, last_name=last, email=email,
                                    customer_type=kind, company=self.company)
        self.b2b = b2b
        self.client.force_login(user)

    def test_export_matches_the_screen(self):
        url = reverse("erp-customer-list")
        params = {"q": "Love", "customer_type": self.b2b, "sort": "email", "dir": "desc"}
        page = self.client.get(url, params)
        on_screen = [c.email for c in page.context["object_list"]]
        self.assertEqual(on_screen, ["adam@example.com", "ada@example.com"])

        href = _href(page.content.decode(), "fa-file-csv")
        query = parse_qs(urlsplit(href).query)
        self.assertEqual(query["q"], ["Love"])
        self.assertEqual(query["customer_type"], [self.b2b])
        self.assertEqual(query["sort"], ["email"])
        self.assertEqual(query["export"], ["csv"])

        export = self.client.get(url + href)
        self.assertEqual(export["Content-Type"], "text/csv")
        rows = list(csv.reader(io.StringIO(export.content.decode())))
        self.assertEqual([r[1] for r in rows[1:]], on_screen)

    def test_search_with_ampersand_survives_in_links(self):
        page = self.client.get(reverse("erp-customer-list"), {"q": "A&B"})
        href = _href(page.content.decode(), "fa-file-csv")
        self.assertEqual(parse_qs(urlsplit(href).query)["q"], ["A&B"])

    def test_party_export_keeps_search_and_sort(self):
        page = self.client.get(reverse("erp-party-list"),
                               {"q": "Love", "sort": "updated_at", "dir": "desc"})
        href = _href(page.content.decode(), "fa-file-csv")
        query = parse_qs(urlsplit(href).query)
        self.assertEqual((query["q"], query["sort"], query["export"]),
                         (["Love"], ["updated_at"], ["csv"]))

    def test_pager_links_keep_search_and_sort(self):
        for i in range(30):
            Customer.objects.create(first_name="P", last_name=f"Lovex{i:02d}",
                                    email=f"p{i}@example.com", company=self.company)
        page = self.client.get(reverse("erp-customer-list"),
                               {"q": "Love", "sort": "email", "dir": "asc"})
        self.assertTrue(page.context["is_paginated"])
        href = _href(page.content.decode(), "fa-angle-right")
        query = parse_qs(urlsplit(href).query)
        self.assertEqual((query["q"], query["sort"], query["dir"], query["page"]),
                         (["Love"], ["email"], ["asc"], ["2"]))
