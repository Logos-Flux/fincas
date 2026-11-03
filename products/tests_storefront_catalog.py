"""The storefront catalog contract: GET /api/products/products/.

The headless storefront renders this list directly, so it must carry real
commerce data (price, stock, category, summary) and never require the
storefront to invent it. Values the ERP doesn't have come back null.
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from .models import Category, Product, ProductCategory, ProductVariant

URL = "/api/products/products/"
PUBLIC = URL + "?visible_on_website=true&is_active=true"


def _variant(product, sku, **kw):
    return ProductVariant.objects.create(product=product, sku=sku, **kw)


class StorefrontCatalogApiTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(
            title="Servo", part_number="SV-1", short_description="<p>Quiet  <b>torque</b>.</p>"
        )
        cat = Category.objects.create(title="Motion")
        ProductCategory.objects.create(product=self.product, category=cat)

    def _row(self, title="Servo", url=URL):
        rows = self.client.get(url).json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        return next(r for r in rows if r["title"] == title)

    def test_price_is_lowest_sellable_variant_price_preferring_sale(self):
        _variant(self.product, "A", retail_price=Decimal("400.00"), sale_price=Decimal("350.00"), stock=5)
        _variant(self.product, "B", retail_price=Decimal("380.00"), stock=2)
        _variant(self.product, "C", retail_price=Decimal("10.00"), is_active=False, stock=99)
        row = self._row()
        self.assertEqual(row["price"], "350.00")
        self.assertEqual(row["stock_quantity"], 7)  # inactive variant excluded
        self.assertEqual(row["stock_status"], "in_stock")
        self.assertEqual(row["category_name"], "Motion")
        self.assertEqual(row["summary"], "Quiet torque.")

    def test_unpriced_product_has_null_price_not_a_guess(self):
        _variant(self.product, "A", stock=0, stock_status=ProductVariant.StockStatus.OUT_OF_STOCK)
        row = self._row()
        self.assertIsNone(row["price"])
        self.assertEqual(row["stock_status"], "out_of_stock")
        self.assertIsNone(row["image_url"])  # no CDN base / no image in tests

    def test_product_without_variants_is_out_of_stock(self):
        row = self._row()
        self.assertIsNone(row["price"])
        self.assertEqual(row["stock_quantity"], 0)
        self.assertEqual(row["stock_status"], "out_of_stock")

    def _titles(self, url=URL):
        rows = self.client.get(url).json()
        return {r["title"] for r in (rows["results"] if isinstance(rows, dict) else rows)}

    def test_non_staff_tokens_read_the_public_catalog(self):
        # Shoppers and the storefront-api identity have no company profile;
        # they used to get an empty catalog (company scoping fell to .none()).
        from core.storefront import ensure_storefront_identity

        Product.objects.create(title="Hidden", visible_on_website=False)
        Product.objects.create(title="Retired", is_active=False)
        for user in (User.objects.create_user("shopper", password="x"), ensure_storefront_identity()):
            self.client.force_login(user)
            titles = self._titles()
            self.assertIn("Servo", titles, user.username)
            self.assertNotIn("Hidden", titles, user.username)
            self.assertNotIn("Retired", titles, user.username)

    def test_public_filter_hides_inactive_and_hidden_even_when_authenticated(self):
        Product.objects.create(title="Hidden", visible_on_website=False)
        Product.objects.create(title="Retired", is_active=False)
        self.client.force_login(User.objects.create_user("shopper", password="x"))
        rows = self.client.get(PUBLIC).json()
        titles = {r["title"] for r in (rows["results"] if isinstance(rows, dict) else rows)}
        self.assertIn("Servo", titles)
        self.assertNotIn("Hidden", titles)
        self.assertNotIn("Retired", titles)

    def test_query_count_does_not_grow_with_products(self):
        def count():
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(PUBLIC)
            return len(ctx.captured_queries)

        _variant(self.product, "A", retail_price=Decimal("1.00"))
        baseline = count()
        for i in range(5):
            p = Product.objects.create(title=f"Extra {i}", part_number=f"X{i}")
            _variant(p, f"X{i}", retail_price=Decimal("2.00"))
        self.assertEqual(count(), baseline)

    def test_detail_carries_the_same_storefront_fields(self):
        _variant(self.product, "A", retail_price=Decimal("400.00"), stock=3)
        body = self.client.get(f"{URL}{self.product.pk}/").json()
        self.assertEqual(body["price"], "400.00")
        self.assertEqual(body["stock_quantity"], 3)
        self.assertEqual(body["category_name"], "Motion")
        self.assertEqual(body["variants"][0]["price"], "400.00")

    def test_public_detail_hides_unsellable_variants_and_costs(self):
        _variant(self.product, "VIS", retail_price=Decimal("40.00"), wholesale_price=Decimal("25.00"))
        _variant(self.product, "HID", retail_price=Decimal("5.00"), visible_on_website=False)
        _variant(self.product, "OFF", retail_price=Decimal("6.00"), is_active=False)
        body = self.client.get(f"{URL}{self.product.pk}/").json()
        self.assertEqual([v["sku"] for v in body["variants"]], ["VIS"])
        self.assertNotIn("wholesale_price", body["variants"][0])
        self.assertNotIn("map_price", body["variants"][0])

    def test_staff_detail_keeps_full_variant_rows(self):
        _variant(self.product, "HID", retail_price=Decimal("5.00"), wholesale_price=Decimal("2.00"),
                 visible_on_website=False)
        self.client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
        body = self.client.get(f"{URL}{self.product.pk}/").json()
        self.assertEqual(body["variants"][0]["wholesale_price"], "2.00")

    def test_zero_sale_price_means_no_sale_on_product_and_variant(self):
        _variant(self.product, "Z", retail_price=Decimal("90.00"), sale_price=Decimal("0.00"))
        self.assertEqual(self._row()["price"], "90.00")
        body = self.client.get(f"{URL}{self.product.pk}/").json()
        self.assertEqual(body["variants"][0]["price"], "90.00")

    def test_detail_listing_is_staff_only(self):
        rows = self.client.get(URL + "?detail=true").json()
        row = (rows["results"] if isinstance(rows, dict) else rows)[0]
        self.assertNotIn("variants", row)  # public callers get the plain list

    def test_hidden_product_detail_is_404_for_the_public(self):
        hidden = Product.objects.create(title="Hidden", visible_on_website=False)
        self.assertEqual(self.client.get(f"{URL}{hidden.pk}/").status_code, 404)


class CatalogWriteScopeTests(TestCase):
    """Reads are public for non-staff; writes must stay company-scoped."""

    def setUp(self):
        from core.models import Company, UserProfile

        mine, theirs = Company.objects.create(name="Mine"), Company.objects.create(name="Theirs")
        self.target = Product.objects.create(title="Theirs", slug="theirs", company=theirs)
        # Non-staff account that still carries a company-linked profile.
        self.user = User.objects.create_user("portal", password="pw")
        UserProfile.objects.create(user=self.user, company=mine)

    def _call(self, method, action, **data):
        from rest_framework.test import APIRequestFactory, force_authenticate

        from .views import ProductViewSet

        request = getattr(APIRequestFactory(), method)(f"{URL}{self.target.pk}/", data, format="json")
        force_authenticate(request, user=self.user)
        return ProductViewSet.as_view({method: action})(request, pk=self.target.pk)

    def test_public_product_is_readable(self):
        self.assertEqual(self._call("get", "retrieve").status_code, 200)

    def test_non_staff_user_cannot_write_the_catalog_at_all(self):
        for method, action, data in (("patch", "partial_update", {"title": "Owned"}), ("delete", "destroy", {})):
            self.assertEqual(self._call(method, action, **data).status_code, 403, method)
        self.target.refresh_from_db()
        self.assertEqual(self.target.title, "Theirs")

    def test_company_less_user_cannot_create_a_product_for_another_company(self):
        # Shopper-style account: authenticated, not staff, no company.
        from rest_framework.test import APIRequestFactory, force_authenticate

        from .views import ProductViewSet

        shopper = User.objects.create_user("shopper", password="x")
        request = APIRequestFactory().post(
            URL, {"title": "Planted", "company": str(self.target.company_id)}, format="json"
        )
        force_authenticate(request, user=shopper)
        response = ProductViewSet.as_view({"post": "create"})(request)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Product.objects.filter(title="Planted").exists())

    def test_staff_writes_stay_company_scoped(self):
        from core.models import Company

        from .tests import company_user

        staff = company_user(Company.objects.create(name="Staffco"), username="st", uid="uid-st")
        self.user = staff
        self.assertEqual(self._call("patch", "partial_update", title="Owned").status_code, 404)
