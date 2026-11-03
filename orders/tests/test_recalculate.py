"""Tests for parent-document total recalculation.

Covers Order, Quote, and SalesInvoice. Verifies:
- recalculate_totals() derives line totals + parent aggregates correctly
- post_save / post_delete signals on line items propagate to parent
- discount_percent gets re-derived to discount_amount on the line
- SalesInvoice recalc re-snapshots from Order, no-ops on finalized invoices
- DRF /recalculate/ action returns refreshed totals
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from accounting.tests.test_posting import make_posting_rules
from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from customers.models import Customer
from orders.models import Order, OrderLineItem, Quote, QuoteLineItem, SalesInvoice
from orders.views import OrderViewSet, QuoteViewSet, SalesInvoiceViewSet


def company_user(company, *, username="staff", uid="uid-staff"):
    """Build an authenticated, company-bound user.

    CompanyScopedManager.for_request() scopes rows to
    request.user.userprofile.company. In production the SupabaseJWT
    middleware attaches that UserProfile to request.user; UserProfile has
    no DB-level FK to auth.User, so here we attach it in-memory to mirror
    exactly what the request carries once authenticated.
    """
    user = make_staff_user(username=username, password="pw")
    user.userprofile = UserProfile.objects.create(company=company)
    return user


class OrderRecalculateTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        make_posting_rules(self.company)
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="recalc@example.com", company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer,
            company=self.company,
            shipping_cost_ex_tax=Decimal("100.00"),
            handling_fee=Decimal("25.00"),
            amount_paid=Decimal("0"),
        )
        # Two lines: 2 @ $50 = $100, 3 @ $200 = $600  → subtotal $700
        self.line_a = OrderLineItem.objects.create(
            order=self.order,
            item_name="A",
            sku="A-1",
            quantity=Decimal("2"),
            unit_price_ex_tax=Decimal("50.00"),
            line_total_ex_tax=Decimal("100.00"),
            line_tax=Decimal("5.00"),
        )
        self.line_b = OrderLineItem.objects.create(
            order=self.order,
            item_name="B",
            sku="B-1",
            quantity=Decimal("3"),
            unit_price_ex_tax=Decimal("200.00"),
            line_total_ex_tax=Decimal("600.00"),
            line_tax=Decimal("10.00"),
        )

    def test_recalculate_sums_lines_and_charges(self):
        # The auto-recalc signals will already have fired during setUp; we
        # call again explicitly to confirm idempotency.
        self.order.recalculate_totals()
        self.order.refresh_from_db()

        self.assertEqual(self.order.subtotal_ex_tax, Decimal("700.00"))
        self.assertEqual(self.order.tax_total, Decimal("15.00"))
        # subtotal - discount + tax + shipping + handling
        self.assertEqual(
            self.order.grand_total,
            Decimal("700.00") - Decimal("0") + Decimal("15.00") + Decimal("100.00") + Decimal("25.00"),
        )
        self.assertEqual(self.order.subtotal_inc_tax, Decimal("715.00"))
        # balance_due = grand_total - payments - credits - max(coupon - discount, 0);
        # no coupon here, so it equals grand_total.
        self.assertEqual(self.order.balance_due, self.order.grand_total)

    def test_balance_due_subtracts_payments_and_credits(self):
        self.order.amount_paid = Decimal("100.00")
        self.order.store_credit_amount = Decimal("25.00")
        self.order.save(update_fields=["amount_paid", "store_credit_amount", "updated_at"])
        self.order.recalculate_totals()
        self.order.refresh_from_db()
        # grand_total $840.00 - 100 - 25 = $715.00
        self.assertEqual(self.order.balance_due, Decimal("715.00"))

    def test_coupon_discount_is_not_subtracted_again(self):
        # orders-core.ts mirrors a web coupon's value into BOTH
        # discount_amount and coupon_discount for reporting; the mirrored
        # portion must be pure metadata here, or balance_due double-counts
        # it (WP #572 BUG-06 -- live coupon orders went negative).
        self.order.discount_amount = Decimal("50.00")
        self.order.coupon_discount = Decimal("50.00")
        self.order.save(update_fields=["discount_amount", "coupon_discount", "updated_at"])
        self.order.recalculate_totals()
        self.order.refresh_from_db()
        # grand_total = 700 - 50 + 15 + 100 + 25 = 790; coupon fully mirrored
        # so nothing extra is subtracted: balance_due = 790.
        self.assertEqual(self.order.grand_total, Decimal("790.00"))
        self.assertEqual(self.order.balance_due, Decimal("790.00"))

    def test_standalone_coupon_is_subtracted(self):
        # Admin / DRF / legacy rows can carry a coupon_discount with NO
        # mirroring discount_amount -- that portion is a real deduction,
        # not reporting metadata (regression for the adversarial review of
        # WP #572 BUG-06).
        self.order.coupon_discount = Decimal("50.00")
        self.order.save(update_fields=["coupon_discount", "updated_at"])
        self.order.recalculate_totals()
        self.order.refresh_from_db()
        # grand_total unaffected (coupon not in discount_amount): 840
        self.assertEqual(self.order.grand_total, Decimal("840.00"))
        # balance_due = 840 - 0 - 0 - max(50 - 0, 0) = 790
        self.assertEqual(self.order.balance_due, Decimal("790.00"))

    def test_partial_mirror_subtracts_only_non_mirrored_portion(self):
        # Coupon larger than discount_amount: only the non-mirrored excess
        # counts against balance_due (never the mirrored part). Invariant:
        # the coupon is deducted exactly once in total -- min(coupon,
        # discount_amount) via grand_total + max(coupon - discount_amount, 0)
        # via balance_due == coupon. Here 30 + 20 == 50 == coupon.
        self.order.discount_amount = Decimal("30.00")
        self.order.coupon_discount = Decimal("50.00")
        self.order.save(update_fields=["discount_amount", "coupon_discount", "updated_at"])
        self.order.recalculate_totals()
        self.order.refresh_from_db()
        # grand_total = 700 - 30 + 15 + 100 + 25 = 810
        self.assertEqual(self.order.grand_total, Decimal("810.00"))
        # balance_due = 810 - max(50 - 30, 0) = 790
        self.assertEqual(self.order.balance_due, Decimal("790.00"))

    def test_non_coupon_discount_without_coupon_is_unaffected(self):
        # discount_amount set independently with NO coupon: nothing extra is
        # subtracted from balance_due (coupon == 0 -> excess 0).
        self.order.discount_amount = Decimal("40.00")
        self.order.save(update_fields=["discount_amount", "updated_at"])
        self.order.recalculate_totals()
        self.order.refresh_from_db()
        # grand_total = 700 - 40 + 15 + 100 + 25 = 800
        self.assertEqual(self.order.grand_total, Decimal("800.00"))
        self.assertEqual(self.order.balance_due, Decimal("800.00"))

    def test_discount_larger_than_coupon_subtracts_nothing_extra(self):
        # A non-coupon discount BIGGER than the coupon. The excess term must
        # clamp at zero rather than going negative and ADDING money back to
        # balance_due -- max() is the whole guard, so it gets its own test.
        self.order.discount_amount = Decimal("60.00")
        self.order.coupon_discount = Decimal("20.00")
        self.order.save(update_fields=["discount_amount", "coupon_discount", "updated_at"])
        self.order.recalculate_totals()
        self.order.refresh_from_db()
        # grand_total = 700 - 60 + 15 + 100 + 25 = 780; excess = max(20-60,0) = 0
        self.assertEqual(self.order.grand_total, Decimal("780.00"))
        self.assertEqual(self.order.balance_due, Decimal("780.00"))

    def test_coupon_interacts_correctly_with_payments_and_credits(self):
        # The original combined test lost its coupon leg when the coupon cases
        # were split out, so nothing asserted the excess deduction ALONGSIDE
        # amount_paid / store_credit_amount. Both coupon shapes, one test.
        self.order.amount_paid = Decimal("100.00")
        self.order.store_credit_amount = Decimal("25.00")
        self.order.discount_amount = Decimal("50.00")
        self.order.coupon_discount = Decimal("50.00")  # fully mirrored
        self.order.save(update_fields=[
            "amount_paid", "store_credit_amount", "discount_amount",
            "coupon_discount", "updated_at",
        ])
        self.order.recalculate_totals()
        self.order.refresh_from_db()
        # grand_total = 700 - 50 + 15 + 100 + 25 = 790
        # balance_due = 790 - 100 - 25 - max(50-50,0) = 665
        self.assertEqual(self.order.grand_total, Decimal("790.00"))
        self.assertEqual(self.order.balance_due, Decimal("665.00"))

        # Same order, now a STANDALONE coupon (no mirroring discount).
        self.order.discount_amount = Decimal("0.00")
        self.order.save(update_fields=["discount_amount", "updated_at"])
        self.order.recalculate_totals()
        self.order.refresh_from_db()
        # grand_total = 840; balance_due = 840 - 100 - 25 - max(50-0,0) = 665
        self.assertEqual(self.order.grand_total, Decimal("840.00"))
        self.assertEqual(self.order.balance_due, Decimal("665.00"))

    def test_signal_fires_on_line_save(self):
        # Mutate one line and confirm parent follows without explicit recalc.
        self.line_a.unit_price_ex_tax = Decimal("75.00")
        self.line_a.save()
        self.order.refresh_from_db()
        # New subtotal: 2*75 + 3*200 = 750
        self.assertEqual(self.order.subtotal_ex_tax, Decimal("750.00"))

    def test_signal_fires_on_line_delete(self):
        self.line_b.delete()
        self.order.refresh_from_db()
        # Only line_a remains: 2 * 50 = 100
        self.assertEqual(self.order.subtotal_ex_tax, Decimal("100.00"))

    def test_discount_percent_derives_line_amount(self):
        # 2 * 50 = 100, 10% discount → discount_amount = 10, line_total = 90
        self.line_a.discount_percent = Decimal("10.00")
        self.line_a.save()
        self.line_a.refresh_from_db()
        self.assertEqual(self.line_a.discount_amount, Decimal("10.00"))
        self.assertEqual(self.line_a.line_total_ex_tax, Decimal("90.00"))
        self.order.refresh_from_db()
        # subtotal: 90 + 600 = 690
        self.assertEqual(self.order.subtotal_ex_tax, Decimal("690.00"))

    def test_parent_discount_percent_applies_to_subtotal(self):
        self.order.discount_percent = Decimal("10.00")
        self.order.save(update_fields=["discount_percent", "updated_at"])
        self.order.recalculate_totals()
        self.order.refresh_from_db()
        # subtotal 700, 10% = 70 discount
        self.assertEqual(self.order.discount_amount, Decimal("70.00"))
        # grand: 700 - 70 + 15 + 100 + 25 = 770
        self.assertEqual(self.order.grand_total, Decimal("770.00"))

    def test_drf_recalculate_action(self):
        factory = APIRequestFactory()
        request = factory.post(f"/api/v1/orders/orders/{self.order.id}/recalculate/")
        force_authenticate(request, user=company_user(self.company))
        view = OrderViewSet.as_view({"post": "recalculate"})
        response = view(request, pk=str(self.order.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Decimal(response.data["subtotal_ex_tax"]), Decimal("700.00"))
        self.assertEqual(Decimal(response.data["grand_total"]), Decimal("840.00"))

    def test_drf_recalculate_scoped_to_company(self):
        """A user from another company cannot reach the order (404), and an
        unauthenticated request is filtered out too — SOL-16 fail-closed."""
        factory = APIRequestFactory()
        view = OrderViewSet.as_view({"post": "recalculate"})

        other_company = Company.objects.create(name="Rival, Inc")
        outsider = company_user(other_company, username="rival", uid="uid-rival")
        req_outsider = factory.post(
            f"/api/v1/orders/orders/{self.order.id}/recalculate/"
        )
        force_authenticate(req_outsider, user=outsider)
        self.assertEqual(view(req_outsider, pk=str(self.order.id)).status_code, 404)

        req_anon = factory.post(f"/api/v1/orders/orders/{self.order.id}/recalculate/")
        force_authenticate(req_anon, user=None)
        # Explicit IsAuthenticated (F-003): anonymous is rejected at the
        # auth layer before scoping runs — 401/403, never data.
        self.assertIn(view(req_anon, pk=str(self.order.id)).status_code, (401, 403))


class OrderLineEditTierCouponTests(TestCase):
    """Line edits, tier-price changes, and coupon apply/remove on Orders.

    Tier prices live on ``PriceListItem`` (min_qty breaks); no server code
    applies them automatically — staff, the API, or checkout writes the
    resolved unit price onto the line. These tests pin that once the line
    carries the new price, every aggregate follows, and that coupons move
    ``balance_due`` symmetrically on apply AND on removal.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        make_posting_rules(self.company)
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="tier@example.com", company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer,
            company=self.company,
            shipping_cost_ex_tax=Decimal("100.00"),
            handling_fee=Decimal("25.00"),
        )
        self.line = OrderLineItem.objects.create(
            order=self.order,
            item_name="Widget",
            sku="W-1",
            quantity=Decimal("2"),
            unit_price_ex_tax=Decimal("50.00"),
            line_total_ex_tax=Decimal("100.00"),
            line_tax=Decimal("5.00"),
        )
        self.order.refresh_from_db()

    def _tier_price(self, qty):
        from pricing.models.price_book import PriceList, PriceListItem
        from products.models import Product, ProductVariant
        product = Product.objects.create(title="Widget", part_number="W-1", slug="w-1-t")
        variant = ProductVariant.objects.create(product=product, sku="W-1-VT")
        price_list = PriceList.objects.create(
            name="Tier List", price_list_type=PriceList.PriceListType.SELLING,
            company=self.company,
        )
        PriceListItem.objects.create(
            price_list=price_list, variant=variant,
            price=Decimal("50.0000"), min_qty=Decimal("1"),
        )
        PriceListItem.objects.create(
            price_list=price_list, variant=variant,
            price=Decimal("45.0000"), min_qty=Decimal("10"),
        )
        tier = (
            PriceListItem.objects.filter(
                price_list=price_list, variant=variant,
                min_qty__lte=qty, is_active=True,
            )
            .order_by("-min_qty")
            .first()
        )
        return tier.price

    def test_quantity_edit_recalculates_parent(self):
        self.line.quantity = Decimal("5")
        self.line.save()
        self.order.refresh_from_db()
        # 5 * 50 = 250; grand = 250 + 5 tax + 100 ship + 25 handling
        self.assertEqual(self.order.subtotal_ex_tax, Decimal("250.00"))
        self.assertEqual(self.order.grand_total, Decimal("380.00"))

    def test_tier_price_change_recalculates(self):
        # Customer crosses the 10+ break: unit price drops to the tier.
        self.line.quantity = Decimal("10")
        self.line.unit_price_ex_tax = self._tier_price(Decimal("10"))
        self.line.save()
        self.line.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.line.unit_price_ex_tax, Decimal("45.0000"))
        self.assertEqual(self.line.line_total_ex_tax, Decimal("450.00"))
        self.assertEqual(self.order.subtotal_ex_tax, Decimal("450.00"))
        self.assertEqual(self.order.grand_total, Decimal("580.00"))

    def test_tier_price_with_line_discount_percent(self):
        self.line.quantity = Decimal("10")
        self.line.unit_price_ex_tax = self._tier_price(Decimal("10"))
        self.line.discount_percent = Decimal("10.00")
        self.line.save()
        self.line.refresh_from_db()
        self.order.refresh_from_db()
        # 10 * 45 = 450, 10% -> 45 off, line 405
        self.assertEqual(self.line.discount_amount, Decimal("45.00"))
        self.assertEqual(self.line.line_total_ex_tax, Decimal("405.00"))
        self.assertEqual(self.order.subtotal_ex_tax, Decimal("405.00"))

    def test_half_cent_line_total_rounds_half_up(self):
        # 3 x 33.335 = 100.005 -> 100.01 (HALF_UP, not banker's 100.00).
        self.line.quantity = Decimal("3")
        self.line.unit_price_ex_tax = Decimal("33.3350")
        self.line.save()
        self.line.refresh_from_db()
        self.assertEqual(self.line.line_total_ex_tax, Decimal("100.01"))

    def test_coupon_apply_then_remove_is_symmetric(self):
        self.order.refresh_from_db()
        base = self.order.grand_total
        self.assertEqual(base, Decimal("230.00"))  # 100 + 5 + 100 + 25
        # Apply: standalone coupon nets against balance_due only.
        self.order.coupon_discount = Decimal("50.00")
        self.order.save(update_fields=["coupon_discount", "updated_at"])
        self.order.recalculate_totals()
        self.order.refresh_from_db()
        self.assertEqual(self.order.grand_total, Decimal("230.00"))
        self.assertEqual(self.order.balance_due, Decimal("180.00"))
        # Remove: balance_due returns to exactly the pre-coupon figure.
        self.order.coupon_discount = Decimal("0.00")
        self.order.save(update_fields=["coupon_discount", "updated_at"])
        self.order.recalculate_totals()
        self.order.refresh_from_db()
        self.assertEqual(self.order.balance_due, Decimal("230.00"))

    def test_drf_line_patch_recalculates_parent(self):
        factory = APIRequestFactory()
        from orders.views import OrderLineItemViewSet
        request = factory.patch(
            f"/api/v1/orders/order-lines/{self.line.id}/",
            {"quantity": "4"},
            format="json",
        )
        force_authenticate(request, user=company_user(self.company))
        view = OrderLineItemViewSet.as_view({"patch": "partial_update"})
        response = view(request, pk=str(self.line.id))
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.subtotal_ex_tax, Decimal("200.00"))


