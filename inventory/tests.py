"""Company-scoping contract for inventory viewsets (Step 2, F-027).

Warehouse owns the company; stock inherits through ``warehouse__company``
(no second FK to drift). Company-less rows fail closed (invisible), never
shared. Cross-company writes 400; cross-company PK guesses 404.

This file rewrites the M1 Track A gap characterization: the leak it pinned
is now closed, so these tests assert isolation.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from inventory.models import Warehouse, WarehouseStock
from inventory.views import WarehouseStockViewSet, WarehouseViewSet
from products.models import Product, ProductVariant


def company_user(company, *, username="staff", uid="uid-staff"):
    user = make_staff_user(username=username, password="pw")
    UserProfile.objects.create(user=user, company=company)
    return user


class InventoryIsolationTests(TestCase):
    """Step 2: tenant boundary on inventory reads and writes."""

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.other = Company.objects.create(name="Rival, Inc")
        self.warehouse = Warehouse.objects.create(
            warehouse_code="WH-A", name="A Warehouse",
            company=self.company,
        )
        self.other_warehouse = Warehouse.objects.create(
            warehouse_code="WH-B", name="B Warehouse",
            company=self.other,
        )
        self.orphan = Warehouse.objects.create(
            warehouse_code="WH-X", name="Orphan Warehouse",
        )
        product = Product.objects.create(
            title="Panel", part_number="P-20", slug="p-20", company=self.company,
        )
        variant = ProductVariant.objects.create(product=product, sku="P-20-A")
        WarehouseStock.objects.create(
            warehouse=self.warehouse, variant=variant, quantity=Decimal("42"),
        )
        other_product = Product.objects.create(
            title="Rival Panel", part_number="P-21", slug="p-21",
            company=self.other,
        )
        other_variant = ProductVariant.objects.create(
            product=other_product, sku="P-21-A"
        )
        WarehouseStock.objects.create(
            warehouse=self.other_warehouse, variant=other_variant,
            quantity=Decimal("7"),
        )
        self.factory = APIRequestFactory()

    def _list(self, view, path, user):
        request = self.factory.get(path)
        force_authenticate(request, user=user)
        return view(request)

    def test_warehouse_list_scoped_to_own_company(self):
        view = WarehouseViewSet.as_view({"get": "list"})
        other_user = company_user(self.other, username="rival", uid="uid-rival")
        response = self._list(view, "/api/v1/inventory/warehouses/", other_user)
        self.assertEqual(response.status_code, 200)
        codes = [w["warehouse_code"] for w in response.data["results"]]
        self.assertEqual(codes, ["WH-B"])

    def test_stock_list_scoped_to_own_company(self):
        view = WarehouseStockViewSet.as_view({"get": "list"})
        other_user = company_user(self.other, username="rival2", uid="uid-rival2")
        response = self._list(view, "/api/v1/inventory/stock/", other_user)
        self.assertEqual(response.status_code, 200)
        quantities = [s["quantity"] for s in response.data["results"]]
        self.assertEqual(quantities, ["7.000"])

    def test_warehouse_pk_not_guessable_cross_company(self):
        view = WarehouseViewSet.as_view({"get": "retrieve"})
        other_user = company_user(self.other, username="rival3", uid="uid-rival3")
        request = self.factory.get(
            f"/api/v1/inventory/warehouses/{self.warehouse.pk}/"
        )
        force_authenticate(request, user=other_user)
        response = view(request, pk=self.warehouse.pk)
        self.assertEqual(response.status_code, 404)

    def test_companyless_warehouse_invisible_not_shared(self):
        view = WarehouseViewSet.as_view({"get": "list"})
        own_user = company_user(self.company, username="own", uid="uid-own")
        response = self._list(view, "/api/v1/inventory/warehouses/", own_user)
        codes = [w["warehouse_code"] for w in response.data["results"]]
        self.assertNotIn("WH-X", codes)

    def test_stock_create_in_foreign_warehouse_rejected(self):
        client = APIClient()
        client.force_authenticate(
            user=company_user(self.company, username="w", uid="uid-w")
        )
        variant = ProductVariant.objects.get(sku="P-20-A")
        response = client.post(
            "/api/v1/inventory/stock/",
            {"variant": str(variant.pk),
             "warehouse": str(self.other_warehouse.pk), "quantity": "1.000"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_warehouse_requires_company(self):
        warehouse = Warehouse(warehouse_code="WH-N", name="No Co")
        with self.assertRaises(ValidationError):
            warehouse.full_clean()


class VariantStockFollowsWarehousesTests(TestCase):
    """F-092: receiving and adjustments write WarehouseStock only; the
    variant's ``stock`` (storefront, product editor, API) must follow."""

    def setUp(self):
        company = Company.objects.create(name="Stock Co")
        self.a = Warehouse.objects.create(warehouse_code="SA", name="A", company=company)
        self.b = Warehouse.objects.create(warehouse_code="SB", name="B", company=company)
        product = Product.objects.create(title="Bracket", company=company)
        self.variant = ProductVariant.objects.create(product=product, sku="BRK-1", stock=18)

    def stock(self):
        self.variant.refresh_from_db(fields=["stock"])
        return self.variant.stock

    def test_variant_without_warehouse_rows_keeps_its_stock(self):
        self.assertEqual(self.stock(), 18)

    def test_on_hand_across_active_warehouses(self):
        row = WarehouseStock.objects.create(variant=self.variant, warehouse=self.a, quantity=Decimal("18"))
        WarehouseStock.objects.create(variant=self.variant, warehouse=self.b, quantity=Decimal("4"))
        self.assertEqual(self.stock(), 22)
        row.quantity = Decimal("33")  # a receipt of 15
        row.save(update_fields=["quantity"])
        self.assertEqual(self.stock(), 37)
        self.b.is_active = False
        self.b.save()
        row.quantity = Decimal("30")
        row.save()
        self.assertEqual(self.stock(), 30)
        row.delete()
        self.assertEqual(self.stock(), 0)
