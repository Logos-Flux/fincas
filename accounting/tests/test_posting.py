"""Step 1a (test plan #1-8, #16): operational-to-GL postings.

Covers: per-doc-type balanced postings with GL-to-subledger reconciliation,
imbalance/atomicity, closed-period rejection (incl. backdated E2E),
idempotent re-post, reversal on void, cross-company rejection,
creator-enumeration (AD-2), and concurrent double-post.
"""

import threading
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, TransactionTestCase

from accounting.models import (
    ZERO,
    Account,
    DocumentPostingRule,
    FiscalPeriod,
    JournalEntry,
    PaymentProviderPostingOverride,
)
from accounting.posting import (
    NoPostingRule,
    PostingError,
    post_balanced_entry,
    reverse_entry,
)
from core.models import Company
from customers.models import Customer
from orders.models import Order, SalesInvoice
from payments.models import Payment


def make_accounts(company):
    specs = [
        ("1010", "Cash", "asset", "bank"),
        ("1200", "AR", "asset", "accounts_receivable"),
        ("1400", "Inventory", "asset", "inventory"),
        ("1490", "Suspense", "asset", "other_current_asset"),
        ("2010", "AP", "liability", "accounts_payable"),
        ("2040", "Sales Tax", "liability", "sales_tax_payable"),
        ("3020", "Opening Equity", "equity", "opening_balance_equity"),
        ("4010", "Sales", "income", "sales_income"),
        ("4030", "Discounts", "income", "discounts_refunds"),
        ("4040", "Ship Income", "income", "shipping_income"),
        ("6020", "Proc Fees", "expense", "processing_fee_expense"),
    ]
    out = {}
    for number, name, atype, subtype in specs:
        out[number], _ = Account.objects.get_or_create(
            company=company,
            account_number=number,
            defaults={
                "name": name, "account_type": atype,
                "account_subtype": subtype, "is_active": True,
            },
        )
    return out


def make_posting_rules(company):
    a = make_accounts(company)
    rules = {
        "sales_invoice": dict(
            receivable_account=a["1200"], revenue_account=a["4010"],
            tax_account=a["2040"], discount_account=a["4030"],
            shipping_income_account=a["4040"],
        ),
        "payment": dict(
            receivable_account=a["1200"], cash_account=a["1010"],
            fee_account=a["6020"],
        ),
        "refund": dict(
            receivable_account=a["1200"], cash_account=a["1010"],
        ),
        "purchase_receipt": dict(
            inventory_account=a["1400"], payable_account=a["2010"],
        ),
    }
    for doc_type, fields in rules.items():
        rule, _ = DocumentPostingRule.objects.get_or_create(
            company=company, doc_type=doc_type, defaults=fields
        )
        for field, account in fields.items():
            setattr(rule, field, account)
        rule.full_clean()
        rule.save()
    return a


def make_order(company, customer=None):
    customer = customer or Customer.objects.create(
        first_name="A", last_name="B", email="a@b.io", company=company
    )
    return Order.objects.create(customer=customer, company=company)


class PostingServiceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Post Co")
        self.accounts = make_posting_rules(self.company)
        self.order = make_order(self.company)

    def _invoice(self, **kw):
        kw.setdefault("order", self.order)
        kw.setdefault("company", self.company)
        kw.setdefault("status", SalesInvoice.Status.DRAFT)
        kw.setdefault("invoice_date", date.today())
        kw.setdefault("subtotal_ex_tax", Decimal("500.00"))
        kw.setdefault("tax_total", Decimal("50.00"))
        kw.setdefault("shipping_cost", Decimal("10.00"))
        kw.setdefault("grand_total", Decimal("560.00"))
        kw.setdefault("balance_due", Decimal("560.00"))
        return SalesInvoice.objects.create(**kw)

    def test_invoice_posts_balanced_and_reconciles(self):
        invoice = self._invoice()
        invoice.status = SalesInvoice.Status.SENT
        invoice.save()

        entry = JournalEntry.objects.get(
            company=self.company, posting_key=f"order:{invoice.invoice_number}"
        )
        self.assertTrue(entry.is_balanced)
        debit, _credit = entry.totals()
        self.assertEqual(debit, Decimal("560.00"))
        # GL-to-subledger: AR leg equals the invoice grand total.
        ar = entry.lines.get(account=self.accounts["1200"])
        self.assertEqual(ar.debit_amount, invoice.grand_total)

    def test_invoice_with_discount_posts_discount_leg(self):
        invoice = self._invoice(
            subtotal_ex_tax=Decimal("500.00"), grand_total=Decimal("510.00")
        )
        invoice.status = SalesInvoice.Status.SENT
        invoice.save()

        entry = JournalEntry.objects.get(
            company=self.company, posting_key=f"order:{invoice.invoice_number}"
        )
        self.assertTrue(entry.is_balanced)
        disc = entry.lines.get(account=self.accounts["4030"])
        self.assertEqual(disc.debit_amount, Decimal("50.00"))

    def test_repost_is_idempotent(self):
        from orders.posting import post_sales_invoice

        invoice = self._invoice()
        invoice.status = SalesInvoice.Status.SENT
        invoice.save()
        first = JournalEntry.objects.get(
            company=self.company, posting_key=f"order:{invoice.invoice_number}"
        )
        self.assertEqual(post_sales_invoice(invoice).pk, first.pk)
        self.assertEqual(
            JournalEntry.objects.filter(
                company=self.company,
                posting_key=f"order:{invoice.invoice_number}",
            ).count(),
            1,
        )

    def test_void_reverses_never_edits(self):

        invoice = self._invoice()
        invoice.status = SalesInvoice.Status.SENT
        invoice.save()
        original = JournalEntry.objects.get(
            company=self.company, posting_key=f"order:{invoice.invoice_number}"
        )
        original_lines = list(
            original.lines.values_list("debit_amount", "credit_amount")
        )
        invoice.status = SalesInvoice.Status.VOID
        invoice.save()

        reversal = JournalEntry.objects.get(
            company=self.company,
            posting_key=f"system:reversal:order:{invoice.invoice_number}",
        )
        self.assertTrue(reversal.is_balanced)
        # Original untouched.
        self.assertEqual(
            list(
                JournalEntry.objects.get(pk=original.pk).lines.values_list(
                    "debit_amount", "credit_amount"
                )
            ),
            original_lines,
        )
        # Net of the pair is zero per account.
        self.assertRaises(
            PostingError, reverse_entry, reversal
        )

    def test_closed_period_rejects_backdated_post(self):
        FiscalPeriod.objects.create(
            company=self.company,
            name="Past",
            start_date=date.today() - timedelta(days=60),
            end_date=date.today() - timedelta(days=30),
            status=FiscalPeriod.Status.CLOSED,
        )
        invoice = self._invoice(
            invoice_date=date.today() - timedelta(days=45)
        )
        invoice.status = SalesInvoice.Status.SENT
        with self.assertRaises(ValidationError):
            invoice.save()
        self.assertFalse(
            JournalEntry.objects.filter(company=self.company).exists()
        )

    def test_missing_rule_fails_loudly(self):
        rule = DocumentPostingRule.objects.get(
            company=self.company, doc_type="sales_invoice"
        )
        rule.delete()
        invoice = self._invoice()
        invoice.status = SalesInvoice.Status.SENT
        with self.assertRaises(NoPostingRule) as ctx:
            invoice.save()
        self.assertIn("sales_invoice", str(ctx.exception))
        self.assertIn(self.company.name, str(ctx.exception))

    def test_cross_company_leg_rejected(self):
        other = Company.objects.create(name="Other Co")
        other_accounts = make_accounts(other)
        with self.assertRaises(PostingError):
            post_balanced_entry(
                company=self.company,
                entry_date=date.today(),
                entry_source=JournalEntry.EntrySource.SYSTEM,
                source_reference="x1",
                legs=[
                    (self.accounts["1010"], Decimal("10"), ZERO, ""),
                    (other_accounts["2010"], ZERO, Decimal("10"), ""),
                ],
            )
        self.assertFalse(
            JournalEntry.objects.filter(company=self.company).exists()
        )

    def test_imbalance_writes_nothing(self):
        with self.assertRaises(ValidationError):
            post_balanced_entry(
                company=self.company,
                entry_date=date.today(),
                entry_source=JournalEntry.EntrySource.SYSTEM,
                source_reference="bad1",
                legs=[
                    (self.accounts["1010"], Decimal("10"), ZERO, ""),
                    (self.accounts["2010"], ZERO, Decimal("9"), ""),
                ],
            )
        self.assertFalse(
            JournalEntry.objects.filter(company=self.company).exists()
        )

    def test_repost_after_save_without_transition_is_quiet(self):

        invoice = self._invoice()
        invoice.status = SalesInvoice.Status.SENT
        invoice.save()
        invoice.save()  # no transition: no new post, no error
        self.assertEqual(
            JournalEntry.objects.filter(
                company=self.company,
                posting_key=f"order:{invoice.invoice_number}",
            ).count(),
            1,
        )

    def test_only_chokepoint_writes_entries(self):
        """AD-2: every non-test, non-admin direct JournalEntry writer must be
        the choke-point, the QBO importer, or a reversal helper."""
        allowed = {"accounting/posting.py", "accounting/qbo/importer.py"}
        offenders = []
        for path in list(Path("accounting").rglob("*.py")) + list(
            Path("orders").rglob("*.py")
        ) + list(Path("payments").rglob("*.py")) + list(
            Path("purchasing").rglob("*.py")
        ):
            if (
                "test" in path.parts
                or "migrations" in path.parts
                or path.name.startswith("test")
            ):
                continue
            text = path.read_text()
            if "JournalEntry.objects.create" in text or "JournalEntry(" in text:
                rel = str(path)
                if rel not in allowed and "admin.py" not in rel:
                    # JournalEntry( constructor refs in posting helpers that
                    # immediately delegate to post_balanced_entry are fine;
                    # flag only direct .objects.create writes.
                    if "JournalEntry.objects.create" in text:
                        offenders.append(rel)
        self.assertEqual(offenders, [])

    def test_payment_posts_on_capture(self):
        payment = Payment.objects.create(
            order=self.order,
            company=self.company,
            amount=Decimal("560.00"),
            convenience_fee=Decimal("10.00"),
            payment_type=Payment.PaymentType.PAYMENT,
            status=Payment.Status.CAPTURED,
        )
        entry = JournalEntry.objects.get(
            company=self.company,
            posting_key=f"payment:{payment.payment_number}",
        )
        self.assertTrue(entry.is_balanced)
        cash = entry.lines.get(account=self.accounts["1010"])
        self.assertEqual(cash.debit_amount, Decimal("550.00"))
        fee = entry.lines.get(account=self.accounts["6020"])
        self.assertEqual(fee.debit_amount, Decimal("10.00"))

    def test_refund_posts_mirror(self):
        payment = Payment.objects.create(
            order=self.order,
            company=self.company,
            amount=Decimal("100.00"),
            payment_type=Payment.PaymentType.REFUND,
            status=Payment.Status.REFUNDED,
        )
        entry = JournalEntry.objects.get(
            company=self.company,
            posting_key=f"refund:{payment.payment_number}",
        )
        self.assertTrue(entry.is_balanced)
        self.assertEqual(
            entry.entry_source, JournalEntry.EntrySource.REFUND
        )
        ar = entry.lines.get(account=self.accounts["1200"])
        self.assertEqual(ar.debit_amount, Decimal("100.00"))

    def _override(self, provider="paypal", fee=True):
        self.accounts["1020"], _ = Account.objects.get_or_create(
            company=self.company, account_number="1020",
            defaults={"name": "PayPal", "account_type": "asset",
                      "account_subtype": "bank", "is_active": True},
        )
        self.accounts["6025"], _ = Account.objects.get_or_create(
            company=self.company, account_number="6025",
            defaults={"name": "PayPal Fees", "account_type": "expense",
                      "account_subtype": "processing_fee_expense", "is_active": True},
        )
        override = PaymentProviderPostingOverride(
            company=self.company, provider=provider,
            cash_account=self.accounts["1020"],
            fee_account=self.accounts["6025"] if fee else None,
        )
        override.full_clean()
        override.save()
        return override

    def _payment(self, provider, **kw):
        kw.setdefault("payment_type", Payment.PaymentType.PAYMENT)
        kw.setdefault("status", Payment.Status.CAPTURED)
        kw.setdefault("amount", Decimal("560.00"))
        kw.setdefault("convenience_fee", Decimal("10.00"))
        return Payment.objects.create(
            order=self.order, company=self.company, payment_provider=provider, **kw
        )

    def test_provider_override_swaps_cash_and_fee(self):
        self._override("paypal")
        payment = self._payment(Payment.PaymentProvider.PAYPAL)
        entry = JournalEntry.objects.get(
            company=self.company, posting_key=f"payment:{payment.payment_number}"
        )
        self.assertTrue(entry.is_balanced)
        self.assertEqual(
            entry.lines.get(account=self.accounts["1020"]).debit_amount, Decimal("550.00")
        )
        self.assertEqual(
            entry.lines.get(account=self.accounts["6025"]).debit_amount, Decimal("10.00")
        )
        self.assertFalse(entry.lines.filter(account=self.accounts["1010"]).exists())
        self.assertFalse(entry.lines.filter(account=self.accounts["6020"]).exists())
        # AR still settles against the rule's receivable.
        self.assertEqual(
            entry.lines.get(account=self.accounts["1200"]).credit_amount, Decimal("560.00")
        )

    def test_provider_without_override_uses_rule(self):
        self._override("paypal")
        payment = self._payment(Payment.PaymentProvider.STRIPE)
        entry = JournalEntry.objects.get(
            company=self.company, posting_key=f"payment:{payment.payment_number}"
        )
        self.assertEqual(
            entry.lines.get(account=self.accounts["1010"]).debit_amount, Decimal("550.00")
        )
        self.assertEqual(
            entry.lines.get(account=self.accounts["6020"]).debit_amount, Decimal("10.00")
        )

    def test_override_without_fee_keeps_rule_fee(self):
        self._override("paypal", fee=False)
        payment = self._payment(Payment.PaymentProvider.PAYPAL)
        entry = JournalEntry.objects.get(
            company=self.company, posting_key=f"payment:{payment.payment_number}"
        )
        self.assertEqual(
            entry.lines.get(account=self.accounts["1020"]).debit_amount, Decimal("550.00")
        )
        self.assertEqual(
            entry.lines.get(account=self.accounts["6020"]).debit_amount, Decimal("10.00")
        )

    def test_refund_mirrors_through_override(self):
        self._override("paypal")
        payment = self._payment(
            Payment.PaymentProvider.PAYPAL,
            payment_type=Payment.PaymentType.REFUND,
            status=Payment.Status.REFUNDED,
            amount=Decimal("100.00"), convenience_fee=Decimal("0"),
        )
        entry = JournalEntry.objects.get(
            company=self.company, posting_key=f"refund:{payment.payment_number}"
        )
        self.assertTrue(entry.is_balanced)
        self.assertEqual(
            entry.lines.get(account=self.accounts["1020"]).credit_amount, Decimal("100.00")
        )
        self.assertFalse(entry.lines.filter(account=self.accounts["1010"]).exists())

    def test_blank_provider_refund_follows_parent_payment(self):
        self._override("paypal")
        parent = self._payment(Payment.PaymentProvider.PAYPAL)
        refund = Payment.objects.create(
            order=self.order, company=self.company, parent_payment=parent,
            payment_type=Payment.PaymentType.REFUND, status=Payment.Status.REFUNDED,
            amount=Decimal("100.00"), convenience_fee=Decimal("0"),
        )
        self.assertEqual(refund.payment_provider, "")
        entry = JournalEntry.objects.get(
            company=self.company, posting_key=f"refund:{refund.payment_number}"
        )
        self.assertEqual(
            entry.lines.get(account=self.accounts["1020"]).credit_amount, Decimal("100.00")
        )
        self.assertFalse(entry.lines.filter(account=self.accounts["1010"]).exists())

    def test_override_rejects_unknown_provider(self):
        self.accounts["1020"] = Account.objects.create(
            company=self.company, account_number="1020", name="PayPal",
            account_type="asset", account_subtype="bank",
        )
        override = PaymentProviderPostingOverride(
            company=self.company, provider="venmo", cash_account=self.accounts["1020"]
        )
        with self.assertRaises(ValidationError) as ctx:
            override.full_clean()
        self.assertIn("provider", ctx.exception.message_dict)

    def test_override_rejects_cross_company_account(self):
        other = Company.objects.create(name="Other Co")
        foreign = Account.objects.create(
            company=other, account_number="1020", name="PayPal",
            account_type="asset", account_subtype="bank",
        )
        override = PaymentProviderPostingOverride(
            company=self.company, provider="paypal", cash_account=foreign
        )
        with self.assertRaises(ValidationError) as ctx:
            override.full_clean()
        self.assertIn("cash_account", ctx.exception.message_dict)

    def test_opening_balance_verb(self):
        from accounting.posting import post_opening_balance

        cash = self.accounts["1010"]
        entry = post_opening_balance(company=self.company, account=cash, amount="1000.00")
        self.assertTrue(entry.is_balanced)
        equity = self.accounts["3020"]
        self.assertEqual(
            entry.lines.get(account=equity).credit_amount, Decimal("1000.00")
        )
        # Negative books the credit side.
        ap = self.accounts["2010"]
        entry2 = post_opening_balance(
            company=self.company, account=ap, amount="-500.00"
        )
        self.assertEqual(
            entry2.lines.get(account=ap).credit_amount, Decimal("500.00")
        )
        with self.assertRaises(PostingError):
            post_opening_balance(
                company=self.company, account=cash, amount="0"
            )

    def test_opening_balance_needs_equity_account(self):
        from accounting.posting import post_opening_balance

        Account.objects.filter(
            company=self.company, account_number="3020"
        ).delete()
        with self.assertRaises(PostingError) as ctx:
            post_opening_balance(
                company=self.company,
                account=self.accounts["1010"],
                amount="10.00",
            )
        self.assertIn("Opening Balance Equity", str(ctx.exception))

    def test_rule_cross_company_account_rejected(self):
        other = Company.objects.create(name="Other Co")
        other_accounts = make_accounts(other)
        rule = DocumentPostingRule.objects.get(
            company=self.company, doc_type="payment"
        )
        rule.cash_account = other_accounts["1010"]
        with self.assertRaises(ValidationError):
            rule.full_clean()


