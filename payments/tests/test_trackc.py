"""Track C: payments — gateway contracts, idempotency, settlement, log hygiene.

Covers docs/testing-plan.md §3 "Payments": per-gateway adapter contract
(Stripe, Manual, USDC) with mocked SDKs, idempotency keys on retried
POSTs, settlement-log reconciliation to invoice balance, and no
card/PII in logs.
"""

import logging
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings

from accounting.tests.test_posting import make_posting_rules
from core.models import Company
from customers.models import Customer
from orders.models import Order, SalesInvoice
from payments.gateways import get_gateway, list_gateways
from payments.models import Payment
from payments.services import charge_with_idempotency


def _order_company():
    company = Company.objects.create(name="Pay Co")
    make_posting_rules(company)
    customer = Customer.objects.create(
        first_name="Pay", last_name="User", email="pay@example.com", company=company,
    )
    order = Order.objects.create(
        customer=customer, company=company, grand_total=Decimal("200.00"),
    )
    invoice = SalesInvoice.objects.create(
        invoice_number="INV-PAY1",
        order=order,
        company=company,
        grand_total=Decimal("200.00"),
        balance_due=Decimal("200.00"),
    )
    return company, order, invoice


class TestGatewayContract(TestCase):
    """Every gateway honors authorize/capture/refund/settle semantics."""

    def test_usdc_gateway_registered(self):
        self.assertIn("usdc", list_gateways())

    def test_manual_charge_and_refund(self):
        gw = get_gateway("manual")
        r = gw.charge(Decimal("150.00"), "USD", "CHECK-1024")
        self.assertTrue(r.success)
        self.assertEqual(r.status, "settled")
        refund = gw.refund(r.transaction_id, amount=Decimal("50.00"), reason="return")
        self.assertTrue(refund.success)
        self.assertEqual(refund.status, "refunded")

    def test_manual_has_no_separate_auth_capture(self):
        gw = get_gateway("manual")
        with self.assertRaises(NotImplementedError):
            gw.authorize(Decimal("10.00"), "USD", "CASH")
        with self.assertRaises(NotImplementedError):
            gw.capture("MANUAL-123")

    @override_settings(STRIPE_MOCK_MODE=True)
    def test_stripe_authorize_then_capture(self):
        gw = get_gateway("stripe")
        auth = gw.authorize(Decimal("49.99"), "USD", "tok_visa")
        self.assertTrue(auth.success)
        self.assertEqual(auth.status, "authorized")
        cap = gw.capture(auth.transaction_id, amount=Decimal("49.99"))
        self.assertTrue(cap.success)
        self.assertEqual(cap.status, "captured")

    @override_settings(STRIPE_MOCK_MODE=True)
    def test_stripe_charge_and_refund(self):
        gw = get_gateway("stripe")
        r = gw.charge(Decimal("20.00"), "USD", "tok_visa")
        self.assertTrue(r.success)
        refund = gw.refund(r.transaction_id, amount=Decimal("20.00"))
        self.assertTrue(refund.success)

    def test_usdc_charge_refund_and_settle(self):
        gw = get_gateway("usdc")
        r = gw.charge(Decimal("250.00"), "USDC", "0x71CWallet")
        self.assertTrue(r.success)
        self.assertEqual(r.currency, "USDC")
        self.assertTrue(r.transaction_id)
        auth = gw.authorize(Decimal("10.00"), "USDC", "0x71CWallet")
        self.assertTrue(auth.success)
        cap = gw.capture(auth.transaction_id)
        self.assertTrue(cap.success)
        refund = gw.refund(r.transaction_id, amount=Decimal("5.00"))
        self.assertTrue(refund.success)


class TestIdempotency(TestCase):
    def test_retried_post_with_same_key_charges_once(self):
        from payments.gateways.manual import ManualPaymentGateway

        company, order, invoice = _order_company()
        calls = []

        real_charge = ManualPaymentGateway.charge

        def counting_charge(self, *a, **k):
            calls.append(1)
            return real_charge(self, *a, **k)

        with mock.patch.object(
            ManualPaymentGateway, "charge", counting_charge
        ):
            p1 = charge_with_idempotency(
                gateway_id="manual",
                order=order,
                invoice=invoice,
                amount=Decimal("200.00"),
                payment_source="CHECK-1",
                idempotency_key="key-abc-123",
            )
            p2 = charge_with_idempotency(
                gateway_id="manual",
                order=order,
                invoice=invoice,
                amount=Decimal("200.00"),
                payment_source="CHECK-1",
                idempotency_key="key-abc-123",
            )
        self.assertEqual(p1.pk, p2.pk)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            Payment.objects.filter(idempotency_key="key-abc-123").count(), 1
        )

    def test_different_keys_create_distinct_payments(self):
        company, order, invoice = _order_company()
        with mock.patch("payments.services.get_gateway") as gg:
            gg.return_value = get_gateway("manual")
            charge_with_idempotency(
                gateway_id="manual", order=order, invoice=invoice,
                amount=Decimal("50.00"), payment_source="CASH",
                idempotency_key="k-1",
            )
            charge_with_idempotency(
                gateway_id="manual", order=order, invoice=invoice,
                amount=Decimal("50.00"), payment_source="CASH",
                idempotency_key="k-2",
            )
        self.assertEqual(Payment.objects.count(), 2)

    def test_missing_key_still_processes(self):
        company, order, invoice = _order_company()
        with mock.patch("payments.services.get_gateway") as gg:
            gg.return_value = get_gateway("manual")
            p = charge_with_idempotency(
                gateway_id="manual", order=order, invoice=invoice,
                amount=Decimal("10.00"), payment_source="CASH",
                idempotency_key="",
            )
        self.assertTrue(p.transaction_id)


class TestSettlementReconciliation(TestCase):
    def test_payment_ledger_sums_to_invoice_paid(self):
        company, order, invoice = _order_company()
        with mock.patch("payments.services.get_gateway") as gg:
            gg.return_value = get_gateway("manual")
            charge_with_idempotency(
                gateway_id="manual", order=order, invoice=invoice,
                amount=Decimal("120.00"), payment_source="CHECK-A",
                idempotency_key="settle-1",
            )
            charge_with_idempotency(
                gateway_id="manual", order=order, invoice=invoice,
                amount=Decimal("80.00"), payment_source="CHECK-B",
                idempotency_key="settle-2",
            )
        invoice.refresh_from_db()
        ledger_total = sum(
            (p.amount for p in invoice.payments.all()), Decimal("0.00")
        )
        self.assertEqual(ledger_total, invoice.amount_paid)
        self.assertEqual(invoice.balance_due, Decimal("0.00"))
        self.assertEqual(invoice.status, SalesInvoice.Status.PAID)


class TestNoPiiInLogs(TestCase):
    def test_card_pan_never_logged(self):
        pan = "4111111111111111"
        token = "tok_visa_trackc"
        # Root capture: child gateway loggers propagate here.
        with self.assertLogs(level="DEBUG") as logs:
            logging.getLogger("golden_erp.payments.stripe").debug("sentinel")
            gw = get_gateway("manual")
            gw.charge(Decimal("10.00"), "USD", pan)
            with override_settings(STRIPE_MOCK_MODE=True):
                gw2 = get_gateway("stripe")
                gw2.charge(Decimal("10.00"), "USD", token)
                gw2.refund("ch_mock_x", amount=Decimal("1.00"))
            gw3 = get_gateway("usdc")
            gw3.charge(Decimal("1.00"), "USDC", "0x" + pan)
        for line in logs.output:
            self.assertNotIn(pan, line)
            self.assertNotIn(token, line)
