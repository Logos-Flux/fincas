"""The admin is a *writer* of journal entries, so it must uphold the same
invariants the importer does.

Neither invariant can be a database constraint — balance spans sibling rows,
and the company match is two FK hops away — so if the admin does not check
them, nothing does. Before these tests existed the admin would happily save a
1000-vs-1 entry, an entry with no lines at all, and a line pointing at another
company's account.

These post real form data through the real admin view. Asserting on
``JournalEntry.objects.count()`` rather than on the response body matters: a
Django admin validation failure re-renders with 200 and a successful save
redirects with 302, so a status-code-only assertion reads backwards.
"""

import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.forms.models import inlineformset_factory
from django.test import Client, TestCase
from django.urls import reverse

from accounting.admin import JournalLineInlineFormSet
from accounting.models import Account, JournalEntry, JournalLine
from core.models import Company

ADD_URL = "/admin/accounting/journalentry/add/"


class JournalEntryAdminTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.other = Company.objects.create(name="Voltage Labs")
        self.ar = Account.objects.create(
            company=self.company, name="Accounts Receivable",
            full_name="Accounts Receivable", account_type=Account.AccountType.ASSET,
        )
        self.sales = Account.objects.create(
            company=self.company, name="Sales", full_name="Sales",
            account_type=Account.AccountType.INCOME,
        )
        self.foreign = Account.objects.create(
            company=self.other, name="Foreign", full_name="Foreign",
            account_type=Account.AccountType.ASSET,
        )
        User.objects.create_superuser("root", "root@example.com", "pw")
        self.client = Client()
        self.client.login(username="root", password="pw")

    def _post(self, lines):
        data = {
            "entry_date": "2025-01-15",
            "entry_source": JournalEntry.EntrySource.MANUAL,
            "status": JournalEntry.Status.POSTED,
            "company": str(self.company.pk),
            "memo": "", "entry_number": "", "source_reference": "",
            "lines-TOTAL_FORMS": str(len(lines)),
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
        }
        for i, (account, debit, credit) in enumerate(lines):
            data.update({
                f"lines-{i}-id": "", f"lines-{i}-entry": "",
                f"lines-{i}-account": str(account.pk),
                f"lines-{i}-sort_order": str(i),
                f"lines-{i}-description": "", f"lines-{i}-entity_name": "",
                f"lines-{i}-debit_amount": debit,
                f"lines-{i}-credit_amount": credit,
            })
        return self.client.post(ADD_URL, data)

    def test_a_balanced_entry_saves_with_its_company_set(self):
        response = self._post([
            (self.ar, "100.00", "0.00"),
            (self.sales, "0.00", "100.00"),
        ])
        self.assertEqual(response.status_code, 302)  # redirect == saved
        self.assertEqual(JournalEntry.objects.count(), 1)

        entry = JournalEntry.objects.get()
        entry.validate_balanced()
        # `company` is a non-null FK and an ordinary editable field, so the add
        # form renders and saves it. Asserted explicitly because an entry that
        # somehow landed without one would break every scoped query.
        self.assertEqual(entry.company, self.company)
        self.assertEqual(
            set(entry.lines.values_list("account__company_id", flat=True)),
            {self.company.pk},
        )

    def test_an_unbalanced_entry_is_rejected(self):
        response = self._post([
            (self.ar, "1000.00", "0.00"),
            (self.sales, "0.00", "1.00"),
        ])
        self.assertEqual(response.status_code, 200)  # re-render == rejected
        self.assertEqual(JournalEntry.objects.count(), 0)
        self.assertContains(response, "does not balance", status_code=200)

    def test_an_entry_with_no_lines_is_rejected(self):
        response = self._post([])
        self.assertEqual(JournalEntry.objects.count(), 0)
        self.assertContains(response, "at least one line", status_code=200)

    def test_a_cross_company_line_is_rejected_on_the_add_flow(self):
        # The add flow is the one worth pinning: Django builds the *unsaved*
        # parent via save_form() before cleaning the formset, so the formset
        # does see `company`. If that ever stopped being true, the guard would
        # silently no-op on the exact path where entries are created.
        response = self._post([
            (self.ar, "50.00", "0.00"),
            (self.foreign, "0.00", "50.00"),
        ])
        self.assertEqual(response.status_code, 200)  # re-render == rejected
        self.assertEqual(JournalEntry.objects.count(), 0)
        self.assertEqual(JournalLine.objects.count(), 0)

    def test_the_add_form_scopes_the_account_dropdown_to_the_chosen_company(self):
        response = self.client.get(f"{ADD_URL}?company={self.company.pk}")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(str(self.ar.pk), body)
        self.assertNotIn(str(self.foreign.pk), body)

    def test_the_formset_itself_rejects_a_cross_company_line(self):
        # The dropdown scoping above is a usability guard — a hand-crafted POST
        # can still name any account id. This exercises the formset guard
        # directly, with an unscoped queryset, so the actual boundary stays
        # pinned even if the widget scoping changes.
        formset_class = inlineformset_factory(
            JournalEntry, JournalLine,
            formset=JournalLineInlineFormSet,
            fields=["account", "sort_order", "debit_amount", "credit_amount"],
            extra=0,
        )
        data = {
            "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        }
        for i, (account, debit, credit) in enumerate([
            (self.ar, "50.00", "0.00"), (self.foreign, "0.00", "50.00"),
        ]):
            data.update({
                f"lines-{i}-id": "", f"lines-{i}-account": str(account.pk),
                f"lines-{i}-sort_order": str(i),
                f"lines-{i}-debit_amount": debit,
                f"lines-{i}-credit_amount": credit,
            })
        unsaved = JournalEntry(
            company=self.company, entry_date=datetime.date(2025, 1, 15)
        )
        formset = formset_class(data, instance=unsaved, prefix="lines")

        self.assertFalse(formset.is_valid())
        self.assertIn(
            "different company", " ".join(formset.non_form_errors())
        )

    def test_a_multi_leg_balanced_entry_saves(self):
        tax = Account.objects.create(
            company=self.company, name="Sales Tax Payable",
            full_name="Sales Tax Payable",
            account_type=Account.AccountType.LIABILITY,
        )
        response = self._post([
            (self.ar, "108.25", "0.00"),
            (self.sales, "0.00", "100.00"),
            (tax, "0.00", "8.25"),
        ])
        self.assertEqual(response.status_code, 302)
        JournalEntry.objects.get().validate_balanced()

    def test_changelist_renders_the_balanced_column(self):
        entry = JournalEntry.objects.create(
            company=self.company, entry_date=datetime.date(2025, 1, 15)
        )
        entry.lines.create(account=self.ar, debit_amount=Decimal("10.00"))
        entry.lines.create(account=self.sales, credit_amount=Decimal("10.00"))
        response = self.client.get(reverse("admin:accounting_journalentry_changelist"))
        self.assertEqual(response.status_code, 200)


class AccountAdminTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        User.objects.create_superuser("root", "root@example.com", "pw")
        self.client = Client()
        self.client.login(username="root", password="pw")

    def test_changelist_renders(self):
        Account.objects.create(
            company=self.company, name="Checking", account_number="1000",
            account_type=Account.AccountType.ASSET,
            account_subtype=Account.AccountSubType.BANK,
        )
        response = self.client.get(reverse("admin:accounting_account_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Checking")
