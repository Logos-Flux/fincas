"""F-016: server-side landed-cost allocation, weighted by line value.

Each line absorbs PO shipping_cost / other_charges in proportion to its
share of subtotal. duty_amount stays a caller-written per-line input
(customs actuals); freight_allocation, other_landed_costs and
landed_cost_per_unit are server-computed by recalculate_totals() and
rejected on direct DRF writes (F-045 style enforcement).
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import Company
from products.models import Product, ProductVariant
from purchasing.models import POLineItem, PurchaseOrder, Supplier
from purchasing.views import POLineItemViewSet, PurchaseOrderViewSet

from .test_recalculate import company_user


def _fixture(tag="LC"):
    company = Company.objects.create(name="Example Supply, Inc")
    supplier = Supplier.objects.create(
        company_name="Acme Corp", supplier_code=f"ACME-{tag}", company=company,
    )
    product = Product.objects.create(
        title="Panel", part_number=f"PNL-{tag}", slug=f"pnl-{tag.lower()}",
    )
    variant = ProductVariant.objects.create(product=product, sku=f"PNL-{tag}-A")
    return company, supplier, variant


def _line(po, variant, *, sku, qty, unit, duty="0"):
    return POLineItem.objects.create(
        purchase_order=po,
        variant=variant,
        item_name="Panel",
        our_sku=sku,
        quantity_ordered=Decimal(qty),
        unit_cost=Decimal(unit),
        duty_amount=Decimal(duty),
    )


class LandedCostAllocationTests(TestCase):
    def setUp(self):
        self.company, self.supplier, self.variant = _fixture()
        self.po = PurchaseOrder.objects.create(
            company=self.company,
            supplier=self.supplier,
            shipping_cost=Decimal("75.00"),
            other_charges=Decimal("10.00"),
        )

    def test_weighted_split_across_two_lines(self):
        # A: 10 x 50 = 500 (25%), B: 10 x 150 = 1500 (75%), subtotal 2000.
        a = _line(self.po, self.variant, sku="A", qty="10", unit="50.00")
        b = _line(self.po, self.variant, sku="B", qty="10", unit="150.00")
        self.po.recalculate_totals()
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.freight_allocation, Decimal("18.75"))
        self.assertEqual(a.other_landed_costs, Decimal("2.50"))
        self.assertEqual(a.landed_cost_per_unit, Decimal("52.1250"))
        self.assertEqual(b.freight_allocation, Decimal("56.25"))
        self.assertEqual(b.other_landed_costs, Decimal("7.50"))
        self.assertEqual(b.landed_cost_per_unit, Decimal("156.3750"))
        # The full pool is absorbed, split to the cent.
        self.assertEqual(
            a.freight_allocation + b.freight_allocation, Decimal("75.00")
        )
        self.assertEqual(
            a.other_landed_costs + b.other_landed_costs, Decimal("10.00")
        )

    def test_duty_is_preserved_and_flows_into_landed(self):
        line = _line(self.po, self.variant, sku="A", qty="10", unit="50.00", duty="12.50")
        self.po.recalculate_totals()
        line.refresh_from_db()
        self.assertEqual(line.duty_amount, Decimal("12.50"))
        self.assertEqual(line.freight_allocation, Decimal("75.00"))
        self.assertEqual(line.other_landed_costs, Decimal("10.00"))
        # 50 + (75 + 12.50 + 10) / 10
        self.assertEqual(line.landed_cost_per_unit, Decimal("59.7500"))

    def test_zero_subtotal_allocates_nothing(self):
        line = _line(self.po, self.variant, sku="A", qty="0", unit="50.00")
        self.po.recalculate_totals()
        line.refresh_from_db()
        self.assertEqual(line.freight_allocation, Decimal("0.00"))
        self.assertEqual(line.other_landed_costs, Decimal("0.00"))
        self.assertEqual(line.landed_cost_per_unit, Decimal("50.0000"))

    def test_payable_excludes_landed_costs(self):
        _line(self.po, self.variant, sku="A", qty="10", unit="50.00", duty="12.50")
        self.po.recalculate_totals()
        self.po.refresh_from_db()
        self.assertEqual(self.po.subtotal, Decimal("500.00"))
        self.assertEqual(self.po.grand_total, Decimal("585.00"))
        self.assertEqual(self.po.balance_due, Decimal("585.00"))

    def test_recalculate_action_returns_allocation(self):
        _line(self.po, self.variant, sku="A", qty="10", unit="50.00")
        factory = APIRequestFactory()
        request = factory.post(
            f"/api/v1/purchasing/purchase-orders/{self.po.id}/recalculate/"
        )
        force_authenticate(request, user=company_user(self.company))
        view = PurchaseOrderViewSet.as_view({"post": "recalculate"})
        response = view(request, pk=str(self.po.id))
        self.assertEqual(response.status_code, 200)
        (line_data,) = response.data["line_items"]
        self.assertEqual(line_data["freight_allocation"], "75.00")
        self.assertEqual(line_data["other_landed_costs"], "10.00")
        self.assertEqual(line_data["landed_cost_per_unit"], "58.5000")


class LandedCostWriteEnforcementTests(TestCase):
    def setUp(self):
        self.company, self.supplier, self.variant = _fixture("LE")
        self.user = company_user(self.company)
        self.po = PurchaseOrder.objects.create(
            company=self.company,
            supplier=self.supplier,
            shipping_cost=Decimal("75.00"),
            other_charges=Decimal("10.00"),
        )
        self.line = _line(self.po, self.variant, sku="A", qty="10", unit="50.00")

    def _patch_line(self, payload):
        factory = APIRequestFactory()
        request = factory.patch(
            f"/api/v1/purchasing/po-lines/{self.line.id}/",
            payload,
            format="json",
        )
        force_authenticate(request, user=self.user)
        view = POLineItemViewSet.as_view({"patch": "partial_update"})
        return view(request, pk=str(self.line.id))

    def test_patch_computed_landed_fields_rejected(self):
        for field in (
            "freight_allocation",
            "other_landed_costs",
            "landed_cost_per_unit",
        ):
            with self.subTest(field=field):
                response = self._patch_line({field: "9999.99"})
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)
        self.line.refresh_from_db()
        self.assertEqual(self.line.freight_allocation, Decimal("75.00"))
        self.assertEqual(self.line.landed_cost_per_unit, Decimal("58.5000"))

    def test_patch_duty_allowed_and_recomputes_landed(self):
        response = self._patch_line({"duty_amount": "20.00"})
        self.assertEqual(response.status_code, 200)
        self.line.refresh_from_db()
        self.assertEqual(self.line.duty_amount, Decimal("20.00"))
        # 50 + (75 + 20 + 10) / 10
        self.assertEqual(self.line.landed_cost_per_unit, Decimal("60.5000"))
