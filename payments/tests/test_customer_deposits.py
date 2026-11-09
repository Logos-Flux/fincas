"""Customer deposits (0.4.4) and the order settlement of API payments.

A web (card) sale: the payment is captured before there is an invoice. With a
``deposit_account`` on the payment rule it is a customer deposit (DR processor
clearing / CR deposits); the order is converted to its invoice at shipment,
the deposit is linked to it, and issuing the invoice moves the deposit to A/R
(DR deposits / CR A/R). Every payment here goes through the REST endpoint the
storefront calls, and the assertions read journal lines and account balances.

Without a deposit account the postings must be exactly what 0.4.1 wrote.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.models import Sum
from django.test import TestCase
from rest_framework.test import APIClient

from accounting.models import (
    Account,
    DocumentPostingRule,
    JournalEntry,
    JournalLine,
    PaymentProviderPostingOverride,
)
from accounting.posting import post_balanced_entry
from accounting.tests.test_posting import make_posting_rules
from core import integrity
from core.models import Company
from customers.models import Customer
from orders.models import Order, SalesInvoice
from orders.tests.test_recalculate import company_user

from ..models import Payment
from ..posting import active_application
from ..services import refund_payment

PAYMENTS = "/api/v1/payments/payments/"
D = Decimal


def _balance(account) -> Decimal:
    """Debit-positive balance over posted entries."""
    totals = JournalLine.objects.filter(
        account=account, entry__status=JournalEntry.Status.POSTED
    ).aggregate(d=Sum("debit_amount"), c=Sum("credit_amount"))
    return (totals["d"] or D("0")) - (totals["c"] or D("0"))


def _legs(entry):
    return [
        (line.account.account_number, line.debit_amount, line.credit_amount, line.description)
        for line in entry.lines.select_related("account").order_by("sort_order")
    ]


class _Base(TestCase):
    deposits = True

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.a = make_posting_rules(self.company)
        self.a["1030"] = Account.objects.create(
            company=self.company, account_number="1030", name="Card processor clearing",
            account_type="asset", account_subtype="undeposited_funds",
        )
        PaymentProviderPostingOverride.objects.create(
            company=self.company, provider="stripe", cash_account=self.a["1030"],
        )
        if self.deposits:
            self.a["2050"] = Account.objects.create(
                company=self.company, account_number="2050", name="Customer Deposits",
                account_type="liability", account_subtype="other_current_liability",
            )
            rule = DocumentPostingRule.objects.get(company=self.company, doc_type="payment")
            rule.deposit_account = self.a["2050"]
            rule.full_clean()
            rule.save()
        customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace", email="ada@example.com", company=self.company,
        )
        self.order = Order.objects.create(
            customer=customer, company=self.company,
            subtotal_ex_tax=D("500.00"), tax_total=D("50.00"), shipping_cost_ex_tax=D("10.00"),
            grand_total=D("560.00"), balance_due=D("560.00"),
            status=Order.Status.AWAITING_PAYMENT,
        )
        self.api = APIClient()
        self.api.force_authenticate(user=company_user(self.company))
        self._n = 0

    # -- the storefront's calls -----------------------------------------------

    def pay(self, amount="560.00", *, invoice=None, fee="0.00", number=None):
        self._n += 1
        body = {
            "company": str(self.company.pk),
            "payment_number": number or f"PAY-W{self._n}",
            "order": str(self.order.pk),
            "payment_method": "credit_card",
            "payment_provider": "stripe",
            "amount": amount,
            "convenience_fee": fee,
            "payment_type": "payment",
            "status": "captured",
            "transaction_id": f"pi_test_{self._n}",
        }
        if invoice is not None:
            body["invoice"] = str(invoice.pk)
        response = self.api.post(PAYMENTS, body, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return Payment.objects.get(pk=response.data["id"])

    def convert(self) -> SalesInvoice:
        response = self.api.post(f"/api/v1/orders/orders/{self.order.pk}/convert-to-invoice/")
        self.assertEqual(response.status_code, 201, response.content)
        return SalesInvoice.objects.get(pk=response.data["id"])

    def issue(self, invoice, status="sent"):
        response = self.api.patch(
            f"/api/v1/orders/sales-invoices/{invoice.pk}/", {"status": status}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        invoice.refresh_from_db()
        return invoice

    def relink(self, payment, invoice):
        response = self.api.patch(
            f"{PAYMENTS}{payment.pk}/",
            {"invoice": str(invoice.pk) if invoice else None}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        payment.refresh_from_db()
        return payment

    def applications(self, payment=None):
        qs = JournalEntry.objects.filter(
            company=self.company, entry_source=JournalEntry.EntrySource.DEPOSIT_APPLICATION,
        )
        if payment is not None:
            qs = qs.filter(source_reference__startswith=f"{payment.payment_number}:")
        return qs

    def balances(self):
        return {n: _balance(self.a[n]) for n in ("1200", "1030", "2050") if n in self.a}


class OrderSettlementTests(_Base):
    """The 0.4.1 bug: an API payment against an order left it unpaid."""

    deposits = False

    def test_api_prepayment_settles_the_order(self):
        self.pay("560.00")
        self.order.refresh_from_db()
        self.assertEqual(self.order.amount_paid, D("560.00"))
        self.assertEqual(self.order.balance_due, D("0.00"))
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(self.order.status, Order.Status.AWAITING_FULFILLMENT)

    def test_partial_api_prepayments_accumulate(self):
        self.pay("200.00")
        self.order.refresh_from_db()
        self.assertEqual(self.order.amount_paid, D("200.00"))
        self.assertEqual(self.order.balance_due, D("360.00"))
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PARTIAL)
        self.pay("360.00")
        self.order.refresh_from_db()
        self.assertEqual(self.order.amount_paid, D("560.00"))
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PAID)

    def test_pending_api_payment_pays_nothing_until_captured(self):
        response = self.api.post(PAYMENTS, {
            "company": str(self.company.pk), "payment_number": "PAY-P1",
            "order": str(self.order.pk), "payment_method": "credit_card",
            "payment_provider": "stripe", "amount": "560.00", "status": "pending",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.order.refresh_from_db()
        self.assertEqual(self.order.amount_paid, D("0.00"))
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.UNPAID)
        patch = self.api.patch(f"{PAYMENTS}{response.data['id']}/", {"status": "captured"}, format="json")
        self.assertEqual(patch.status_code, 200, patch.content)
        self.order.refresh_from_db()
        self.assertEqual(self.order.amount_paid, D("560.00"))
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PAID)

    def test_api_payment_on_an_issued_invoice_settles_it(self):
        invoice = SalesInvoice.objects.create(
            order=self.order, company=self.company, status=SalesInvoice.Status.SENT,
            subtotal_ex_tax=D("500.00"), tax_total=D("50.00"), shipping_cost=D("10.00"),
            grand_total=D("560.00"), balance_due=D("560.00"),
        )
        self.pay("560.00", invoice=invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.amount_paid, D("560.00"))
        self.assertEqual(invoice.balance_due, D("0.00"))
        self.assertEqual(invoice.status, SalesInvoice.Status.PAID)


class NoDepositAccountUnchangedTests(_Base):
    """No deposit_account: the 0.4.1 postings, leg for leg."""

    deposits = False

    def test_prepayment_credits_ar_exactly_as_before(self):
        payment = self.pay("570.00", fee="10.00")
        entry = JournalEntry.objects.get(posting_key=f"payment:{payment.payment_number}")
        self.assertEqual(entry.entry_source, "payment")
        self.assertEqual(entry.source_reference, payment.payment_number)
        self.assertEqual(entry.memo, f"Payment {payment.payment_number}")
        self.assertEqual(_legs(entry), [
            ("1030", D("560.00"), D("0.00"), f"Payment {payment.payment_number}"),
            ("1200", D("0.00"), D("570.00"), "AR settled"),
            ("6020", D("10.00"), D("0.00"), "Processing fee"),
        ])

    def test_conversion_and_issue_post_only_the_invoice(self):
        payment = self.pay("560.00")
        invoice = self.issue(self.convert())
        payment.refresh_from_db()
        self.assertIsNone(payment.invoice_id)  # an A/R payment is not re-linked
        self.assertFalse(self.applications().exists())
        self.assertEqual(
            sorted(JournalEntry.objects.values_list("posting_key", flat=True)),
            sorted([f"payment:{payment.payment_number}", f"order:{invoice.invoice_number}"]),
        )
        self.assertEqual(_balance(self.a["1200"]), D("0.00"))

    def test_refund_debits_ar_as_before(self):
        payment = self.pay("560.00")
        refund = refund_payment(payment, D("60.00"))
        entry = JournalEntry.objects.get(posting_key=f"refund:{refund.payment_number}")
        self.assertEqual(_legs(entry), [
            ("1200", D("60.00"), D("0.00"), f"Refund {refund.payment_number}"),
            ("1030", D("0.00"), D("60.00"), "Cash refunded"),
        ])


class CustomerDepositFlowTests(_Base):
    def test_web_sale_end_to_end(self):
        payment = self.pay("560.00")
        entry = JournalEntry.objects.get(posting_key=f"payment:{payment.payment_number}")
        self.assertEqual(_legs(entry), [
            ("1030", D("560.00"), D("0.00"), f"Payment {payment.payment_number}"),
            ("2050", D("0.00"), D("560.00"), "Customer deposit received"),
        ])
        self.assertEqual(self.balances(), {"1200": D("0"), "1030": D("560.00"), "2050": D("-560.00")})
        self.order.refresh_from_db()
        self.assertEqual(self.order.amount_paid, D("560.00"))
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PAID)

        # Shipment: the order becomes its (draft) invoice; the deposit is
        # linked but stays a liability until the invoice is issued.
        invoice = self.convert()
        payment.refresh_from_db()
        self.assertEqual(payment.invoice_id, invoice.pk)
        self.assertEqual(invoice.status, SalesInvoice.Status.DRAFT)
        self.assertEqual(invoice.amount_paid, D("560.00"))
        self.assertEqual(invoice.balance_due, D("0.00"))
        self.assertFalse(self.applications().exists())
        self.assertEqual(_balance(self.a["2050"]), D("-560.00"))

        invoice = self.issue(invoice)
        self.assertEqual(invoice.status, SalesInvoice.Status.PAID)
        application = self.applications().get()
        self.assertEqual(
            application.posting_key,
            f"deposit_application:{payment.payment_number}:{invoice.invoice_number}:1",
        )
        self.assertEqual(_legs(application), [
            ("2050", D("560.00"), D("0.00"), f"Deposit {payment.payment_number} applied"),
            ("1200", D("0.00"), D("560.00"), f"Invoice {invoice.invoice_number} settled"),
        ])
        self.assertEqual(self.balances(), {"1200": D("0.00"), "1030": D("560.00"), "2050": D("0.00")})
        self.assertEqual(_balance(self.a["4010"]), D("-500.00"))

        # Replays post nothing new.
        count = JournalEntry.objects.count()
        payment.save()
        self.issue(invoice, "paid")
        self.assertEqual(JournalEntry.objects.count(), count)

        result = integrity.compare_totals()
        self.assertTrue(all(r["status"] == "OK" for r in result.trial_balance), result.trial_balance)
        self.assertNotIn("ledger_mismatch", result.invoice_balances)
        self.assertNotIn("no_payment_rows", result.invoice_balances)
        self.assertNotIn("deposit_overapplied", result.invoice_balances)

    def test_payment_with_an_invoice_still_credits_ar(self):
        invoice = SalesInvoice.objects.create(
            order=self.order, company=self.company, status=SalesInvoice.Status.SENT,
            subtotal_ex_tax=D("500.00"), tax_total=D("50.00"), shipping_cost=D("10.00"),
            grand_total=D("560.00"), balance_due=D("560.00"),
        )
        payment = self.pay("560.00", invoice=invoice)
        entry = JournalEntry.objects.get(posting_key=f"payment:{payment.payment_number}")
        self.assertEqual(_legs(entry)[1], ("1200", D("0.00"), D("560.00"), "AR settled"))
        self.assertFalse(self.applications().exists())
        self.assertEqual(self.balances(), {"1200": D("0.00"), "1030": D("560.00"), "2050": D("0")})

    def test_voiding_the_invoice_returns_the_money_to_deposits(self):
        payment = self.pay("560.00")
        invoice = self.issue(self.convert())
        self.issue(invoice, "void")
        self.assertEqual(self.balances(), {"1200": D("0.00"), "1030": D("560.00"), "2050": D("-560.00")})
        self.assertIsNone(active_application(payment))

    def test_cancelling_an_unapplied_deposit(self):
        payment = self.pay("560.00")
        payment.status = Payment.Status.CANCELLED
        payment.save()
        self.assertEqual(self.balances(), {"1200": D("0.00"), "1030": D("0.00"), "2050": D("0.00")})

    def test_cancelling_an_applied_deposit_reopens_the_receivable(self):
        payment = self.pay("560.00")
        self.issue(self.convert())
        payment.refresh_from_db()
        payment.status = Payment.Status.CANCELLED
        payment.save()
        self.assertEqual(self.balances(), {"1200": D("560.00"), "1030": D("0.00"), "2050": D("0.00")})

    def test_unlink_and_relink_through_the_api(self):
        payment = self.pay("560.00")
        invoice = self.issue(self.convert())
        payment.refresh_from_db()
        self.relink(payment, None)
        invoice.refresh_from_db()
        self.assertEqual(invoice.amount_paid, D("0.00"))
        self.assertEqual(self.balances(), {"1200": D("560.00"), "1030": D("560.00"), "2050": D("-560.00")})
        self.relink(payment, invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.amount_paid, D("560.00"))
        self.assertEqual(self.balances(), {"1200": D("0.00"), "1030": D("560.00"), "2050": D("0.00")})
        keys = sorted(self.applications(payment).values_list("source_reference", flat=True))
        self.assertEqual(keys, [
            f"{payment.payment_number}:{invoice.invoice_number}:1",
            f"{payment.payment_number}:{invoice.invoice_number}:2",
        ])

    def test_overpayment_applies_whole_and_leaves_an_ar_credit(self):
        self.pay("600.00")
        self.issue(self.convert())
        self.assertEqual(self.balances(), {"1200": D("-40.00"), "1030": D("600.00"), "2050": D("0.00")})

    def test_several_prepayments_each_apply(self):
        first, second = self.pay("300.00"), self.pay("260.00")
        invoice = self.issue(self.convert())
        self.assertEqual(invoice.status, SalesInvoice.Status.PAID)
        self.assertEqual(self.applications().count(), 2)
        self.assertEqual(self.applications(first).get().lines.get(account=self.a["2050"]).debit_amount,
                         D("300.00"))
        self.assertEqual(self.balances(), {"1200": D("0.00"), "1030": D("560.00"), "2050": D("0.00")})
        self.assertTrue(self.applications(second).exists())

    def test_refund_before_invoicing_comes_out_of_deposits(self):
        payment = self.pay("560.00")
        refund = refund_payment(payment, D("60.00"))
        entry = JournalEntry.objects.get(posting_key=f"refund:{refund.payment_number}")
        self.assertEqual(_legs(entry)[0][:3], ("2050", D("60.00"), D("0.00")))
        self.assertEqual(self.balances(), {"1200": D("0"), "1030": D("500.00"), "2050": D("-500.00")})
        self.issue(self.convert())
        application = self.applications(payment).get()
        self.assertEqual(application.lines.get(account=self.a["2050"]).debit_amount, D("500.00"))
        # The order was 560 and 500 was kept: A/R carries the 60 still owed.
        self.assertEqual(self.balances(), {"1200": D("60.00"), "1030": D("500.00"), "2050": D("0.00")})

    def test_refund_after_invoicing_comes_out_of_ar(self):
        payment = self.pay("560.00")
        self.issue(self.convert())
        payment.refresh_from_db()
        refund = refund_payment(payment, D("60.00"))
        entry = JournalEntry.objects.get(posting_key=f"refund:{refund.payment_number}")
        self.assertEqual(_legs(entry)[0][:3], ("1200", D("60.00"), D("0.00")))
        self.assertEqual(self.balances(), {"1200": D("60.00"), "1030": D("500.00"), "2050": D("0.00")})
        self.assertEqual(self.applications().count(), 1)

    def test_full_refund_of_a_deposit_applies_nothing(self):
        payment = self.pay("560.00")
        refund_payment(payment, D("560.00"))
        self.convert()  # a refunded payment is not a held deposit: not linked
        self.assertFalse(self.applications().exists())
        self.assertEqual(self.balances(), {"1200": D("0"), "1030": D("0.00"), "2050": D("0.00")})

    def test_integrity_flags_an_overapplied_deposit_account(self):
        post_balanced_entry(
            company=self.company, entry_date=date.today(),
            entry_source=JournalEntry.EntrySource.MANUAL, source_reference="bad",
            legs=[(self.a["2050"], D("25.00"), D("0"), "x"), (self.a["1200"], D("0"), D("25.00"), "y")],
        )
        bucket = integrity.compare_totals().invoice_balances["deposit_overapplied"]
        self.assertEqual(bucket.count, 1)
        self.assertEqual(bucket.status, "ROLL BACK")


class DepositRuleValidationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.a = make_posting_rules(self.company)
        self.rule = DocumentPostingRule.objects.get(company=self.company, doc_type="payment")

    def _liability(self, company, number="2050"):
        return Account.objects.create(
            company=company, account_number=number, name="Customer Deposits",
            account_type="liability", account_subtype="other_current_liability",
        )

    def test_must_be_a_liability(self):
        self.rule.deposit_account = self.a["1490"]
        with self.assertRaises(ValidationError) as ctx:
            self.rule.full_clean()
        self.assertIn("deposit_account", ctx.exception.message_dict)

    def test_must_belong_to_the_company(self):
        other = Company.objects.create(name="Other Co")
        self.rule.deposit_account = self._liability(other)
        with self.assertRaises(ValidationError) as ctx:
            self.rule.full_clean()
        self.assertIn("deposit_account", ctx.exception.message_dict)

    def test_payment_rule_only(self):
        refund = DocumentPostingRule.objects.get(company=self.company, doc_type="refund")
        refund.deposit_account = self._liability(self.company)
        with self.assertRaises(ValidationError):
            refund.full_clean()

    def test_posting_rules_command_sets_shows_and_clears_it(self):
        self._liability(self.company)
        out = StringIO()
        call_command("posting_rules", "set", "--doc-type", "payment", "--deposit", "2050",
                     "--company", str(self.company.pk), stdout=out)
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.deposit_account.account_number, "2050")
        out = StringIO()
        call_command("posting_rules", "show", "--company", str(self.company.pk), stdout=out)
        self.assertIn("deposit_account", out.getvalue())
        self.assertIn("2050 Customer Deposits", out.getvalue())
        call_command("posting_rules", "set", "--doc-type", "payment", "--no-deposit",
                     "--company", str(self.company.pk), stdout=StringIO())
        self.rule.refresh_from_db()
        self.assertIsNone(self.rule.deposit_account_id)
