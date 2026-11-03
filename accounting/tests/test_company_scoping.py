"""Company-scoping contract for accounting /erp/ views (journal entries).

The GL pages are staff-only and resolve the visible company via
``resolve_company``: a single Company row shows it; otherwise only a
superuser may name one with ``?company=<uuid>`` — staff ``?company=``
tampering is ignored (fail closed to an empty page, never another
company's ledger). Detail views filter ``company=`` on the resolved
company, so direct-PK guessing across tenants 404s.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from accounting.models import Account, JournalEntry, JournalLine
from core.models import Company


def make_entry(company, *, memo="entry"):
    entry = JournalEntry.objects.create(
        company=company,
        entry_date=datetime.date(2026, 1, 15),
        memo=memo,
        status=JournalEntry.Status.POSTED,
    )
    cash = Account.objects.create(
        company=company, name="Cash", account_type=Account.AccountType.ASSET,
    )
    revenue = Account.objects.create(
        company=company, name="Revenue", account_type=Account.AccountType.INCOME,
    )
    JournalLine.objects.create(
        entry=entry, account=cash, debit_amount=Decimal("100.00"),
    )
    JournalLine.objects.create(
        entry=entry, account=revenue, credit_amount=Decimal("100.00"),
    )
    return entry


class JournalEntryScopingTests(TestCase):
    def setUp(self):
        self.company_a = Company.objects.create(name="Example Supply, Inc")
        self.company_b = Company.objects.create(name="Rival, Inc")
        self.entry_b = make_entry(self.company_b, memo="B-secret")
        self.staff = User.objects.create_user("staff", "s@x.com", "pw", is_staff=True)
        self.superuser = User.objects.create_superuser("root", "r@x.com", "pw")

    def test_staff_company_tamper_is_ignored(self):
        """Staff ?company=<B> is not honoured: empty page, not B's ledger."""
        self.client.force_login(self.staff)
        url = reverse("erp-accounting-entries") + f"?company={self.company_b.pk}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "B-secret")

    def test_staff_cannot_guess_entry_pk(self):
        self.client.force_login(self.staff)
        url = reverse(
            "erp-accounting-entry-detail", args=[self.entry_b.pk]
        ) + f"?company={self.company_b.pk}"
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_staff_cannot_guess_entry_pk_without_company_param(self):
        self.client.force_login(self.staff)
        url = reverse("erp-accounting-entry-detail", args=[self.entry_b.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_superuser_with_company_param_sees_entry(self):
        self.client.force_login(self.superuser)
        url = reverse(
            "erp-accounting-entry-detail", args=[self.entry_b.pk]
        ) + f"?company={self.company_b.pk}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "B-secret")

    def test_anonymous_is_redirected_to_login(self):
        url = reverse("erp-accounting-entries")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/erp/login/", response["Location"])

    def test_non_staff_is_denied(self):
        plain = User.objects.create_user("plain", "p@x.com", "pw")
        self.client.force_login(plain)
        url = reverse("erp-accounting-entries")
        response = self.client.get(url)
        # StaffRequiredMixin denies via 302-to-login or 403 — never 200.
        self.assertIn(response.status_code, (302, 403))
