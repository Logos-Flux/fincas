"""Regression tests for Brand/Category/Product auto-slug behavior.

These guard the end-to-end "POST with no slug" path: the ModelSerializer
must treat slug as optional (because model has blank=True) AND the
model's save() override must auto-derive a slug from title. If either
layer regresses, creating records via the DRF API or the admin form
will 400 on a "slug: required" error.

Also includes data-integrity tests for the import_products and
update_prices management commands (PR #93).
"""

from __future__ import annotations

import csv
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user

from .models import (
    Brand,
    Product,
    ProductOption,
    ProductOptionValue,
    ProductVariant,
    SupplierProduct,
    VariantOptionValue,
)
from .serializers import BrandSerializer, CategorySerializer
from .views import (
    ProductOptionValueViewSet,
    ProductOptionViewSet,
    SupplierProductViewSet,
    VariantOptionValueViewSet,
)


class BrandSlugAutoDeriveTests(TestCase):
    def test_serializer_accepts_payload_without_slug(self):
        serializer = BrandSerializer(data={"title": "PV PI"})
        self.assertTrue(
            serializer.is_valid(),
            f"expected slug to be optional, errors: {serializer.errors}",
        )
        brand = serializer.save()
        self.assertEqual(brand.slug, "pv-pi")

    def test_serializer_respects_explicit_slug(self):
        serializer = BrandSerializer(data={"title": "PV PI", "slug": "pvpi"})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        brand = serializer.save()
        self.assertEqual(brand.slug, "pvpi")


class CategorySlugAutoDeriveTests(TestCase):
    def test_serializer_accepts_payload_without_slug(self):
        serializer = CategorySerializer(data={"title": "Solar Inverters"})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        category = serializer.save()
        self.assertEqual(category.slug, "solar-inverters")


class ProductSlugAutoDeriveTests(TestCase):
    """Uses the ORM directly — ProductSerializer has many nested-only
    fields that complicate a serializer-level create test; the important
    guarantee here is that Product.save() fills slug when missing."""

    def test_save_derives_slug_from_title_when_blank(self):
        brand = Brand.objects.create(title="Acme", slug="acme")
        product = Product(title="Acme 15K Inverter", part_number="A15K", brand=brand)
        product.save()
        self.assertEqual(product.slug, "acme-15k-inverter")

    def test_save_respects_explicit_slug(self):
        brand = Brand.objects.create(title="Acme", slug="acme")
        product = Product(
            title="Acme 15K",
            part_number="A15K",
            brand=brand,
            slug="custom-slug",
        )
        product.save()
        self.assertEqual(product.slug, "custom-slug")

    def test_save_collision_appends_suffix(self):
        brand = Brand.objects.create(title="Acme", slug="acme")
        Product.objects.create(title="Panel", part_number="P1", brand=brand)
        second = Product(title="Panel", part_number="P2", brand=brand)
        second.save()
        self.assertEqual(second.slug, "panel-1")


# ---------------------------------------------------------------------------
# Management command tests: import_products + update_prices  (PR #93)
# ---------------------------------------------------------------------------


def _write_csv(tmpdir: Path, filename: str, rows: list[dict]) -> Path:
    """Write a list of dict rows as a CSV file, return the path."""
    path = tmpdir / filename
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    return path