class QuoteCouponTests(TestCase):
    """Coupon semantics on quotes mirror orders: ``coupon_discount`` is
    preserved on the document but never double-deducted — the quote has no
    balance_due leg, so ``grand_total`` follows subtotal/discount/tax/
    shipping alone, exactly like an order's grand_total."""

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        make_posting_rules(self.company)
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="qcoupon@example.com", company=self.company,
        )
        self.quote = Quote.objects.create(
            customer=self.customer, company=self.company,
            shipping_cost_ex_tax=Decimal("50.00"),
        )
        QuoteLineItem.objects.create(
            quote=self.quote,
            item_name="Widget",
            sku="W-1-V1",
            quantity=Decimal("4"),
            unit_price_ex_tax=Decimal("25.00"),
            line_total_ex_tax=Decimal("100.00"),
            line_tax=Decimal("8.00"),
        )

    def test_coupon_apply_and_remove_leaves_grand_total_stable(self):
        self.quote.coupon_discount = Decimal("20.00")
        self.quote.save(update_fields=["coupon_discount", "updated_at"])
        self.quote.recalculate_totals()
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.coupon_discount, Decimal("20.00"))
        self.assertEqual(self.quote.grand_total, Decimal("158.00"))
        self.quote.coupon_discount = Decimal("0.00")
        self.quote.save(update_fields=["coupon_discount", "updated_at"])
        self.quote.recalculate_totals()
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.grand_total, Decimal("158.00"))


class QuoteRecalculateTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        make_posting_rules(self.company)
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="qrecalc@example.com", company=self.company,
        )
        from products.models import Product, ProductVariant
        product = Product.objects.create(title="Widget", part_number="W-1", slug="w-1")
        self.variant = ProductVariant.objects.create(product=product, sku="W-1-V1")
        self.quote = Quote.objects.create(
            customer=self.customer, company=self.company,
            shipping_cost_ex_tax=Decimal("50.00"),
        )
        QuoteLineItem.objects.create(
            quote=self.quote,
            variant=self.variant,
            item_name="Widget",
            sku="W-1-V1",
            quantity=Decimal("4"),
            unit_price_ex_tax=Decimal("25.00"),
            line_total_ex_tax=Decimal("100.00"),
            line_tax=Decimal("8.00"),
        )

    def test_recalculate_quote_totals(self):
        self.quote.recalculate_totals()
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.subtotal_ex_tax, Decimal("100.00"))
        self.assertEqual(self.quote.tax_total, Decimal("8.00"))
        # subtotal + tax + shipping
        self.assertEqual(self.quote.grand_total, Decimal("158.00"))

    def test_drf_recalculate_action(self):
        factory = APIRequestFactory()
        request = factory.post(f"/api/v1/orders/quotes/{self.quote.id}/recalculate/")
        force_authenticate(request, user=company_user(self.company))
        view = QuoteViewSet.as_view({"post": "recalculate"})
        response = view(request, pk=str(self.quote.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Decimal(response.data["grand_total"]), Decimal("158.00"))


class SalesInvoiceRecalculateTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        make_posting_rules(self.company)
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="invrecalc@example.com", company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer,
            company=self.company,
            subtotal_ex_tax=Decimal("500.00"),
            tax_total=Decimal("40.00"),
            shipping_cost_ex_tax=Decimal("60.00"),
            grand_total=Decimal("600.00"),
        )
        OrderLineItem.objects.create(
            order=self.order,
            item_name="Item",
            sku="ITEM-1",
            quantity=Decimal("1"),
            unit_price_ex_tax=Decimal("500.00"),
            line_total_ex_tax=Decimal("500.00"),
            line_tax=Decimal("40.00"),
        )
        # Re-pin order totals because the line signal will have rewritten
        # them based on the single line we just made.
        self.order.refresh_from_db()
        self.invoice = SalesInvoice.objects.create(
            order=self.order,
            company=self.company,
            status=SalesInvoice.Status.SENT,
            subtotal_ex_tax=Decimal("0"),
            tax_total=Decimal("0"),
            shipping_cost=Decimal("0"),
            grand_total=Decimal("0"),
            balance_due=Decimal("0"),
            amount_paid=Decimal("0"),
        )

    def test_recalculate_resnaps_from_order(self):
        self.invoice.recalculate_totals()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.subtotal_ex_tax, self.order.subtotal_ex_tax)
        self.assertEqual(self.invoice.tax_total, self.order.tax_total)
        self.assertEqual(self.invoice.shipping_cost, self.order.shipping_cost_ex_tax)
        self.assertEqual(self.invoice.grand_total, self.order.grand_total)
        self.assertEqual(self.invoice.balance_due, self.order.grand_total)

    def test_paid_invoice_is_noop(self):
        self.invoice.status = SalesInvoice.Status.PAID
        self.invoice.subtotal_ex_tax = Decimal("999.99")
        self.invoice.save(update_fields=["status", "subtotal_ex_tax", "updated_at"])
        self.invoice.recalculate_totals()
        self.invoice.refresh_from_db()
        # Untouched.
        self.assertEqual(self.invoice.subtotal_ex_tax, Decimal("999.99"))

    def test_drf_recalculate_action(self):
        factory = APIRequestFactory()
        request = factory.post(
            f"/api/v1/orders/sales-invoices/{self.invoice.id}/recalculate/"
        )
        force_authenticate(request, user=company_user(self.company))
        view = SalesInvoiceViewSet.as_view({"post": "recalculate"})
        response = view(request, pk=str(self.invoice.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            Decimal(response.data["grand_total"]), self.order.grand_total
        )
