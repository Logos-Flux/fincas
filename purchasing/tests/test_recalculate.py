"""Tests for PurchaseOrder total recalculation."""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from products.models import Product, ProductVariant
from purchasing.models import POLineItem, PurchaseOrder, Supplier
from purchasing.views import PurchaseOrderViewSet


def company_user(company, *, username="staff", uid="uid-staff"):
    """Authenticated, company-bound user. See orders.tests.test_recalculate
    for why the UserProfile is attached in-memory (no FK to auth.User)."""
    user = make_staff_user(username=username, password="pw")
    user.userprofile = UserProfile.objects.create(company=company)
    return user


class PurchaseOrderRecalculateTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.supplier = Supplier.objects.create(
            company_name="Acme Corp", supplier_code="ACME", company=self.company,
        )
        product = Product.objects.create(
            title="Panel", part_number="PNL-RC", slug="pnl-rc"
        )
        self.variant = ProductVariant.objects.create(product=product, sku="PNL-RC-A")

        self.po = PurchaseOrder.objects.create(
            company=self.company,
            supplier=self.supplier,
            shipping_cost=Decimal("75.00"),
            other_charges=Decimal("10.00"),
            amount_paid=Decimal("0"),
        )
        # 10 * 50 = 500, tax 25
        self.line = POLineItem.objects.create(
            purchase_order=self.po,
            variant=self.variant,
            item_name="Panel",
            our_sku="PNL-RC-A",
            quantity_ordered=Decimal("10"),
            unit_cost=Decimal("50.00"),
            line_total=Decimal("500.00"),
            tax_amount=Decimal("25.00"),
        )

    def test_recalculate_sums_lines_and_charges(self):
        self.po.recalculate_totals()
        self.po.refresh_from_db()
        self.assertEqual(self.po.subtotal, Decimal("500.00"))
        self.assertEqual(self.po.tax_total, Decimal("25.00"))
        # 500 - 0 + 25 + 75 + 10 = 610
        self.assertEqual(self.po.grand_total, Decimal("610.00"))
        self.assertEqual(self.po.balance_due, Decimal("610.00"))

    def test_balance_due_subtracts_amount_paid(self):
        self.po.amount_paid = Decimal("100.00")
        self.po.save(update_fields=["amount_paid", "updated_at"])
        self.po.recalculate_totals()
        self.po.refresh_from_db()
        self.assertEqual(self.po.balance_due, Decimal("510.00"))

    def test_signal_fires_on_line_save(self):
        self.line.unit_cost = Decimal("75.00")
        self.line.save()
        self.po.refresh_from_db()
        # 10 * 75 = 750
        self.assertEqual(self.po.subtotal, Decimal("750.00"))

    def test_signal_fires_on_line_delete(self):
        self.line.delete()
        self.po.refresh_from_db()
        self.assertEqual(self.po.subtotal, Decimal("0.00"))

    def test_discount_percent_derives_line_amount(self):
        # 10 * 50 = 500, 5% → discount 25, line_total = 475
        self.line.discount_percent = Decimal("5.00")
        self.line.save()
        self.line.refresh_from_db()
        self.assertEqual(self.line.discount_amount, Decimal("25.00"))
        self.assertEqual(self.line.line_total, Decimal("475.00"))
        self.po.refresh_from_db()
        self.assertEqual(self.po.subtotal, Decimal("475.00"))

    def test_drf_recalculate_action(self):
        factory = APIRequestFactory()
        request = factory.post(
            f"/api/v1/purchasing/purchase-orders/{self.po.id}/recalculate/"
        )
        force_authenticate(request, user=company_user(self.company))
        view = PurchaseOrderViewSet.as_view({"post": "recalculate"})
        response = view(request, pk=str(self.po.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Decimal(response.data["grand_total"]), Decimal("610.00"))

    def test_drf_recalculate_scoped_to_company(self):
        """Another company's user gets 404, anonymous gets 404 — fail-closed."""
        factory = APIRequestFactory()
        view = PurchaseOrderViewSet.as_view({"post": "recalculate"})

        other_company = Company.objects.create(name="Rival, Inc")
        outsider = company_user(other_company, username="rival", uid="uid-rival")
        req_outsider = factory.post(
            f"/api/v1/purchasing/purchase-orders/{self.po.id}/recalculate/"
        )
        force_authenticate(req_outsider, user=outsider)
        self.assertEqual(view(req_outsider, pk=str(self.po.id)).status_code, 404)

        req_anon = factory.post(
            f"/api/v1/purchasing/purchase-orders/{self.po.id}/recalculate/"
        )
        force_authenticate(req_anon, user=None)
        # Explicit IsAuthenticated (F-003): anonymous is rejected at the
        # auth layer before scoping runs — 401/403, never data.
        self.assertIn(view(req_anon, pk=str(self.po.id)).status_code, (401, 403))


class POLineEditTierLandedCostTests(TestCase):
    """Line edits, supplier tier-cost changes, and landed-cost isolation.

    Landed-cost fields (freight/duty/other + landed_cost_per_unit) feed
    inventory valuation, not the payable: changing them must never move
    ``grand_total`` / ``balance_due``. Since F-016 the allocation itself
    is server-side math — freight/other/landed are computed by
    recalculate_totals() and overwritten on every recalculation, while
    duty_amount stays a caller-written input.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.supplier = Supplier.objects.create(
            company_name="Acme Corp", supplier_code="ACME2", company=self.company,
        )
        product = Product.objects.create(
            title="Panel", part_number="PNL-T", slug="pnl-t"
        )
        self.variant = ProductVariant.objects.create(product=product, sku="PNL-T-A")
        self.po = PurchaseOrder.objects.create(
            company=self.company,
            supplier=self.supplier,
            shipping_cost=Decimal("75.00"),
            other_charges=Decimal("10.00"),
        )
        self.line = POLineItem.objects.create(
            purchase_order=self.po,
            variant=self.variant,
            item_name="Panel",
            our_sku="PNL-T-A",
            quantity_ordered=Decimal("10"),
            unit_cost=Decimal("50.00"),
            line_total=Decimal("500.00"),
            tax_amount=Decimal("25.00"),
        )

    def test_quantity_edit_recalculates_parent(self):
        self.line.quantity_ordered = Decimal("20")
        self.line.save()
        self.po.refresh_from_db()
        # 20 * 50 = 1000; grand = 1000 + 25 + 75 + 10
        self.assertEqual(self.po.subtotal, Decimal("1000.00"))
        self.assertEqual(self.po.grand_total, Decimal("1110.00"))

    def test_supplier_tier_cost_change_recalculates(self):
        # Volume break renegotiated: unit cost 50 -> 42 at 10+ units.
        self.line.unit_cost = Decimal("42.00")
        self.line.save()
        self.line.refresh_from_db()
        self.po.refresh_from_db()
        self.assertEqual(self.line.line_total, Decimal("420.00"))
        self.assertEqual(self.po.subtotal, Decimal("420.00"))
        self.assertEqual(self.po.grand_total, Decimal("530.00"))

    def test_half_cent_line_total_rounds_half_up(self):
        # 3 x 16.665 = 49.995 -> 50.00 (HALF_UP).
        self.line.quantity_ordered = Decimal("3")
        self.line.unit_cost = Decimal("16.6650")
        self.line.save()
        self.line.refresh_from_db()
        self.assertEqual(self.line.line_total, Decimal("50.00"))

    def test_landed_costs_computed_without_moving_payable(self):
        # Written landed values are overwritten by the computed allocation
        # (F-016); only duty_amount survives as a caller-written input.
        self.line.freight_allocation = Decimal("1.00")
        self.line.duty_amount = Decimal("12.50")
        self.line.other_landed_costs = Decimal("2.00")
        self.line.landed_cost_per_unit = Decimal("59.2500")
        self.line.save()
        self.po.recalculate_totals()
        self.po.refresh_from_db()
        # Payable untouched: 500 + 25 + 75 + 10 = 610.
        self.assertEqual(self.po.subtotal, Decimal("500.00"))
        self.assertEqual(self.po.grand_total, Decimal("610.00"))
        self.assertEqual(self.po.balance_due, Decimal("610.00"))
        self.line.refresh_from_db()
        self.assertEqual(self.line.duty_amount, Decimal("12.50"))
        self.assertEqual(self.line.freight_allocation, Decimal("75.00"))
        self.assertEqual(self.line.other_landed_costs, Decimal("10.00"))
        # 50 + (75 + 12.50 + 10) / 10
        self.assertEqual(self.line.landed_cost_per_unit, Decimal("59.7500"))

    def test_drf_line_patch_recalculates_parent(self):
        from purchasing.views import POLineItemViewSet
        factory = APIRequestFactory()
        request = factory.patch(
            f"/api/v1/purchasing/po-lines/{self.line.id}/",
            {"quantity_ordered": "4"},
            format="json",
        )
        force_authenticate(request, user=company_user(self.company))
        view = POLineItemViewSet.as_view({"patch": "partial_update"})
        response = view(request, pk=str(self.line.id))
        self.assertEqual(response.status_code, 200)
        self.po.refresh_from_db()
        self.assertEqual(self.po.subtotal, Decimal("200.00"))