class ImportProductsAtomicTests(TestCase):
    """import_products: per-row atomicity — a failure inside the row must
    roll back Product, ProductCategory, ProductVariant, and SupplierProduct
    so no orphan records are left."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def _csv(self, rows):
        return _write_csv(self.tmpdir, "test.csv", rows)

    def test_happy_path_creates_all_related(self):
        """Valid row creates Product + Variant + (optional) SupplierProduct."""
        csv_path = self._csv(
            [
                {
                    "sku": "TEST-001",
                    "name": "Test Panel 400W",
                    "description": "A test panel",
                    "retail_price": "199.99",
                    "cost_price": "120.00",
                    "weight_lbs": "45",
                    "length_in": "65",
                    "width_in": "39",
                    "height_in": "1.5",
                    "wattage": "400",
                }
            ]
        )
        call_command("import_products", f"--file={csv_path}")
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(ProductVariant.objects.count(), 1)
        variant = ProductVariant.objects.first()
        self.assertEqual(variant.sku, "TEST-001")
        self.assertEqual(variant.retail_price, Decimal("199.99"))

    def test_atomic_rollback_on_variant_create_failure(self):
        """If ProductVariant.objects.create() raises inside the atomic block,
        the Product (and anything else in the row) must be rolled back."""
        csv_path = self._csv(
            [
                {
                    "sku": "ATOMIC-001",
                    "name": "Atomic Test Panel",
                    "description": "",
                    "retail_price": "99.00",
                    "cost_price": "",
                    "weight_lbs": "",
                    "length_in": "",
                    "width_in": "",
                    "height_in": "",
                    "wattage": "",
                }
            ]
        )

        with patch(
            "products.models.ProductVariant.objects.create",
            side_effect=IntegrityError("forced variant failure"),
        ):
            call_command("import_products", f"--file={csv_path}")

        # The atomic block should have rolled back the Product.
        self.assertEqual(Product.objects.count(), 0,
                         "Orphan Product found — atomic rollback failed")
        self.assertEqual(ProductVariant.objects.count(), 0)

    def test_atomic_rollback_on_supplierproduct_failure(self):
        """If SupplierProduct.objects.create() raises (e.g. constraint
        violation), the entire row rolls back."""
        csv_path = self._csv(
            [
                {
                    "sku": "ATOMIC-SP-001",
                    "name": "Atomic SP Panel",
                    "description": "",
                    "retail_price": "149.00",
                    "cost_price": "80.00",
                    "weight_lbs": "",
                    "length_in": "",
                    "width_in": "",
                    "height_in": "",
                    "wattage": "",
                }
            ]
        )

        # Need a supplier so the SupplierProduct path is taken.
        from purchasing.models import Supplier

        Supplier.objects.create(
            supplier_code="ATOMICSUP",
            company_name="Atomic Supplier Co",
        )

        with patch(
            "products.models.SupplierProduct.objects.create",
            side_effect=IntegrityError("forced SP failure"),
        ):
            call_command(
                "import_products",
                f"--file={csv_path}",
                "--supplier=ATOMICSUP",
            )

        self.assertEqual(Product.objects.count(), 0,
                         "Orphan Product found — SupplierProduct failure must roll back row")
        self.assertEqual(ProductVariant.objects.count(), 0)
        self.assertEqual(SupplierProduct.objects.count(), 0)

    def test_error_counting_per_row(self):
        """A failing row does not abort the whole import; subsequent rows
        still process.  Errors are counted."""
        csv_path = self._csv(
            [
                {
                    "sku": "ROW-OK",
                    "name": "Good Row",
                    "description": "",
                    "retail_price": "10.00",
                    "cost_price": "",
                    "weight_lbs": "",
                    "length_in": "",
                    "width_in": "",
                    "height_in": "",
                    "wattage": "",
                },
                {
                    "sku": "ROW-BAD",
                    "name": "Bad Row",
                    "description": "",
                    "retail_price": "10.00",
                    "cost_price": "",
                    "weight_lbs": "",
                    "length_in": "",
                    "width_in": "",
                    "height_in": "",
                    "wattage": "",
                },
            ]
        )

        # Force the second row to fail inside its atomic block.
        original_create = ProductVariant.objects.create

        call_count = 0

        def fail_second(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise IntegrityError("forced second-row failure")
            return original_create(*args, **kwargs)

        with patch(
            "products.models.ProductVariant.objects.create",
            side_effect=fail_second,
        ):
            call_command("import_products", f"--file={csv_path}")

        # First row should be intact.
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(ProductVariant.objects.filter(sku="ROW-OK").count(), 1)
        # Second row must NOT exist.
        self.assertFalse(
            ProductVariant.objects.filter(sku="ROW-BAD").exists(),
            "Failing row should have been rolled back",
        )

    def test_dry_run_makes_no_changes(self):
        csv_path = self._csv(
            [
                {
                    "sku": "DRY-001",
                    "name": "Dry Run Panel",
                    "description": "",
                    "retail_price": "299.00",
                    "cost_price": "",
                    "weight_lbs": "",
                    "length_in": "",
                    "width_in": "",
                    "height_in": "",
                    "wattage": "",
                }
            ]
        )
        call_command("import_products", f"--file={csv_path}", "--dry-run")
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(ProductVariant.objects.count(), 0)


class UpdatePricesDecoupledTests(TestCase):
    """update_prices: variant price changes survive cost-update failures
    (invalid values, missing supplier products) — no silent drops."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        # Create a product + variant to update.
        self.brand = Brand.objects.create(title="Test Brand", slug="test-brand")
        self.product = Product.objects.create(
            title="Decouple Panel",
            slug="decouple-panel",
            part_number="DEC-001",
            brand=self.brand,
        )
        self.variant = ProductVariant.objects.create(
            product=self.product,
            sku="DEC-001",
            name="Decouple Panel",
            retail_price=Decimal("100.00"),
            sale_price=Decimal("90.00"),
        )
        # Supplier + SupplierProduct for cost-update tests.
        from purchasing.models import Supplier

        self.supplier = Supplier.objects.create(
            supplier_code="DECSUP",
            company_name="Decouple Supplier",
        )
        self.sp = SupplierProduct.objects.create(
            variant=self.variant,
            supplier=self.supplier,
            supplier_sku="DEC-001",
            cost=Decimal("60.00"),
            is_primary=True,
        )

    def _csv(self, rows):
        return _write_csv(self.tmpdir, "prices.csv", rows)

    def test_variant_price_saved_despite_invalid_cost(self):
        """When --update-cost is used and cost_price is garbage, the valid
        retail_price change on the same row MUST still be persisted."""
        csv_path = self._csv(
            [
                {
                    "sku": "DEC-001",
                    "retail_price": "149.99",
                    "sale_price": "",
                    "map_price": "",
                    "cost_price": "NOT-A-NUMBER",
                }
            ]
        )
        call_command("update_prices", f"--file={csv_path}", "--update-cost")

        self.variant.refresh_from_db()
        # Retail price MUST be updated even though cost was invalid.
        self.assertEqual(
            self.variant.retail_price, Decimal("149.99"),
            "Valid price change was silently dropped due to cost failure",
        )
        # Cost must remain unchanged.
        self.sp.refresh_from_db()
        self.assertEqual(self.sp.cost, Decimal("60.00"),
                         "Cost should not have changed on invalid input")

    def test_variant_price_saved_when_supplier_product_missing(self):
        """When cost update can't find a SupplierProduct, variant price
        changes still persist."""
        # Delete the SupplierProduct so cost lookup finds nothing.
        self.sp.delete()

        csv_path = self._csv(
            [
                {
                    "sku": "DEC-001",
                    "retail_price": "175.00",
                    "sale_price": "",
                    "map_price": "",
                    "cost_price": "70.00",
                }
            ]
        )
        call_command("update_prices", f"--file={csv_path}", "--update-cost")

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.retail_price, Decimal("175.00"))

    def test_happy_path_updates_both_variant_and_cost(self):
        """Normal case: valid variant price + valid cost, both updated."""
        csv_path = self._csv(
            [
                {
                    "sku": "DEC-001",
                    "retail_price": "199.99",
                    "sale_price": "179.99",
                    "map_price": "",
                    "cost_price": "72.50",
                }
            ]
        )
        call_command("update_prices", f"--file={csv_path}", "--update-cost")

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.retail_price, Decimal("199.99"))
        self.assertEqual(self.variant.sale_price, Decimal("179.99"))

        self.sp.refresh_from_db()
        self.assertEqual(self.sp.cost, Decimal("72.50"))

    def test_dry_run_makes_no_changes(self):
        csv_path = self._csv(
            [
                {
                    "sku": "DEC-001",
                    "retail_price": "500.00",
                    "sale_price": "",
                    "map_price": "",
                    "cost_price": "",
                }
            ]
        )
        call_command("update_prices", f"--file={csv_path}", "--dry-run")

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.retail_price, Decimal("100.00"),
                         "Dry-run must not mutate variant prices")

    def test_update_cost_only_does_not_touch_variant_save(self):
        """When only cost changes (no variant price fields), cost is saved
        without unnecessarily calling variant.save()."""
        csv_path = self._csv(
            [
                {
                    "sku": "DEC-001",
                    "retail_price": "",
                    "sale_price": "",
                    "map_price": "",
                    "cost_price": "85.00",
                }
            ]
        )
        call_command("update_prices", f"--file={csv_path}", "--update-cost")

        self.sp.refresh_from_db()
        self.assertEqual(self.sp.cost, Decimal("85.00"))
        # Variant prices unchanged.
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.retail_price, Decimal("100.00"))


