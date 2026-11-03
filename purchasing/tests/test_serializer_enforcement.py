"""Serializer enforcement on PurchaseOrder: direct status/totals writes rejected.

Same contract as the order-side enforcement tests
(orders/tests/test_serializer_enforcement.py): PATCH of ``status`` → 400
even for a map-legal move (the ``transition`` action is the only writer),
PATCH of server-computed totals → 400 with the value unchanged, and
``POST .../recalculate/`` derives totals from the line items.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from products.models import Product, ProductVariant
from purchasing.models import POLineItem, PurchaseOrder, Supplier
from purchasing.views import PurchaseOrderViewSet


def company_user(company, *, username, uid):
    user = make_staff_user(username=username, password="pw")
    user.userprofile = UserProfile.objects.create(company=company)
    return user


def patch_detail(pk, user, payload):
    factory = APIRequestFactory()
    request = factory.patch("/api/v1/patch/", payload, format="json")
    force_authenticate(request, user=user)
    view = PurchaseOrderViewSet.as_view({"patch": "partial_update"})
    return view(request, pk=str(pk))


def post_action(action_name, pk, user, payload=None):
    factory = APIRequestFactory()
    request = factory.post("/api/v1/action/", payload or {}, format="json")
    force_authenticate(request, user=user)
    view = PurchaseOrderViewSet.as_view({"post": action_name})
    return view(request, pk=str(pk))


class PurchaseOrderDirectWriteTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.supplier = Supplier.objects.create(
            company_name="Acme Corp", supplier_code="ACME-SE", company=self.company,
        )
        self.user = company_user(self.company, username="se-po", uid="uid-se-po")
        self.po = PurchaseOrder.objects.create(
            company=self.company, supplier=self.supplier,
            status=PurchaseOrder.Status.DRAFT,
        )

    def test_patch_status_rejected_even_for_legal_move(self):
        # sent IS legal from draft; direct PATCH is still rejected.
        response = patch_detail(self.po.pk, self.user, {"status": "sent"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.data)
        self.assertNotIn("detail", response.data)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.Status.DRAFT)

    def test_patch_status_rejected_for_illegal_move(self):
        response = patch_detail(self.po.pk, self.user, {"status": "received"})
        self.assertEqual(response.status_code, 400)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.Status.DRAFT)

    def test_patch_server_totals_rejected(self):
        for field in ("grand_total", "subtotal", "tax_total",
                      "balance_due", "amount_paid"):
            with self.subTest(field=field):
                response = patch_detail(self.po.pk, self.user, {field: "9999.99"})
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)
                self.po.refresh_from_db()
                self.assertEqual(getattr(self.po, field), Decimal("0.00"))

    def test_patch_misc_fields_still_allowed(self):
        response = patch_detail(
            self.po.pk, self.user,
            {"tracking_number": "1Z999", "status_note": "Vendor confirmed"},
        )
        self.assertEqual(response.status_code, 200)
        self.po.refresh_from_db()
        self.assertEqual(self.po.tracking_number, "1Z999")
        self.assertEqual(self.po.status_note, "Vendor confirmed")


class PurchaseOrderRecalculateEndpointTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.supplier = Supplier.objects.create(
            company_name="Acme Corp", supplier_code="ACME-SE2", company=self.company,
        )
        self.user = company_user(self.company, username="se-por", uid="uid-se-por")
        self.po = PurchaseOrder.objects.create(
            company=self.company, supplier=self.supplier,
            status=PurchaseOrder.Status.DRAFT,
        )
        product = Product.objects.create(
            title="Widget", part_number="W-SE", slug="w-se"
        )
        variant = ProductVariant.objects.create(product=product, sku="W-SE-A")
        POLineItem.objects.create(
            purchase_order=self.po, variant=variant,
            item_name="Widget", our_sku="W-SE-A",
            quantity_ordered=Decimal("10"), unit_cost=Decimal("3.50"),
        )

    def test_totals_patch_rejected_and_recalculate_computes(self):
        # Line-item signals have already recalculated the PO at creation;
        # a totals PATCH cannot overwrite that.
        self.assertEqual(self.po.grand_total, Decimal("35.00"))
        response = patch_detail(self.po.pk, self.user, {"grand_total": "9999.99"})
        self.assertEqual(response.status_code, 400)
        self.po.refresh_from_db()
        self.assertEqual(self.po.grand_total, Decimal("35.00"))

        response = post_action("recalculate", self.po.pk, self.user)
        self.assertEqual(response.status_code, 200)
        self.po.refresh_from_db()
        self.assertEqual(self.po.subtotal, Decimal("35.00"))
        self.assertEqual(self.po.grand_total, Decimal("35.00"))
        self.assertEqual(self.po.balance_due, Decimal("35.00"))
