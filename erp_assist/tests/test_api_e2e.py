"""End-to-end tests: erp_assist core functions against the real DRF API.

The assistant talks to Django over HTTP with ``Authorization: Api-Key``,
so these tests stand up the actual ASGI app in-process
(``httpx.ASGITransport`` → ``config.asgi.application``) and drive the real
``erp_assist.core`` async functions against it — serializers, viewsets,
transition actions, and the assistant's HTTP transport all in the loop.

TransactionTestCase (not TestCase) is required: Django's ASGI handler runs
sync views on worker threads with separate DB connections, which cannot see
uncommitted per-test data.

Covers the 2026-09-10 migration: status writes route through the guarded
``transition`` action, total writes are rejected server-side, and the
external tool contracts (``update_order_status`` et al.) still work.
"""

from __future__ import annotations

import asyncio
import inspect
from decimal import Decimal

import httpx
from django.contrib.auth.models import User
from django.test import TransactionTestCase, override_settings

from config.asgi import application
from core.models import Company
from customers.models import Customer
from erp_assist import mcp_adapter
from erp_assist.core import (
    ERPClient,
    ERPConfig,
    ERPValidationError,
)
from erp_assist.core import customers as customers_core
from erp_assist.core import orders as orders_core
from erp_assist.core import purchasing as purchasing_core
from orders.models import Order, OrderLineItem, Quote, SubOrder
from purchasing.models import PurchaseOrder, Supplier

API_KEY = "erp-assist-e2e-key"


def run(coro):
    return asyncio.run(coro)


