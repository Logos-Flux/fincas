"""``manage.py check_money`` (docs/release-hardening-plan.md 7.2).

The comparator recomputes totals inside a rolled-back transaction, so every
test also asserts the tampered row is still tampered afterwards: the command
reports, it never repairs.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from decimal import Decimal

from django.core.management import call_command
from django.db.models import F
from django.test import TestCase

from accounting.models import JournalEntry, JournalLine
from accounting.tests.test_posting import make_accounts, make_posting_rules
from core.integrity import CAUSES, compare_totals
from core.management import release_exit_codes as codes
from core.models import Company
from customers.models import Customer
from orders.convert import convert_order_to_invoice
from orders.models import Order, OrderLineItem, Quote, QuoteLineItem, SalesInvoice
from payments.models import Payment


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        call_command("check_money", *args, stdout=out, stderr=err)
    except SystemExit as exc:
        code = exc.code
    return code, out.getvalue()


class MoneyFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        make_posting_rules(cls.company)
        cls.customer = Customer.objects.create(
            first_name="A", last_name="B", email="money@example.com", company=cls.company,
        )
        cls.order = Order.objects.create(
            customer=cls.customer, company=cls.company,
            shipping_cost_ex_tax=Decimal("10.00"),
        )
        OrderLineItem.objects.create(
            order=cls.order, item_name="A", sku="A-1", quantity=Decimal("2"),
            unit_price_ex_tax=Decimal("50.00"), line_total_ex_tax=Decimal("100.00"),
            line_tax=Decimal("5.00"),
        )
        OrderLineItem.objects.create(
            order=cls.order, item_name="B", sku="B-1", quantity=Decimal("3"),
            unit_price_ex_tax=Decimal("20.00"), line_total_ex_tax=Decimal("60.00"),
        )
        cls.order.refresh_from_db()
        cls.invoice, _ = convert_order_to_invoice(cls.order)
        cls.invoice.status = SalesInvoice.Status.SENT  # posts the GL entry
        cls.invoice.save()
        cls.quote = Quote.objects.create(customer=cls.customer, company=cls.company)
        QuoteLineItem.objects.create(
            quote=cls.quote, item_name="Q", sku="Q-1", quantity=Decimal("1"),
            unit_price_ex_tax=Decimal("30.00"), line_total_ex_tax=Decimal("30.00"),
        )
        cls.quote.refresh_from_db()

    def assertStillTampered(self, model, pk, field, value):
        self.assertEqual(model.objects.filter(pk=pk).values_list(field, flat=True).get(), value)


class CleanTests(MoneyFixture):
    def test_fixture_totals_are_what_the_code_computes(self):
        self.assertEqual(self.order.grand_total, Decimal("175.00"))
        self.assertEqual(self.invoice.grand_total, Decimal("175.00"))
        self.assertTrue(JournalEntry.objects.filter(company=self.company).exists())

    def test_clean_database_exits_0(self):
        code, out = run()
        self.assertEqual(code, codes.OK, out)
        self.assertIn("STATUS: OK (exit 0)", out)
        self.assertIn("order=1", out)
        self.assertIn("invoice=1", out)
        self.assertIn("quote=1", out)
        self.assertIn("every stored total equals the recomputed one", out)

    def test_json(self):
        code, out = run("--json")
        self.assertEqual(code, 0)
        body = json.loads(out)
        self.assertEqual(body["status"], "OK")
        self.assertEqual(body["buckets"], [])
        self.assertEqual(body["examined"]["order"], 1)
        [tb] = body["trial_balance"]
        self.assertEqual(tb["difference"], "0.00")
        self.assertEqual(tb["unbalanced_entries"], 0)

    def test_limit_caps_the_documents_examined(self):
        Order.objects.create(customer=self.customer, company=self.company)
        result = compare_totals(limit=1)
        self.assertEqual(result.examined["order"], 1)


class TamperedTotalTests(MoneyFixture):
    def test_posted_invoice_off_by_more_than_one_unit_rolls_back(self):
        SalesInvoice.objects.filter(pk=self.invoice.pk).update(
            grand_total=F("grand_total") + 5, balance_due=F("balance_due") + 5,
        )
        code, out = run()
        self.assertEqual(code, codes.ROLL_BACK, out)
        self.assertIn("invoice_order_drift", out)
        self.assertIn(self.invoice.invoice_number, out)
        self.assertIn("STATUS: ROLL BACK (exit 20)", out)
        self.assertStillTampered(SalesInvoice, self.invoice.pk, "grand_total", Decimal("180.00"))
        [bucket] = json.loads(run("--json")[1])["buckets"]
        self.assertEqual(bucket["status"], "ROLL BACK")
        self.assertEqual(bucket["posted_over_unit"], 1)
        self.assertFalse(bucket["explained"])

    def test_tampered_grand_total_alone_also_breaks_the_balance_arithmetic(self):
        SalesInvoice.objects.filter(pk=self.invoice.pk).update(grand_total=F("grand_total") + 5)
        code, out = run("--json")
        self.assertEqual(code, codes.ROLL_BACK, out)
        [check] = json.loads(out)["invoice_balances"]
        self.assertEqual(check["cause"], "balance_arithmetic")

    def test_posted_invoice_off_by_one_unit_is_degraded(self):
        SalesInvoice.objects.filter(pk=self.invoice.pk).update(
            grand_total=F("grand_total") + Decimal("0.01"),
            balance_due=F("balance_due") + Decimal("0.01"),
        )
        code, out = run("--json")
        self.assertEqual(code, codes.DEGRADED, out)
        [bucket] = json.loads(out)["buckets"]
        self.assertEqual((bucket["doc_type"], bucket["cause"]), ("invoice", "invoice_order_drift"))
        self.assertEqual(bucket["posted_over_unit"], 0)
        self.assertEqual(bucket["max_abs_diff"], "0.01")

    def test_draft_invoice_is_never_s1(self):
        SalesInvoice.objects.filter(pk=self.invoice.pk).update(
            grand_total=F("grand_total") + 5, balance_due=F("balance_due") + 5,
            status=SalesInvoice.Status.DRAFT,
        )
        code, _ = run()
        self.assertEqual(code, codes.DEGRADED)

    def test_paid_invoice_whose_order_moved_is_a_historical_bucket(self):
        SalesInvoice.objects.filter(pk=self.invoice.pk).update(
            status=SalesInvoice.Status.PAID, amount_paid=F("grand_total"), balance_due=0,
        )
        # A line added after settlement: the order's true total moves, the
        # paid invoice keeps its snapshot (recalculate_totals refuses it).
        OrderLineItem.objects.create(
            order=self.order, item_name="C", sku="C-1", quantity=Decimal("1"),
            unit_price_ex_tax=Decimal("5.00"), line_total_ex_tax=Decimal("5.00"),
        )
        self.assertEqual(SalesInvoice.objects.get(pk=self.invoice.pk).grand_total, Decimal("175.00"))
        code, out = run("--json")
        body = json.loads(out)
        self.assertEqual(code, codes.DEGRADED, out)
        [bucket] = body["buckets"]
        self.assertEqual((bucket["doc_type"], bucket["cause"]), ("invoice", "finalized_snapshot"))
        self.assertEqual(bucket["max_abs_diff"], "5.00")
        self.assertTrue(bucket["historical"])
        # Paid by hand (no Payment row): named, never S1.
        [check] = body["invoice_balances"]
        self.assertEqual(check["cause"], "no_payment_rows")

    def test_tampered_order_total_is_unexplained_and_degraded(self):
        Order.objects.filter(pk=self.order.pk).update(grand_total=F("grand_total") + 5)
        code, out = run()
        self.assertEqual(code, codes.DEGRADED, out)
        self.assertIn("unexplained", out)
        self.assertIn(self.order.order_number, out)
        self.assertStillTampered(Order, self.order.pk, "grand_total", Decimal("180.00"))
        # The invoice is compared with the order's recomputed total, which it
        # still matches: only the order row is wrong.
        self.assertNotIn("invoice_order_drift", out)

    def test_rounding_bucket(self):
        Quote.objects.filter(pk=self.quote.pk).update(grand_total=F("grand_total") + Decimal("0.01"))
        code, out = run("--json")
        self.assertEqual(code, codes.DEGRADED)
        [bucket] = json.loads(out)["buckets"]
        self.assertEqual((bucket["doc_type"], bucket["cause"]), ("quote", "rounding"))
        self.assertTrue(bucket["historical"])

    def test_legacy_import_bucket(self):
        Order.objects.filter(pk=self.order.pk).update(
            order_number="SO-BC123", grand_total=F("grand_total") + 7,
        )
        SalesInvoice.objects.filter(pk=self.invoice.pk).update(
            grand_total=F("grand_total") + 7, balance_due=F("balance_due") + 7,
        )
        code, out = run("--json")
        self.assertEqual(code, codes.DEGRADED)
        causes = {(b["doc_type"], b["cause"]) for b in json.loads(out)["buckets"]}
        self.assertIn(("order", "legacy_import"), causes)
        # The invoice of an imported order is explained by the same cause.
        self.assertIn(("invoice", "legacy_import"), causes)

    def test_posted_invoice_of_an_imported_order_is_never_s1(self):
        Order.objects.filter(pk=self.order.pk).update(bc_id="123")
        SalesInvoice.objects.filter(pk=self.invoice.pk).update(
            grand_total=F("grand_total") + 5, balance_due=F("balance_due") + 5,
        )
        code, out = run("--json")
        self.assertEqual(code, codes.DEGRADED, out)
        [bucket] = json.loads(out)["buckets"]
        self.assertEqual(bucket["cause"], "legacy_import")
        self.assertEqual(bucket["posted_over_unit"], 1)

    def test_stale_line_total_bucket(self):
        line = self.order.line_items.first()
        OrderLineItem.objects.filter(pk=line.pk).update(line_total_ex_tax=F("line_total_ex_tax") + 9)
        Order.objects.filter(pk=self.order.pk).update(
            subtotal_ex_tax=F("subtotal_ex_tax") + 9, grand_total=F("grand_total") + 9,
        )
        code, out = run("--json")
        self.assertEqual(code, codes.DEGRADED)
        causes = {(b["doc_type"], b["cause"]) for b in json.loads(out)["buckets"]}
        self.assertIn(("order", "line_totals_stale"), causes)
        self.assertStillTampered(OrderLineItem, line.pk, "line_total_ex_tax", Decimal("109.00"))

    def test_no_lines_bucket(self):
        empty = Order.objects.create(customer=self.customer, company=self.company)
        Order.objects.filter(pk=empty.pk).update(
            subtotal_ex_tax=Decimal("40.00"), grand_total=Decimal("40.00"),
        )
        _, out = run("--json")
        causes = {(b["doc_type"], b["cause"]) for b in json.loads(out)["buckets"]}
        self.assertIn(("order", "no_lines"), causes)

    def test_coupon_excess_bucket(self):
        Order.objects.filter(pk=self.order.pk).update(coupon_discount=Decimal("15.00"))
        _, out = run("--json")
        [bucket] = json.loads(out)["buckets"]
        self.assertEqual((bucket["doc_type"], bucket["cause"]), ("order", "coupon_excess"))
        self.assertTrue(bucket["historical"])

    def test_web_checkout_is_not_legacy(self):
        Order.objects.filter(pk=self.order.pk).update(
            external_source="web_checkout", balance_due=Decimal("100.00"),
        )
        _, out = run("--json")
        [bucket] = json.loads(out)["buckets"]
        self.assertEqual((bucket["doc_type"], bucket["cause"]), ("order", "balance_not_refreshed"))
        self.assertEqual(bucket["fields"], {"balance_due": 1})

    def test_bigcommerce_source_is_legacy(self):
        Order.objects.filter(pk=self.order.pk).update(
            external_source="bigcommerce", balance_due=Decimal("100.00"),
        )
        _, out = run("--json")
        [bucket] = json.loads(out)["buckets"]
        self.assertEqual(bucket["cause"], "legacy_import")

    def test_discount_applied_twice_bucket(self):
        # 10% of 30 = 3; the writer subtracted it twice: 30 - 3 - 3.
        Quote.objects.filter(pk=self.quote.pk).update(
            discount_percent=Decimal("10.00"), discount_amount=Decimal("3.00"),
            grand_total=Decimal("24.00"),
        )
        _, out = run("--json")
        [bucket] = json.loads(out)["buckets"]
        self.assertEqual((bucket["doc_type"], bucket["cause"]), ("quote", "discount_double_applied"))
        self.assertEqual(bucket["max_abs_diff"], "3.00")
        self.assertEqual(bucket["fields"], {"grand_total": 1})

    def test_tax_stale_bucket(self):
        Order.objects.filter(pk=self.order.pk).update(subtotal_inc_tax=F("subtotal_inc_tax") + 21)
        _, out = run("--json")
        [bucket] = json.loads(out)["buckets"]
        self.assertEqual((bucket["doc_type"], bucket["cause"]), ("order", "tax_stale"))

    def test_cancelled_order_with_a_zeroed_balance(self):
        Order.objects.filter(pk=self.order.pk).update(
            status=Order.Status.CANCELLED, balance_due=Decimal("0.00"),
        )
        _, out = run("--json")
        [bucket] = json.loads(out)["buckets"]
        self.assertEqual((bucket["doc_type"], bucket["cause"]), ("order", "cancelled_balance_zeroed"))

    def test_a_document_is_counted_under_both_its_header_and_balance_cause(self):
        Order.objects.filter(pk=self.order.pk).update(
            status=Order.Status.CANCELLED, balance_due=Decimal("0.00"),
            subtotal_inc_tax=F("subtotal_inc_tax") + 21,
        )
        _, out = run("--json")
        causes = {(b["doc_type"], b["cause"]) for b in json.loads(out)["buckets"]}
        self.assertEqual(causes, {("order", "tax_stale"), ("order", "cancelled_balance_zeroed")})

    def test_every_cause_is_described(self):
        for cause in CAUSES.values():
            self.assertTrue(cause.description)


class InvoiceBalanceTests(MoneyFixture):
    def test_balance_arithmetic_on_a_posted_invoice_rolls_back(self):
        SalesInvoice.objects.filter(pk=self.invoice.pk).update(balance_due=F("balance_due") - 20)
        code, out = run()
        self.assertEqual(code, codes.ROLL_BACK, out)
        self.assertIn("balance_arithmetic", out)
        self.assertStillTampered(SalesInvoice, self.invoice.pk, "balance_due", Decimal("155.00"))

    def test_amount_paid_without_payment_rows_is_explained(self):
        SalesInvoice.objects.filter(pk=self.invoice.pk).update(
            amount_paid=Decimal("175.00"), balance_due=Decimal("0.00"),
        )
        code, out = run("--json")
        self.assertEqual(code, codes.DEGRADED, out)
        [check] = json.loads(out)["invoice_balances"]
        self.assertEqual(check["cause"], "no_payment_rows")
        self.assertTrue(check["historical"])

    def test_settled_payments_that_disagree_roll_back(self):
        Payment.objects.create(
            order=self.order, invoice=self.invoice, company=self.company,
            payment_method=Payment.PaymentMethod.CASH,
            payment_provider=Payment.PaymentProvider.MANUAL, amount=Decimal("100.00"),
            status=Payment.Status.CAPTURED,
        )
        SalesInvoice.objects.filter(pk=self.invoice.pk).update(
            amount_paid=Decimal("75.00"), balance_due=Decimal("100.00"),
        )
        code, out = run("--json")
        self.assertEqual(code, codes.ROLL_BACK, out)
        [check] = json.loads(out)["invoice_balances"]
        self.assertEqual(check["cause"], "ledger_mismatch")
        self.assertEqual(check["posted_over_unit"], 1)

    def test_settled_payments_that_agree_pass(self):
        Payment.objects.create(
            order=self.order, invoice=self.invoice, company=self.company,
            payment_method=Payment.PaymentMethod.CASH,
            payment_provider=Payment.PaymentProvider.MANUAL, amount=Decimal("100.00"),
            convenience_fee=Decimal("3.00"), status=Payment.Status.SETTLED,
        )
        Payment.objects.create(  # a failed attempt never applies
            order=self.order, invoice=self.invoice, company=self.company,
            payment_method=Payment.PaymentMethod.CASH,
            payment_provider=Payment.PaymentProvider.MANUAL, amount=Decimal("50.00"),
            status=Payment.Status.FAILED,
        )
        SalesInvoice.objects.filter(pk=self.invoice.pk).update(
            amount_paid=Decimal("97.00"), balance_due=Decimal("78.00"),
        )
        code, out = run()
        self.assertEqual(code, codes.OK, out)


class TrialBalanceTests(MoneyFixture):
    def test_unbalanced_posted_entry_rolls_back(self):
        accounts = make_accounts(self.company)
        entry = JournalEntry.objects.create(
            company=self.company, entry_date=dt.date(2026, 1, 15), entry_number="JE-BAD",
            status=JournalEntry.Status.POSTED,
        )
        JournalLine.objects.bulk_create([
            JournalLine(entry=entry, account=accounts["1200"], debit_amount=Decimal("10.00")),
            JournalLine(entry=entry, account=accounts["4010"], credit_amount=Decimal("7.00")),
        ])
        code, out = run("--json")
        self.assertEqual(code, codes.ROLL_BACK, out)
        [tb] = json.loads(out)["trial_balance"]
        self.assertEqual(tb["difference"], "3.00")
        self.assertEqual(tb["unbalanced_entries"], 1)
        self.assertEqual(tb["unbalanced_samples"], ["JE-BAD"])
        self.assertEqual(tb["status"], "ROLL BACK")

    def test_draft_entries_are_not_in_the_trial_balance(self):
        accounts = make_accounts(self.company)
        entry = JournalEntry.objects.create(
            company=self.company, entry_date=dt.date(2026, 1, 15), entry_number="JE-DRAFT",
            status=JournalEntry.Status.DRAFT,
        )
        JournalLine.objects.bulk_create([
            JournalLine(entry=entry, account=accounts["1200"], debit_amount=Decimal("10.00")),
        ])
        code, _ = run()
        self.assertEqual(code, codes.OK)

    def test_every_company_is_listed_even_without_entries(self):
        Company.objects.create(name="Tenant B")
        _, out = run("--json")
        rows = json.loads(out)["trial_balance"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["status"] for r in rows}, {"OK"})
