"""SOL-16 company-scoping contract for purchasing line-item viewsets.

Mirrors orders/tests/test_company_scoping.py: POLineItemViewSet scopes via
``purchase_order__company`` and must never raise AttributeError -> 500 on
unauthenticated requests (AnonymousUser / the superuser service account
both lack ``userprofile``). Fail closed instead.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from core.models import Company
from core.tests.helpers import make_staff_user
from products.models import Product, ProductVariant
from purchasing.models import POLineItem, PurchaseOrder, Supplier
from purchasing.views import POLineItemViewSet

from .test_recalculate import company_user


class POLineItemScopingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.supplier = Supplier.objects.create(
            company_name="Acme", supplier_code="ACME", company=self.company,
        )
        product = Product.objects.create(title="Panel", part_number="P-1", slug="p-1")
        self.variant = ProductVariant.objects.create(product=product, sku="P-1-A")
        self.po = PurchaseOrder.objects.create(company=self.company, supplier=self.supplier)
        POLineItem.objects.create(
            purchase_order=self.po, variant=self.variant, item_name="Panel",
            our_sku="P-1-A", quantity_ordered=Decimal("1"), unit_cost=Decimal("5.00"),
            line_total=Decimal("5.00"), tax_amount=Decimal("0"),
        )
        self.factory = APIRequestFactory()
        self.list_view = POLineItemViewSet.as_view({"get": "list"})

    def _list(self, user):
        request = self.factory.get("/api/v1/purchasing/po-line-items/")
        force_authenticate(request, user=user)
        return self.list_view(request)

    def test_anonymous_is_fail_closed_not_500(self):
        # Prod permission semantics (explicit IsAuthenticated, F-003):
        # anonymous is rejected at the auth layer — never 500, never data.
        response = self._list(user=None)
        self.assertIn(response.status_code, (401, 403))

    def test_same_company_user_sees_own_lines(self):
        response = self._list(user=company_user(self.company))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)

    def test_other_company_user_sees_nothing(self):
        other = Company.objects.create(name="Rival, Inc")
        response = self._list(
            user=company_user(other, username="rival", uid="uid-rival")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)


class PurchaseOrderCrudMatrixTests(TestCase):
    """Full tenancy matrix on PurchaseOrderViewSet + POReceipt scoping."""

    def setUp(self):
        self.company_a = Company.objects.create(name="Example Supply, Inc")
        self.company_b = Company.objects.create(name="Rival, Inc")
        self.supplier_a = Supplier.objects.create(
            company_name="Acme", supplier_code="ACME-A",
            company=self.company_a,
        )
        self.supplier_b = Supplier.objects.create(
            company_name="RivalSup", supplier_code="RSUP-B",
            company=self.company_b,
        )
        self.po_b = PurchaseOrder.objects.create(
            company=self.company_b, supplier=self.supplier_b,
            po_number="PO-B-00001",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=company_user(self.company_a))

    def test_list_excludes_other_company(self):
        response = self.client.get("/api/v1/purchasing/purchase-orders/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_company_param_tamper_returns_nothing(self):
        response = self.client.get(
            f"/api/v1/purchasing/purchase-orders/?company={self.company_b.pk}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_retrieve_update_delete_other_company_404(self):
        base = f"/api/v1/purchasing/purchase-orders/{self.po_b.pk}/"
        self.assertEqual(self.client.get(base).status_code, 404)
        self.assertEqual(
            self.client.patch(base, {"tracking_number": "hijack"},
                              format="json").status_code, 404,
        )
        self.assertEqual(self.client.delete(base).status_code, 404)
        self.assertTrue(
            PurchaseOrder.objects.filter(pk=self.po_b.pk).exists()
        )

    def test_create_under_other_company_is_400(self):
        response = self.client.post(
            "/api/v1/purchasing/purchase-orders/",
            {"supplier": str(self.supplier_b.pk),
             "company": str(self.company_b.pk)},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_create_defaults_to_own_company(self):
        # NOTE: po_number is serializer-required (pre-existing wart —
        # save() would auto-number, but DRF validation runs first), so
        # the test supplies it; company defaulting is what is under test.
        response = self.client.post(
            "/api/v1/purchasing/purchase-orders/",
            {"supplier": str(self.supplier_a.pk), "po_number": "PO-A-00001"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        po = PurchaseOrder.objects.get(pk=response.data["id"])
        self.assertEqual(po.company, self.company_a)

    def test_line_create_under_other_company_po_is_400(self):
        from products.models import Product, ProductVariant

        product = Product.objects.create(
            title="Panel", part_number="P-30", slug="p-30")
        variant = ProductVariant.objects.create(product=product, sku="P-30-A")
        response = self.client.post(
            "/api/v1/purchasing/po-lines/",
            {
                "purchase_order": str(self.po_b.pk),
                "variant": str(variant.pk),
                "item_name": "Panel",
                "quantity_ordered": "1",
                "unit_cost": "5.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(POLineItem.objects.count(), 0)

    def test_gap_erp_po_edit_guessable_cross_company(self):
        """KNOWN GAP (M1 Track A): /erp/ update views take any PK."""
        staff = make_staff_user("erpstaff3", password="pw", is_staff=True)
        self.client.force_login(staff)
        from django.urls import reverse

        response = self.client.get(
            reverse("erp-purchaseorder-edit", args=[self.po_b.pk])
        )
        self.assertEqual(response.status_code, 200)