@override_settings(API_KEY=API_KEY)
class AssistApiE2ETests(TransactionTestCase):
    """update_order_status / update_* against the live DRF stack."""

    def setUp(self):
        # APIKeyAuthentication binds to the first superuser — the shared
        # API service account it would auto-create otherwise.
        User.objects.create_superuser(username="api-service-e2e", password=None)
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="assist-e2e@example.com",
            company=self.company,
        )
        real_client = ERPClient(
            ERPConfig(base_url="http://testserver", api_key=API_KEY)
        )
        # Swap the real network transport for an in-process ASGI bridge so
        # the full DRF stack (auth, scoping, serializers, actions) runs.
        client = ERPClient.__new__(ERPClient)
        client._config = real_client._config
        client._http = httpx.AsyncClient(
            transport=httpx.ASGITransport(application),
            base_url="http://testserver",
            follow_redirects=True,
            headers={
                "Authorization": f"Api-Key {API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        client.lookup_cache = real_client.lookup_cache
        self.client = client

    def tearDown(self):
        run(self.client._http.aclose())

    # ---- Orders -----------------------------------------------------------

    def test_update_order_status_legal_move(self):
        order = Order.objects.create(
            customer=self.customer, company=self.company,
            status=Order.Status.PENDING,
        )
        result = run(
            orders_core.update_order_status(
                self.client, str(order.pk),
                status="awaiting_payment", status_note="Invoice sent",
            )
        )
        self.assertEqual(result["status"], "awaiting_payment")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)
        self.assertEqual(order.status_note, "Invoice sent")

    def test_update_order_status_illegal_move_raises(self):
        order = Order.objects.create(
            customer=self.customer, company=self.company,
            status=Order.Status.PENDING,
        )
        with self.assertRaises(ERPValidationError):
            run(
                orders_core.update_order_status(
                    self.client, str(order.pk), status="shipped"
                )
            )
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING)

    def test_update_order_routes_status_then_patches_rest(self):
        order = Order.objects.create(
            customer=self.customer, company=self.company,
            status=Order.Status.PENDING,
        )
        result = run(
            orders_core.update_order(
                self.client,
                str(order.pk),
                {"status": "awaiting_payment", "internal_notes": "AI: follow up"},
            )
        )
        self.assertEqual(result["status"], "awaiting_payment")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)
        self.assertEqual(order.internal_notes, "AI: follow up")

    def test_update_order_rejects_unknown_fields_before_transitioning(self):
        order = Order.objects.create(
            customer=self.customer, company=self.company,
            status=Order.Status.PENDING,
        )
        with self.assertRaises(ERPValidationError) as ctx:
            run(
                orders_core.update_order(
                    self.client, str(order.pk),
                    {"status": "awaiting_payment", "staus": "typo"},
                )
            )
        self.assertIn("staus", str(ctx.exception))
        # Validation failed client-side BEFORE the transition fired.
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING)

    def test_totals_patch_rejected_by_api(self):
        order = Order.objects.create(
            customer=self.customer, company=self.company,
            status=Order.Status.PENDING, grand_total=Decimal("10.00"),
        )
        with self.assertRaises(ERPValidationError):
            run(
                orders_core.update_order(
                    self.client, str(order.pk), {"grand_total": "9999.99"}
                )
            )
        order.refresh_from_db()
        self.assertEqual(order.grand_total, Decimal("10.00"))

    def test_recalculate_order_totals_via_assist(self):
        order = Order.objects.create(
            customer=self.customer, company=self.company,
            status=Order.Status.PENDING,
        )
        OrderLineItem.objects.create(
            order=order, item_name="Widget", sku="W-1",
            quantity=Decimal("4"), unit_price_ex_tax=Decimal("25.00"),
        )
        result = run(orders_core.recalculate_order_totals(self.client, str(order.pk)))
        self.assertEqual(result["grand_total"], "100.00")
        order.refresh_from_db()
        self.assertEqual(order.grand_total, Decimal("100.00"))

    # ---- Quotes -----------------------------------------------------------

    def test_update_quote_status_via_transition(self):
        quote = Quote.objects.create(
            customer=self.customer, company=self.company,
            status=Quote.Status.DRAFT,
        )
        result = run(
            orders_core.update_quote(self.client, str(quote.pk), {"status": "sent"})
        )
        self.assertEqual(result["status"], "sent")
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.SENT)

    # ---- Sub-orders -------------------------------------------------------

    def test_update_sub_order_status_via_transition(self):
        order = Order.objects.create(
            customer=self.customer, company=self.company,
            status=Order.Status.AWAITING_SHIPMENT,
        )
        sub = SubOrder.objects.create(
            sub_order_number="SUB-E2E-1", order=order,
            status=SubOrder.Status.PACKED,
        )
        result = run(
            orders_core.update_sub_order(
                self.client, str(sub.pk), {"status": "shipped"}
            )
        )
        self.assertEqual(result["status"], "shipped")
        sub.refresh_from_db()
        self.assertEqual(sub.status, SubOrder.Status.SHIPPED)

    # ---- Purchase orders --------------------------------------------------

    def test_update_purchase_order_status_via_transition(self):
        supplier = Supplier.objects.create(
            company_name="Acme Corp", supplier_code="ACME-E2E",
            company=self.company,
        )
        po = PurchaseOrder.objects.create(
            company=self.company, supplier=supplier,
            status=PurchaseOrder.Status.DRAFT,
        )
        result = run(
            purchasing_core.update_purchase_order(
                self.client, str(po.pk), {"status": "sent"}
            )
        )
        self.assertEqual(result["status"], "sent")
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.SENT)

    # ---- Customers --------------------------------------------------------

    def test_create_customer_with_only_required_args_returns_201(self):
        # customer_type is left to its default: the default must be a valid
        # Customer.CustomerType, or the API answers 400.
        posted = []

        async def record(response):
            if response.request.method == "POST" and response.request.url.path == customers_core.CUSTOMERS_URL:
                posted.append(response.status_code)

        self.client._http.event_hooks["response"].append(record)
        result = run(
            customers_core.create_customer(
                self.client, first_name="Ada", last_name="Lovelace",
                email="ada-default-type@example.com",
            )
        )
        self.assertEqual(posted, [201])
        self.assertIn("id", result)
        customer = Customer.objects.get(email="ada-default-type@example.com")
        self.assertEqual(customer.customer_type, Customer.CustomerType.INDIVIDUAL)

    def test_create_customer_default_customer_type_is_a_model_choice(self):
        model_default = Customer._meta.get_field("customer_type").default
        valid = {value for value, _ in Customer.CustomerType.choices}
        self.assertIn(model_default, valid)
        for fn in (customers_core.create_customer, mcp_adapter.create_customer):
            default = inspect.signature(fn).parameters["customer_type"].default
            self.assertEqual(default, model_default, fn.__module__)
