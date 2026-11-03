"""Shared list cells: booleans render as Yes/No badges, dates without time.

Regression guards for /qa ISSUE-002/004 (2026-09-25): ACTIVE columns showed
literal "True" text, and DATE columns showed "Sept. 25, 2026, midnight"
(Django's DATETIME_FORMAT renders 00:00 as "midnight"). Both are fixed in
the shared list renderer (golden_erp/templates/erp/list.html + bool_badge filter), so
one test per symptom covers every list that uses the flags.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import Company, UserProfile
from core.templatetags.erp_tags import bool_badge
from customers.models import Customer


class BoolBadgeFilterTests(TestCase):
    def test_true_false(self):
        self.assertEqual(bool_badge(True), "yes")
        self.assertEqual(bool_badge(False), "no")

    def test_non_booleans_fall_through_to_plain_text(self):
        # isinstance-strict: numeric 1/0 and "True" strings must NOT badge.
        for value in (1, 0, "True", "False", None, ""):
            self.assertEqual(bool_badge(value), "", f"{value!r} must not badge")


class ListCellDisplayTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="ada@example.com", company=self.company, is_active=True,
        )
        self.user = User.objects.create_user(
            username="staff", password="pw", is_staff=True,
        )
        self.user.userprofile = UserProfile.objects.create(
            company=self.company,
        )
        self.client.force_login(self.user)

    def test_active_column_badges_instead_of_raw_true(self):
        response = self.client.get(reverse("erp-customer-list"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(">Yes<", html)
        self.assertNotIn(">True<", html)

    def test_order_date_has_no_midnight_word(self):
        from orders.models import Order

        Order.objects.create(customer=self.customer, company=self.company)
        response = self.client.get(reverse("erp-order-list"))
        self.assertEqual(response.status_code, 200)
        # Only the data table: the page also embeds the theme registry, and a
        # theme may be called "Midnight".
        html = response.content.decode()
        start = html.index('id="erp-list-table"')
        table = html[start : html.index("</table>", start)]
        self.assertNotIn("midnight", table.lower())