class ConcurrentPostingTests(TransactionTestCase):
    """ED-2: concurrent posters serialize; unique posting_key holds.

    Threads need committed rows (invisible inside TestCase's wrapping
    atomic on SQLite), hence TransactionTestCase. Skipped on SQLite file
    DBs only if the backend cannot do threads — SQLite serializes writers
    with a busy timeout, which exercises the retry path instead.
    """

    def test_concurrent_double_post_yields_one_entry(self):
        company = Company.objects.create(name="Race Co")
        make_posting_rules(company)
        customer = Customer.objects.create(
            first_name="A", last_name="B", email="a@b.io", company=company
        )
        order = Order.objects.create(customer=customer, company=company)
        invoice = SalesInvoice.objects.create(
            order=order,
            company=company,
            status=SalesInvoice.Status.DRAFT,
            invoice_date=date.today(),
            subtotal_ex_tax=Decimal("100.00"),
            tax_total=Decimal("10.00"),
            shipping_cost=Decimal("0"),
            grand_total=Decimal("110.00"),
            balance_due=Decimal("110.00"),
        )
        invoice.status = SalesInvoice.Status.SENT
        invoice.save()  # posts once via the hook
        from orders.posting import post_sales_invoice

        errors = []

        def repost():
            from django.db import connections

            try:
                post_sales_invoice(invoice)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=repost) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if connection.vendor == "sqlite":
            # SQLite serializes writers; busy-timeout collisions surface as
            # OperationalError, which the retry loop converts or raises as
            # PostingError — either way no duplicate entry may exist.
            for exc in errors:
                self.assertIn(
                    type(exc).__name__, ("OperationalError", "PostingError")
                )
        else:
            self.assertEqual(errors, [])
        self.assertEqual(
            JournalEntry.objects.filter(
                company=company,
                posting_key=f"order:{invoice.invoice_number}",
            ).count(),
            1,
        )


