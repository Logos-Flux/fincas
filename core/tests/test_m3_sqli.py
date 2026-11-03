"""Milestone 3 Track G — S5: SQL injection via filters/search/ordering params.

Probes the DRF filter/search/ordering surface with a classic `' OR 1=1`
fixture set. ORM-parameterised backends must return normal responses
(200, possibly unfiltered per the §4 pinned decision) — never 500, and
never broaden the result set beyond the unfiltered baseline.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company
from core.tests.helpers import make_staff_user
from customers.models import Customer
from orders.models import Order
from products.models import Product

SQLI_PAYLOADS = [
    "' OR '1'='1",
    "' OR 1=1--",
    "'; DROP TABLE products_product;--",
    '" OR ""="',
    "1' UNION SELECT 1,2,3--",
]


class SqliFilterTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Sqli Co")
        self.user = make_staff_user(username="sqli", password="pw")
        self.customer = Customer.objects.create(
            first_name="A",
            last_name="B",
            email="sqli@example.com",
            company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer, company=self.company, order_number="SQ-1",
        )
        self.product = Product.objects.create(
            title="Plain Widget", slug="plain-widget", company=self.company,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _baseline_ids(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return {row["id"] for row in response.data["results"]}

    def test_search_sqli_never_errors_nor_widens_products(self):
        url = "/api/v1/products/products/"
        baseline = self._baseline_ids(url)
        for payload in SQLI_PAYLOADS:
            with self.subTest(payload=payload):
                response = self.client.get(url, {"search": payload})
                self.assertEqual(response.status_code, 200)
                ids = {row["id"] for row in response.data["results"]}
                self.assertLessEqual(ids, baseline)

    def test_search_sqli_never_errors_nor_widens_orders(self):
        url = "/api/v1/orders/orders/"
        baseline = self._baseline_ids(url)
        for payload in SQLI_PAYLOADS:
            with self.subTest(payload=payload):
                response = self.client.get(url, {"search": payload})
                self.assertEqual(response.status_code, 200)
                ids = {row["id"] for row in response.data["results"]}
                self.assertLessEqual(ids, baseline)

    def test_ordering_sqli_is_rejected_or_ignored(self):
        url = "/api/v1/products/products/"
        for payload in SQLI_PAYLOADS + ["-title;DROP", "(case when 1=1)"]:
            with self.subTest(payload=payload):
                response = self.client.get(url, {"ordering": payload})
                # DRF OrderingFilter: unknown/invalid ordering → 200
                # (silently ignored) or 400; never 500.
                self.assertIn(response.status_code, (200, 400))

    def test_filter_sqli_on_exact_field(self):
        url = "/api/v1/orders/orders/"
        baseline = self._baseline_ids(url)
        for payload in SQLI_PAYLOADS:
            with self.subTest(payload=payload):
                response = self.client.get(url, {"status": payload})
                # Choice-field validation rejects garbage (400) or matches
                # nothing (200 empty); never 500, never widens.
                self.assertIn(response.status_code, (200, 400))
                if response.status_code == 200:
                    ids = {row["id"] for row in response.data["results"]}
                    self.assertLessEqual(ids, baseline)

    def test_unknown_filter_param_ignored_per_pinned_decision(self):
        # §4 pinned: unknown filter params are ignored (200, unfiltered).
        response = self.client.get(
            "/api/v1/products/products/", {"nonexistent_field": "' OR 1=1"}
        )
        self.assertEqual(response.status_code, 200)

    def test_erp_search_view_with_sqli_query(self):
        # /erp/ list views use Django ORM filters — payload must render
        # escaped or empty, never 500.
        staff = make_staff_user(
            username="sqli-staff", password="pw", is_staff=True
        )
        self.client.force_login(staff)
        response = self.client.get("/erp/products/", {"q": "' OR 1=1--"})
        self.assertIn(response.status_code, (200, 404))
