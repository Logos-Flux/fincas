"""Step 1c (test plan #18): reconciliation balances to zero on posted docs."""

from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from accounting.models import Account, PaymentProviderPostingOverride
from accounting.tests.test_posting import make_order, make_posting_rules
from core.models import Company
from orders.models import SalesInvoice
from payments.models import Payment


class ReconciliationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Recon Co")
        make_posting_rules(self.company)
        self.order = make_order(self.company)
        self.staff = User.objects.create_user("r", password="p", is_staff=True)

    def _login(self):
        self.assertTrue(self.client.login(username="r", password="p"))

    def _invoice(self, grand="560.00", balance=None):
        invoice = SalesInvoice.objects.create(
            order=self.order,
            company=self.company,
            status=SalesInvoice.Status.DRAFT,
            invoice_date=date.today(),
            subtotal_ex_tax=Decimal("500.00"),
            tax_total=Decimal("50.00"),
            shipping_cost=Decimal("10.00"),
            grand_total=Decimal(grand),
            balance_due=Decimal(balance or grand),
        )
        invoice.status = SalesInvoice.Status.SENT
        invoice.save()
        return invoice

    def test_full_cycle_reconciles_to_zero(self):
        invoice = self._invoice()
        payment = Payment.objects.create(
            order=self.order,
            company=self.company,
            amount=Decimal("560.00"),
            payment_type=Payment.PaymentType.PAYMENT,
            status=Payment.Status.CAPTURED,
        )
        # SENT→PAID is not a posting transition: the hook stays quiet and
        # the fixture values are what recon reads.
        invoice.balance_due = Decimal("0")
        invoice.amount_paid = Decimal("560.00")
        invoice.status = SalesInvoice.Status.PAID
        invoice.save()
        _ = payment

        self._login()
        response = self.client.get("/erp/accounting/reconciliation/")
        self.assertEqual(response.status_code, 200)
        rows = {r["label"]: r for r in response.context["rows"]}
        self.assertEqual(rows["Accounts Receivable"]["diff"], Decimal("0"))
        self.assertEqual(rows["Cash"]["diff"], Decimal("0"))
        self.assertEqual(
            response.context["out_of_balance"], []
        )

    def _pay_and_close(self, invoice, provider, amount="560.00", fee="0"):
        payment = Payment.objects.create(
            order=self.order, company=self.company, amount=Decimal(amount),
            convenience_fee=Decimal(fee), payment_provider=provider,
            payment_type=Payment.PaymentType.PAYMENT, status=Payment.Status.CAPTURED,
        )
        invoice.balance_due = Decimal("0")
        invoice.amount_paid = Decimal(amount)
        invoice.status = SalesInvoice.Status.PAID
        invoice.save()
        return payment

    def _cash_row(self):
        self._login()
        response = self.client.get("/erp/accounting/reconciliation/")
        self.assertEqual(response.status_code, 200)
        return {r["label"]: r for r in response.context["rows"]}["Cash"]

    def test_cash_row_sums_override_accounts(self):
        paypal = Account.objects.create(
            company=self.company, account_number="1020", name="PayPal",
            account_type="asset", account_subtype="bank",
        )
        PaymentProviderPostingOverride.objects.create(
            company=self.company, provider="paypal", cash_account=paypal,
        )
        self._pay_and_close(self._invoice(), Payment.PaymentProvider.PAYPAL, fee="10.00")
        self._pay_and_close(self._invoice(), Payment.PaymentProvider.STRIPE)
        row = self._cash_row()
        # 550 in PayPal + 560 in the rule's cash account; subledger is net of fees.
        self.assertEqual(row["gl"], Decimal("1110.00"))
        self.assertEqual(row["diff"], Decimal("0"))

    def test_cash_row_does_not_double_count_shared_account(self):
        rule_cash = Account.objects.get(company=self.company, account_number="1010")
        PaymentProviderPostingOverride.objects.create(
            company=self.company, provider="ebay", cash_account=rule_cash,
        )
        self._pay_and_close(self._invoice(), Payment.PaymentProvider.EBAY)
        row = self._cash_row()
        self.assertEqual(row["gl"], Decimal("560.00"))
        self.assertEqual(row["diff"], Decimal("0"))

    def test_unpaid_invoice_shows_open_ar(self):
        self._invoice()
        self._login()
        response = self.client.get("/erp/accounting/reconciliation/")
        rows = {r["label"]: r for r in response.context["rows"]}
        self.assertEqual(rows["Accounts Receivable"]["diff"], Decimal("0"))
        self.assertEqual(
            rows["Accounts Receivable"]["subledger"], Decimal("560.00")
        )

    def test_csv_export(self):
        self._invoice()
        self._login()
        response = self.client.get("/erp/accounting/reconciliation/?export=csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        content = response.content.decode()
        self.assertIn("Accounts Receivable", content)

    def test_missing_rule_is_loud_not_500(self):
        from accounting.models import DocumentPostingRule

        DocumentPostingRule.objects.filter(company=self.company).delete()
        self._login()
        response = self.client.get("/erp/accounting/reconciliation/")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["setup_error"])
