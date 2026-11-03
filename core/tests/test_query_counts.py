"""Query-count pins for the five hottest ERP views (testing-plan §7.6).

Caps fail on N+1 growth, not on absolute perfection. Re-pin explicitly if a
view legitimately gains queries.
"""

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.models import Company
from customers.models import Customer
from orders.models import Order
from products.models import Product


def _count(client, url):
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url)
    assert response.status_code == 200, (url, response.status_code)
    return len(ctx)


class HotViewQueryCountsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("qc", "qc@x.com", "pass", is_staff=True)
        cls.company = Company.objects.create(name="QC Corp")
        cls.customer = Customer.objects.create(
            first_name="Q", last_name="C", email="qc@example.com", company=cls.company
        )
        cls.order = Order.objects.create(
            order_number="SO-QC-1", customer=cls.customer, company=cls.company
        )
        cls.product = Product.objects.create(title="QC Widget", slug="qc-widget")

    def setUp(self):
        self.client.force_login(self.user)

    def test_counts(self):
        urls = {
            "dashboard": reverse("erp-dashboard"),
            "order_list": reverse("erp-order-list"),
            "order_edit": reverse("erp-order-edit", args=[self.order.id]),
            "product_list": reverse("erp-product-list"),
            "product_detail": reverse("erp-product-detail", args=[self.product.id]),
        }
        counts = {name: _count(self.client, url) for name, url in urls.items()}
        print("\nQUERY_COUNTS:", counts)
        # Measured 2026-09-10: dashboard 10, order_list 5, order_edit 18,
        # product_list 5, product_detail 9. Caps allow ~50% headroom.
        caps = {
            "dashboard": 15,
            "order_list": 10,
            "order_edit": 25,
            "product_list": 10,
            "product_detail": 15,
        }
        for name, cap in caps.items():
            self.assertLessEqual(counts[name], cap, f"{name} exceeded query cap")
