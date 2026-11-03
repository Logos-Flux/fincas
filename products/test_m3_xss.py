"""Milestone 3 Track G — S4: XSS (products surface) + anonymous-write guard.

Payload fixture set shared across surfaces. Asserts escaping on:
- stored product titles in /erp/ list views (autoescape)
- stored product descriptions on /erp/ detail (must not render raw HTML)
- reflected search queries echoed into list templates
- DRF JSON search responses (JSON content-type, never HTML)

Also pins: anonymous POST to the catalog must not create records
(ProductViewSet reads are anonymous by §4 design; writes are not).
"""

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from products.models import Product

XSS_PAYLOADS = [
    "<script>alert('stored')</script>",
    '"><img src=x onerror=alert(1)>',
    "<svg onload=alert(1)>",
    "';alert(String.fromCharCode(88));explain//",
]


class AnonymousCatalogWriteTests(TestCase):
    def test_anonymous_post_does_not_create_product(self):
        response = APIClient().post(
            "/api/v1/products/products/",
            {"title": "Anon Plant", "slug": "anon-plant"},
            format="json",
        )
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(Product.objects.filter(slug="anon-plant").exists())

    def test_anonymous_patch_does_not_update_product(self):
        product = Product.objects.create(title="Real", slug="real-1")
        response = APIClient().patch(
            f"/api/v1/products/products/{product.pk}/",
            {"title": "Anon Edit"},
            format="json",
        )
        self.assertIn(response.status_code, (401, 403))
        product.refresh_from_db()
        self.assertEqual(product.title, "Real")

    def test_anonymous_delete_rejected(self):
        product = Product.objects.create(title="Gone?", slug="gone-1")
        response = APIClient().delete(
            f"/api/v1/products/products/{product.pk}/"
        )
        self.assertIn(response.status_code, (401, 403))
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())

    def test_anonymous_read_still_allowed_per_pinned_decision(self):
        # §4 pinned: catalog list/detail stay anonymous-readable.
        Product.objects.create(
            title="Visible", slug="vis-1", visible_on_website=True,
            is_active=True,
        )
        response = APIClient().get("/api/v1/products/products/")
        self.assertEqual(response.status_code, 200)


class StoredXssProductTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="xss-staff", password="pw", is_staff=True
        )
        self.client.force_login(self.staff)

    def test_product_title_escaped_in_erp_list(self):
        for payload in XSS_PAYLOADS:
            with self.subTest(payload=payload):
                Product.objects.create(
                    title=payload, slug=f"xss-{abs(hash(payload)) % 999999}",
                )
        response = self.client.get("/erp/products/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn("<script>alert('stored')</script>", content)
        self.assertIn("&lt;script&gt;", content)

    def test_product_description_script_not_executed(self):
        product = Product.objects.create(
            title="Desc test",
            slug="desc-test",
            description="<script>alert('stored')</script>",
        )
        response = self.client.get(f"/erp/products/{product.pk}/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn("<script>alert('stored')</script>", content)

    def test_reflected_search_query_escaped(self):
        response = self.client.get(
            "/erp/products/", {"q": "<script>alert('reflected')</script>"}
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn("<script>alert('reflected')</script>", content)


class ApiSearchContentTypeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="xss-api", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        Product.objects.create(title="Plain Widget", slug="plain-widget")

    def test_search_response_is_json_never_html(self):
        response = self.client.get(
            "/api/v1/products/products/",
            {"search": "<script>alert(1)</script>"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json", response["Content-Type"])
        self.assertNotIn(b"<script>", response.content)

    def test_error_shape_is_json_not_debug_page(self):
        response = self.client.get("/api/v1/products/products/00000000-0000-0000-0000-000000000000/")
        self.assertEqual(response.status_code, 404)
        self.assertIn("application/json", response["Content-Type"])
        self.assertNotIn(b"Traceback", response.content)