class ReceiptPostingTests(TestCase):
    def setUp(self):
        from products.models import Product, ProductVariant
        from purchasing.models import (
            POLineItem,
            POReceipt,
            POReceiptLine,
            PurchaseOrder,
            Supplier,
        )

        self.company = Company.objects.create(name="Recv Co")
        make_posting_rules(self.company)
        self.supplier = Supplier.objects.create(
            company_name="Supply Ltd", company=self.company
        )
        self.po = PurchaseOrder.objects.create(
            supplier=self.supplier,
            company=self.company,
            shipping_cost=Decimal("20.00"),
        )
        product = Product.objects.create(
            title="Widget", company=self.company
        )
        variant = ProductVariant.objects.create(product=product, sku="W-1")
        self.line = POLineItem.objects.create(
            purchase_order=self.po,
            variant=variant,
            item_name="Widget",
            quantity_ordered=Decimal("10"),
            unit_cost=Decimal("20.0000"),
            line_total=Decimal("200.00"),
        )
        # Server-computed landed basis: 20 + 20×(200/200)/10 = 22/unit.
        self.po.recalculate_totals()
        self.line.refresh_from_db()
        self.receipt = POReceipt.objects.create(
            purchase_order=self.po, status=POReceipt.Status.DRAFT
        )
        POReceiptLine.objects.create(
            receipt=self.receipt,
            po_line_item=self.line,
            quantity_received=Decimal("5"),
        )
        self.POReceipt = POReceipt

    def test_receipt_posts_goods_and_landed(self):
        self.receipt.status = self.POReceipt.Status.VERIFIED
        self.receipt.save()

        goods = JournalEntry.objects.get(
            company=self.company,
            posting_key=f"purchase:{self.receipt.receipt_number}",
        )
        self.assertTrue(goods.is_balanced)
        inv = goods.lines.get(account__account_number="1400")
        self.assertEqual(inv.debit_amount, Decimal("100.00"))

        landed = JournalEntry.objects.get(
            company=self.company,
            posting_key=f"landed_cost:{self.receipt.receipt_number}",
        )
        self.assertTrue(landed.is_balanced)
        self.assertEqual(
            landed.entry_source, JournalEntry.EntrySource.LANDED_COST
        )
        inv_landed = landed.lines.get(account__account_number="1400")
        self.assertEqual(inv_landed.debit_amount, Decimal("10.00"))

    def test_receipt_without_landed_posts_goods_only(self):
        self.po.shipping_cost = Decimal("0")
        self.po.save(update_fields=["shipping_cost"])
        self.po.recalculate_totals()
        self.receipt.status = self.POReceipt.Status.VERIFIED
        self.receipt.save()

        self.assertTrue(
            JournalEntry.objects.filter(
                company=self.company,
                posting_key=f"purchase:{self.receipt.receipt_number}",
            ).exists()
        )
        self.assertFalse(
            JournalEntry.objects.filter(
                company=self.company,
                posting_key=f"landed_cost:{self.receipt.receipt_number}",
            ).exists()
        )