class ProductProfileRenderTests(TestCase):
    """Regression test ensuring product profile renders without NoReverseMatch."""

    def setUp(self):
        self.staff_user = make_staff_user(
            username="staff", password="secretpassword", is_staff=True
        )
        self.client.force_login(self.staff_user)

    def test_new_product_page_renders_ok(self):
        response = self.client.get(reverse("erp-product-create"))
        self.assertEqual(response.status_code, 200)

    def test_existing_product_page_renders_ok(self):
        brand = Brand.objects.create(title="Test Brand", slug="test-brand")
        product = Product.objects.create(title="Test Solar Inverter", brand=brand)
        response = self.client.get(reverse("erp-product-detail", kwargs={"pk": product.pk}))
        self.assertEqual(response.status_code, 200)

"""Company-scoping contract for catalog junction viewsets.

SupplierProduct links a variant to a supplier — both company-owned — so
the junction must scope (via ``supplier__company``). ProductOption scopes
via ``product__company``, ProductOptionValue via ``option__product__company``,
VariantOptionValue via ``variant__product__company``. Cross-company users
must see none of another company's junctions.
"""


def company_user(company, *, username="staff", uid="uid-staff"):
    user = make_staff_user(username=username, password="pw")
    user.userprofile = UserProfile.objects.create(company=company)
    return user


class CatalogJunctionScopingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.other = Company.objects.create(name="Rival, Inc")
        self.other_user = company_user(self.other, username="rival", uid="uid-rival")
        product = Product.objects.create(
            title="Panel", part_number="P-10", slug="p-10", company=self.company,
        )
        self.variant = ProductVariant.objects.create(product=product, sku="P-10-A")
        from purchasing.models import Supplier

        self.supplier = Supplier.objects.create(
            company_name="Acme", supplier_code="ACME10", company=self.company,
        )
        SupplierProduct.objects.create(
            variant=self.variant, supplier=self.supplier, cost=Decimal("5.00"),
        )
        self.option = ProductOption.objects.create(product=product, name="Voltage")
        self.value = ProductOptionValue.objects.create(option=self.option, value="12V")
        VariantOptionValue.objects.create(variant=self.variant, option_value=self.value)
        self.factory = APIRequestFactory()

    def _list(self, viewset, path, user):
        request = self.factory.get(path)
        force_authenticate(request, user=user)
        return viewset.as_view({"get": "list"})(request)

    def test_supplier_products_scoped(self):
        response = self._list(
            SupplierProductViewSet, "/api/v1/products/supplier-products/", self.other_user,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_product_options_scoped(self):
        response = self._list(
            ProductOptionViewSet, "/api/v1/products/product-options/", self.other_user,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_product_option_values_scoped(self):
        response = self._list(
            ProductOptionValueViewSet,
            "/api/v1/products/product-option-values/",
            self.other_user,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_variant_option_values_scoped(self):
        response = self._list(
            VariantOptionValueViewSet,
            "/api/v1/products/variant-option-values/",
            self.other_user,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_same_company_user_sees_junctions(self):
        me = company_user(self.company, username="mine", uid="uid-mine")
        response = self._list(
            SupplierProductViewSet, "/api/v1/products/supplier-products/", me,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)


class SupplierCostViewTests(TestCase):
    """The supplier-cost AJAX endpoints answer bad input with 400/404, not 500.

    The access matrix found ``KeyError`` (a body without ``variant_id``) and
    ``DoesNotExist`` (an unknown pk) escaping as 500s from
    ``SupplierCostSaveView``, ``SupplierCostDetailView``,
    ``SupplierCostDeleteView`` and ``ProductProfileView``.
    """

    MISSING = "00000000-0000-0000-0000-000000000000"

    def setUp(self):
        from purchasing.models import Supplier

        self.staff_user = make_staff_user(username="staff", password="pw")
        self.client.force_login(self.staff_user)
        self.product = Product.objects.create(title="Inverter")
        self.variant = ProductVariant.objects.create(
            product=self.product, sku="INV-1", retail_price=Decimal("100.00"),
        )
        self.supplier = Supplier.objects.create(
            supplier_code="SUP1", company_name="Parts Co", supplier_type="distributor",
        )
        self.sp = SupplierProduct.objects.create(
            variant=self.variant, supplier=self.supplier, cost=Decimal("40.00"),
        )

    def _post(self, url, body):
        return self.client.post(url, body, content_type="application/json")

    def test_create_without_variant_id_is_400(self):
        response = self._post(reverse("erp-supplier-cost-create"), {
            "supplier_id": str(self.supplier.pk), "cost": "41.00",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("variant_id", response.json()["error"])
        self.assertEqual(SupplierProduct.objects.count(), 1)

    def test_update_without_variant_id_is_400(self):
        response = self._post(reverse("erp-supplier-cost-update", args=[self.sp.pk]), {
            "supplier_id": str(self.supplier.pk), "cost": "41.00",
        })
        self.assertEqual(response.status_code, 400)
        self.sp.refresh_from_db()
        self.assertEqual(self.sp.cost, Decimal("40.00"))

    def test_create_with_malformed_json_is_400(self):
        response = self.client.post(
            reverse("erp-supplier-cost-create"), "{not json", content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_create_with_unknown_variant_is_400(self):
        response = self._post(reverse("erp-supplier-cost-create"), {
            "variant_id": self.MISSING, "supplier_id": str(self.supplier.pk), "cost": "1",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SupplierProduct.objects.count(), 1)

    def test_create_with_a_valid_body_still_works(self):
        from purchasing.models import Supplier

        other = Supplier.objects.create(
            supplier_code="SUP2", company_name="Other Co", supplier_type="distributor",
        )
        response = self._post(reverse("erp-supplier-cost-create"), {
            "variant_id": str(self.variant.pk), "supplier_id": str(other.pk), "cost": "39.50",
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(SupplierProduct.objects.count(), 2)

    def test_unknown_supplier_cost_is_404(self):
        for name in ("erp-supplier-cost-detail",):
            self.assertEqual(self.client.get(reverse(name, args=[self.MISSING])).status_code, 404)
        for name in ("erp-supplier-cost-update", "erp-supplier-cost-delete"):
            response = self._post(reverse(name, args=[self.MISSING]), {
                "variant_id": str(self.variant.pk), "supplier_id": str(self.supplier.pk), "cost": "1",
            })
            self.assertEqual(response.status_code, 404, name)

    def test_unknown_product_is_404(self):
        self.assertEqual(
            self.client.get(reverse("erp-product-detail", kwargs={"pk": self.MISSING})).status_code, 404,
        )
        self.assertEqual(
            self.client.post(reverse("erp-product-detail", kwargs={"pk": self.MISSING}), {}).status_code, 404,
        )
