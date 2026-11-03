"""Milestone 2 Track D — render sweep over every /erp/ staff template.

Covers testing-plan §5 "Render sweep": every golden_erp/templates/erp/*.html renders
200 for staff with minimal seed AND with empty querysets (no None crashes,
no empty-table layout breakage).

Two classes share one URL table:
- EmptyDbRenderSweepTests: fresh DB, no seed — catches None-crashes.
- SeededRenderSweepTests: runs seed_test_minimal, then re-hits every list
  view plus detail/edit/print views built on real objects.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

# (url_name, kwargs) for plain staff GET views. POST-only / JSON / AJAX
# endpoints are intentionally excluded (covered by their own suites).
LIST_URLS = [
    "erp-dashboard",
    "erp-account",
    "erp-account-edit",
    "erp-account-change-password",
    "erp-user-list",
    "erp-user-create",
    "erp-order-list",
    "erp-order-create",
    "erp-quote-list",
    "erp-quote-create",
    "erp-product-list",
    "erp-product-create",
    "erp-brand-list",
    "erp-brand-create",
    "erp-category-list",
    "erp-category-create",
    "erp-customer-list",
    "erp-customer-create",
    "erp-purchaseorder-list",
    "erp-purchaseorder-create",
    "erp-supplier-list",
    "erp-supplier-create",
    "erp-pickticket-list",
    "erp-pickticket-create",
    "erp-warehouse-list",
    "erp-warehouse-create",
    "erp-pricelist-list",
    "erp-pricelist-create",
    "erp-invoice-list",
    "erp-document-library",
    "erp-report-list",
    "erp-discount-list",
    "erp-discount-create",
    "erp-discount-generate",
    "erp-article-list",
    "erp-article-create",
    "erp-accounting-dashboard",
    "erp-accounting-trial-balance",
    "erp-accounting-entries",
    "erp-accounting-import",
    "erp-integrations",
]


class SweepMixin:
    def _login_staff(self):
        user = User.objects.create_user(
            username="sweep-staff", password="pw", is_staff=True
        )
        self.client.force_login(user)
        return user

    def assertSweepOk(self, name):
        response = self.client.get(reverse(name))
        self.assertEqual(
            response.status_code, 200, f"{name} returned {response.status_code}"
        )
        # Every staff page must render inside the ERP chrome, not leak a
        # bare exception page.
        content = response.content.decode()
        self.assertNotIn("Traceback", content, name)
        return response


class EmptyDbRenderSweepTests(SweepMixin, TestCase):
    """Every staff list/create view renders 200 on an empty database."""

    def setUp(self):
        self._login_staff()

    def test_empty_db_sweep(self):
        for name in LIST_URLS:
            with self.subTest(view=name):
                self.assertSweepOk(name)

    def test_anonymous_redirects_to_login(self):
        self.client.logout()
        for name in LIST_URLS:
            with self.subTest(view=name):
                response = self.client.get(reverse(name))
                self.assertIn(
                    response.status_code, (301, 302), f"{name} anon"
                )


class EdgeSeedSmokeTests(SweepMixin, TestCase):
    """Key views against seed_test_edge (multi-company, multi-currency,
    empty warehouse, expired coupon, closed fiscal period)."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_test_minimal")
        call_command("seed_test_edge")

    def setUp(self):
        self._login_staff()

    def test_edge_seed_key_views(self):
        for name in (
            "erp-dashboard",
            "erp-order-list",
            "erp-quote-list",
            "erp-report-list",
            "erp-accounting-dashboard",
            "erp-accounting-trial-balance",
            "erp-discount-list",
            "erp-warehouse-list",
        ):
            with self.subTest(view=name):
                self.assertSweepOk(name)


class SeededRenderSweepTests(SweepMixin, TestCase):
    """Same sweep against seed_test_minimal, plus detail/edit/print views."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_test_minimal")

    def setUp(self):
        self._login_staff()

    def test_seeded_list_sweep(self):
        for name in LIST_URLS:
            with self.subTest(view=name):
                self.assertSweepOk(name)

    def _detail_urls(self):
        from core.models import Company
        from customers.models import Customer
        from discounts.models import DiscountCode
        from fulfillment.models import PickTicket
        from inventory.models import Warehouse
        from orders.models import Order, Quote
        from pricing.models.price_book import PriceList
        from products.models import Brand, Category, Product
        from purchasing.models import PurchaseOrder, Supplier

        company = Company.objects.get(name="Test Minimal Corp")
        customer = Customer.objects.get(email="retail-customer@example.com")
        supplier = Supplier.objects.get(supplier_code="TEST-SUP-01")
        warehouse = Warehouse.objects.get(warehouse_code="TEST-WH1")
        price_list = PriceList.objects.get(name="Test Retail")
        brand = Brand.objects.get(title="Test Brand")
        category = Category.objects.get(title="Test Category")
        product = Product.objects.get(slug="test-product-01")

        quote = Quote.objects.create(customer=customer, company=company)
        order = Order.objects.create(customer=customer, company=company)
        po = PurchaseOrder.objects.create(
            supplier=supplier, company=company
        )
        ticket = PickTicket.objects.create(
            warehouse=warehouse, order=order
        )
        discount = DiscountCode.objects.create(
            code="SWEEP10",
            discount_type=DiscountCode.DiscountType.PERCENT,
            value=10,
        )
        return [
            ("erp-quote-detail", {"pk": quote.pk}),
            ("erp-quote-edit", {"pk": quote.pk}),
            ("erp-order-edit", {"pk": order.pk}),
            ("erp-purchaseorder-edit", {"pk": po.pk}),
            ("erp-supplier-edit", {"pk": supplier.pk}),
            ("erp-pickticket-edit", {"pk": ticket.pk}),
            ("erp-pickticket-print", {"pk": ticket.pk}),
            ("erp-customer-detail", {"pk": customer.pk}),
            ("erp-customer-edit", {"pk": customer.pk}),
            ("erp-product-detail", {"pk": product.pk}),
            ("erp-brand-detail", {"pk": brand.pk}),
            ("erp-category-detail", {"pk": category.pk}),
            ("erp-warehouse-edit", {"pk": warehouse.pk}),
            ("erp-pricelist-detail", {"pk": price_list.pk}),
            ("erp-pricelist-edit", {"pk": price_list.pk}),
            ("erp-discount-detail", {"pk": discount.pk}),
            ("erp-discount-edit", {"pk": discount.pk}),
        ]

    def test_seeded_detail_sweep(self):
        for name, kwargs in self._detail_urls():
            with self.subTest(view=name):
                url = reverse(name, kwargs=kwargs)
                response = self.client.get(url)
                self.assertEqual(
                    response.status_code,
                    200,
                    f"{name} returned {response.status_code}",
                )
                self.assertNotIn("Traceback", response.content.decode(), name)

    def test_variant_lookup_ajax_shape(self):
        """AJAX lookup: 200 with valid id, 400 without, 404 on unknown id."""
        from products.models import ProductVariant

        variant = ProductVariant.objects.get(sku="TEST-SKU-01")
        url = reverse("erp-variant-lookup")
        ok = self.client.get(url, {"id": str(variant.pk)})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["sku"], "TEST-SKU-01")
        self.assertEqual(self.client.get(url).status_code, 400)
        self.assertEqual(
            self.client.get(
                url, {"id": "00000000-0000-0000-0000-000000000000"}
            ).status_code,
            404,
        )
