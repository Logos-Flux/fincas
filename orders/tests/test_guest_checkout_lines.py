"""Guest checkout copies the cart lines onto the order (OP #3426).

The storefront identity posts ``lines``; the server prices them from the
variant's public price, totals the order, checks stock without reserving it,
and answers a narrow confirmation. Any refused line creates nothing.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from core.authentication import issue_api_jwt
from core.models import Company, DocumentSequence
from core.plugins import signals
from customers.models import Customer
from inventory.models import Warehouse, WarehouseStock
from orders import guest_checkout
from orders.models import Order, OrderLineItem, ShipmentGroup
from products.models import Product, ProductVariant

API_KEY = "test-" + secrets.token_hex(16)
CONCURRENT_KEY = "-".join(["concurrent", "checkout", "0" * 8])
HELD_KEY = "-".join(["held", "checkout", "1" * 8])
URL = "/api/v1/orders/orders/"


def _variant(product, sku, *, retail="100.00", sale=None, stock=10,
             status=ProductVariant.StockStatus.IN_STOCK, **extra):
    return ProductVariant.objects.create(
        product=product, sku=sku,
        retail_price=Decimal(retail) if retail is not None else None,
        sale_price=Decimal(sale) if sale is not None else None,
        stock=stock, stock_status=status, **extra,
    )


@override_settings(API_KEY=API_KEY)
class GuestCheckoutLinesTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="SoloCo")
        self.client = APIClient(
            HTTP_AUTHORIZATION=f"Bearer {issue_api_jwt(lifetime_s=3600, sub='storefront')}"
        )
        self.panel = Product.objects.create(title="Solar Panel", part_number="SP-1", slug="sp-1")
        self.panel_v = _variant(self.panel, "SP-1-A", retail="250.00", stock=20, name="400 W")
        self.cable = Product.objects.create(title="Cable", part_number="CB-1", slug="cb-1")
        # A sale price is the public price (what the catalog shows).
        self.cable_v = _variant(self.cable, "CB-1-A", retail="12.50", sale="9.99", stock=100)

    def post(self, lines, **extra):
        body = {
            "email": "guest@example.com",
            "customer_name": "Guest Shopper",
            "shipping_address": "1 Main St",
            "payment_method": "net_30",
            "lines": lines,
        }
        body.update(extra)
        return self.client.post(URL, body, format="json")

    def line(self, variant, quantity=1, **extra):
        return {"variant": str(variant.pk), "quantity": quantity, **extra}

    def assertNothingCreated(self):
        self.assertFalse(Order.objects.exists())
        self.assertFalse(OrderLineItem.objects.exists())
        self.assertFalse(ShipmentGroup.objects.exists())

    def refusal(self, response, code="invalid_lines"):
        self.assertEqual(response.status_code, 400, response.content)
        body = response.json()
        self.assertEqual(body["code"], code)
        self.assertTrue(body["detail"])
        return body

    # --- priced, totalled, grouped -------------------------------------

    def test_lines_are_priced_and_totalled_server_side(self):
        response = self.post([self.line(self.panel_v, 2), self.line(self.cable_v, 3)])
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        order = Order.objects.get(pk=body["id"])

        lines = list(order.line_items.order_by("sort_order"))
        self.assertEqual([(li.variant_id, li.quantity) for li in lines],
                         [(self.panel_v.pk, 2), (self.cable_v.pk, 3)])
        self.assertEqual(lines[0].unit_price_ex_tax, Decimal("250.00"))
        self.assertEqual(lines[0].line_total_ex_tax, Decimal("500.00"))
        self.assertEqual(lines[0].item_name, "400 W")
        self.assertEqual(lines[0].sku, "SP-1-A")
        self.assertEqual(lines[1].unit_price_ex_tax, Decimal("9.99"))
        self.assertEqual(lines[1].list_price, Decimal("12.50"))
        self.assertEqual(lines[1].line_total_ex_tax, Decimal("29.97"))
        self.assertEqual(lines[1].item_name, "Cable")  # no variant name: the product title

        self.assertEqual(order.subtotal_ex_tax, Decimal("529.97"))
        self.assertEqual(order.tax_total, Decimal("0.00"))  # no tax engine: staff add tax
        self.assertEqual(order.grand_total, Decimal("529.97"))
        self.assertEqual(order.balance_due, Decimal("529.97"))
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(order.company, self.company)
        self.assertIsNone(order.customer)

        self.assertEqual(body["order_number"], order.order_number)
        self.assertTrue(body["order_number"].startswith("SO-"))
        self.assertEqual(body["total_amount"], "529.97")
        self.assertEqual(body["grand_total"], "529.97")
        self.assertEqual(body["subtotal"], "529.97")
        self.assertEqual(body["tax_total"], "0.00")
        self.assertEqual(body["currency"], "USD")
        self.assertEqual(body["status"], "pending")
        self.assertEqual(
            [(li["variant"], li["sku"], li["quantity"], li["unit_price"], li["line_total"])
             for li in body["lines"]],
            [(str(self.panel_v.pk), "SP-1-A", 2, "250.00", "500.00"),
             (str(self.cable_v.pk), "CB-1-A", 3, "9.99", "29.97")],
        )

    def test_lines_land_in_one_default_shipment_group(self):
        response = self.post([self.line(self.panel_v), self.line(self.cable_v)])
        order = Order.objects.get(pk=response.json()["id"])
        group = ShipmentGroup.objects.get(order=order)
        self.assertEqual(group.name, "Shipment 1")
        self.assertEqual(group.sort_order, 0)
        self.assertEqual(set(order.line_items.values_list("shipment_group", flat=True)), {group.pk})

    def test_confirmation_leaks_no_costs_or_internal_fields(self):
        self.panel_v.wholesale_price = Decimal("150.00")
        self.panel_v.save()
        body = self.post([self.line(self.panel_v)]).json()
        for key in ("metadata", "ip_address", "internal_notes", "unit_cost", "margin_amount"):
            self.assertNotIn(key, body)
            self.assertNotIn(key, body["lines"][0])
        self.assertNotIn("150.00", str(body))

    def test_client_prices_totals_and_header_fields_are_ignored(self):
        customer = Customer.objects.create(
            first_name="Real", last_name="Customer", email="real@example.com", company=self.company,
        )
        response = self.post(
            [self.line(self.panel_v, 1, unit_price="0.01", line_total="0.01", price="0.01")],
            grand_total="0.01", subtotal_ex_tax="0.01", total_amount="0.01",
            status=Order.Status.COMPLETED, payment_status="paid",
            discount_amount="999.00", discount_percent="50", coupon_discount="99",
            amount_paid="250.00", company=str(uuid.uuid4()), customer=str(customer.pk),
            items=[{"product_id": 1, "quantity": 99, "unit_price": "0.01"}],
        )
        self.assertEqual(response.status_code, 201, response.content)
        order = Order.objects.get(pk=response.json()["id"])
        self.assertEqual(order.grand_total, Decimal("250.00"))
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(order.payment_status, Order.PaymentStatus.UNPAID)
        self.assertEqual(order.discount_amount, 0)
        self.assertEqual(order.discount_percent, 0)
        self.assertEqual(order.coupon_discount, 0)
        self.assertEqual(order.amount_paid, 0)
        self.assertEqual(order.company, self.company)
        self.assertIsNone(order.customer)
        self.assertEqual(order.line_items.get().unit_price_ex_tax, Decimal("250.00"))
        self.assertEqual(order.line_items.count(), 1)  # the legacy `items` key is not read

    # --- stock: check only ---------------------------------------------

    def test_out_of_stock_line_refused_with_code_and_nothing_created(self):
        sold_out = _variant(self.panel, "SP-1-B", stock=0)
        seq_before = DocumentSequence.objects.filter(company=self.company).values_list(
            "next_number", flat=True).first()
        body = self.refusal(self.post([self.line(self.cable_v), self.line(sold_out, 1)]))
        self.assertEqual(body["lines"], [{
            "index": 1, "code": "out_of_stock", "detail": body["lines"][0]["detail"],
            "variant": str(sold_out.pk), "product": None, "available": 0,
        }])
        self.assertNothingCreated()
        seq_after = DocumentSequence.objects.filter(company=self.company).values_list(
            "next_number", flat=True).first()
        self.assertEqual(seq_before, seq_after)  # no order number burned

    def test_flagged_out_of_stock_refused_even_with_units(self):
        flagged = _variant(self.panel, "SP-1-C", stock=5, status=ProductVariant.StockStatus.OUT_OF_STOCK)
        body = self.refusal(self.post([self.line(flagged)]))
        self.assertEqual(body["lines"][0]["code"], "out_of_stock")

    def test_more_than_in_stock_refused_with_available(self):
        body = self.refusal(self.post([self.line(self.panel_v, 21)]))
        self.assertEqual(body["lines"][0]["code"], "insufficient_stock")
        self.assertEqual(body["lines"][0]["available"], 20)
        self.assertNothingCreated()

    def test_backorder_and_preorder_allowed_without_stock(self):
        back = _variant(self.panel, "SP-1-BO", stock=0, status=ProductVariant.StockStatus.BACKORDER)
        pre = _variant(self.cable, "CB-1-PO", stock=-3, status=ProductVariant.StockStatus.PREORDER)
        response = self.post([self.line(back, 4), self.line(pre, 2)])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Order.objects.get().line_items.count(), 2)

    def test_stock_is_never_reserved_or_written(self):
        warehouse = Warehouse.objects.create(warehouse_code="WH-1", name="Main", company=self.company)
        stock = WarehouseStock.objects.create(
            warehouse=warehouse, variant=self.panel_v, quantity=Decimal("20"),
        )
        before = WarehouseStock.objects.filter(pk=stock.pk).values().get()
        response = self.post([self.line(self.panel_v, 5)])
        self.assertEqual(response.status_code, 201, response.content)
        after = WarehouseStock.objects.filter(pk=stock.pk).values().get()
        self.assertEqual(before, after)
        self.panel_v.refresh_from_db()
        self.assertEqual(self.panel_v.stock, 20)

    # --- what may be sold ------------------------------------------------

    def test_unsellable_variants_refused_as_not_available(self):
        hidden_product = Product.objects.create(
            title="Hidden", part_number="H-1", slug="h-1", visible_on_website=False)
        inactive_product = Product.objects.create(
            title="Retired", part_number="R-1", slug="r-1", is_active=False)
        cases = {
            "inactive variant": _variant(self.panel, "SP-1-X", is_active=False),
            "hidden variant": _variant(self.panel, "SP-1-Y", visible_on_website=False),
            "hidden product": _variant(hidden_product, "H-1-A"),
            "inactive product": _variant(inactive_product, "R-1-A"),
        }
        for label, variant in cases.items():
            with self.subTest(label):
                body = self.refusal(self.post([self.line(variant)]))
                self.assertEqual(body["lines"][0]["code"], "not_available")
        for label, ref in {"unknown": str(uuid.uuid4()), "malformed": "not-a-uuid", "int": 7}.items():
            with self.subTest(label):
                body = self.refusal(self.post([{"variant": ref, "quantity": 1}]))
                self.assertEqual(body["lines"][0]["code"], "not_available")
        self.assertNothingCreated()

    def test_unpriced_variant_refused(self):
        unpriced = _variant(self.panel, "SP-1-P", retail=None)
        body = self.refusal(self.post([self.line(unpriced)]))
        self.assertEqual(body["lines"][0]["code"], "price_unavailable")

    def test_product_line_resolves_its_only_sellable_variant(self):
        _variant(self.cable, "CB-1-OLD", is_active=False)  # not sellable, not a choice
        response = self.post([{"product": str(self.cable.pk), "quantity": 2}])
        self.assertEqual(response.status_code, 201, response.content)
        line = Order.objects.get().line_items.get()
        self.assertEqual(line.variant, self.cable_v)
        self.assertEqual(line.unit_price_ex_tax, Decimal("9.99"))

    def test_product_line_with_several_variants_needs_a_variant(self):
        _variant(self.panel, "SP-1-B2", retail="300.00")
        body = self.refusal(self.post([{"product": str(self.panel.pk), "quantity": 1}]))
        self.assertEqual(body["lines"][0]["code"], "variant_required")
        self.assertEqual(body["lines"][0]["product"], str(self.panel.pk))
        self.assertNothingCreated()

    # --- shape of the request ------------------------------------------

    def test_header_only_checkout_is_refused(self):
        for label, lines in {"missing": None, "empty": [], "not a list": {"a": 1}}.items():
            with self.subTest(label):
                body = {"email": "guest@example.com"}
                if lines is not None:
                    body["lines"] = lines
                response = self.client.post(URL, body, format="json")
                self.refusal(response, code="lines_required")
        self.assertNothingCreated()

    def test_line_count_is_capped(self):
        lines = [self.line(self.cable_v)] * (guest_checkout.MAX_LINES + 1)
        self.refusal(self.post(lines), code="too_many_lines")
        self.assertNothingCreated()

    def test_quantity_must_be_a_positive_whole_number(self):
        for quantity in (0, -1, "2", 1.5, True, None, guest_checkout.MAX_QUANTITY + 1):
            with self.subTest(quantity=quantity):
                body = self.refusal(self.post([self.line(self.cable_v, quantity)]))
                self.assertEqual(body["lines"][0]["code"], "invalid_quantity")
        self.assertNothingCreated()

    def test_malformed_lines_are_refused(self):
        body = self.refusal(self.post(["nope", {"quantity": 1}]))
        self.assertEqual([e["code"] for e in body["lines"]], ["invalid_line", "invalid_line"])
        self.assertEqual([e["index"] for e in body["lines"]], [0, 1])

    def test_every_bad_line_is_reported(self):
        sold_out = _variant(self.panel, "SP-1-Z", stock=0)
        body = self.refusal(self.post([
            self.line(sold_out), self.line(self.cable_v), self.line(self.cable_v, 0),
        ]))
        self.assertEqual([(e["index"], e["code"]) for e in body["lines"]],
                         [(0, "out_of_stock"), (2, "invalid_quantity")])

    def test_duplicate_variants_are_merged(self):
        response = self.post([
            self.line(self.cable_v, 2), self.line(self.panel_v, 1),
            {"product": str(self.cable.pk), "quantity": 3},
        ])
        self.assertEqual(response.status_code, 201, response.content)
        order = Order.objects.get()
        self.assertEqual(
            [(li.variant_id, li.quantity) for li in order.line_items.order_by("sort_order")],
            [(self.cable_v.pk, 5), (self.panel_v.pk, 1)],
        )
        self.assertEqual(order.grand_total, Decimal("299.95"))  # 5 x 9.99 + 250

    def test_merged_quantity_is_what_the_stock_check_sees(self):
        body = self.refusal(self.post([self.line(self.panel_v, 15), self.line(self.panel_v, 10)]))
        self.assertEqual(body["lines"][0]["code"], "insufficient_stock")
        self.assertEqual(body["lines"][0]["index"], 0)

    # --- review fixes (PR #234) ------------------------------------------

    def test_zero_price_is_not_orderable(self):
        free = _variant(self.panel, "SP-1-FREE", retail="0.00")
        body = self.refusal(self.post([self.line(free)]))
        self.assertEqual(body["lines"][0]["code"], "price_unavailable")
        self.assertNothingCreated()

    def test_a_line_past_the_money_columns_is_refused_not_a_500(self):
        _, max_line, _ = guest_checkout.money_limits()
        # 9999 x 10,002 = 100,009,998 > 99,999,999.99: the column cap, read from the model.
        self.assertEqual(max_line, Decimal("99999999.99"))
        pricey = _variant(self.panel, "SP-1-BIG", retail="10002.00", stock=0,
                          status=ProductVariant.StockStatus.BACKORDER)
        body = self.refusal(self.post([self.line(pricey, 9999)]))
        self.assertEqual(body["lines"][0]["code"], "order_too_large")
        self.assertEqual(body["lines"][0]["variant"], str(pricey.pk))
        self.assertNothingCreated()

    def test_a_unit_price_past_its_column_is_refused(self):
        max_unit, _, _ = guest_checkout.money_limits()
        huge = _variant(self.panel, "SP-1-HUGE", retail=str(max_unit + 1), stock=5)
        body = self.refusal(self.post([self.line(huge, 1)]))
        self.assertEqual(body["lines"][0]["code"], "order_too_large")

    def test_lines_summing_past_the_order_columns_are_refused(self):
        a = _variant(self.panel, "SP-1-HA", retail="9000.00", stock=0,
                     status=ProductVariant.StockStatus.PREORDER)
        b = _variant(self.cable, "CB-1-HB", retail="9000.00", stock=0,
                     status=ProductVariant.StockStatus.PREORDER)
        # Each line is 89,991,000.00 (fits); together they don't.
        body = self.refusal(self.post([self.line(a, 9999), self.line(b, 9999)]),
                            code="order_too_large")
        self.assertNotIn("lines", body)
        self.assertNothingCreated()

    def test_client_metadata_is_never_stored(self):
        response = self.post(
            [self.line(self.panel_v)],
            metadata={"_reserved": "planted", "provenance": "x", "party": "p",
                      "blob": "x" * 100_000},
            shipping_address={"nested": "x" * 100_000},
            customer_name="  Ada  ",
            company_name="y" * 5000,
        )
        self.assertEqual(response.status_code, 201, response.content)
        metadata = Order.objects.get().metadata
        for key in ("_reserved", "provenance", "party", "blob", "shipping_address"):
            self.assertNotIn(key, metadata)
        self.assertEqual(metadata["customer_name"], "Ada")
        self.assertEqual(len(metadata["company_name"]), 1000)
        self.assertLess(len(json.dumps(metadata)), 2000)

    @override_settings(TRUSTED_PROXIES=["127.0.0.1/32"])
    def test_client_ip_is_the_one_past_the_proxy(self):
        response = self.client.post(
            URL, {"email": "g@example.com", "lines": [self.line(self.panel_v)]},
            format="json", HTTP_X_FORWARDED_FOR="203.0.113.9",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Order.objects.get().ip_address, "203.0.113.9")

    @override_settings(GOLDEN_GUEST_ORDER_RATE="2/minute", TRUSTED_PROXIES=["127.0.0.1/32"])
    def test_guest_checkout_is_throttled_per_client_ip(self):
        cache.clear()
        self.addCleanup(cache.clear)

        def post(ip):
            return self.client.post(
                URL, {"email": "g@example.com", "lines": [self.line(self.cable_v)]},
                format="json", HTTP_X_FORWARDED_FOR=ip,
            )

        self.assertEqual(post("198.51.100.1").status_code, 201)
        self.assertEqual(post("198.51.100.1").status_code, 201)
        throttled = post("198.51.100.1")
        self.assertEqual(throttled.status_code, 429)
        self.assertEqual(Order.objects.count(), 2)
        # Another shopper behind the same proxy has their own budget.
        self.assertEqual(post("198.51.100.2").status_code, 201)

    # --- idempotency (OP #3431) ----------------------------------------

    # A low-entropy UUID built at runtime: gitleaks flags a random-looking
    # literal on a line with "KEY" (CLAUDE.md, gitleaks).
    KEY = str(uuid.UUID(int=3431))

    def keyed(self, lines, key=None, **extra):
        body = {"email": "guest@example.com", "lines": lines}
        body.update(extra)
        return self.client.post(URL, body, format="json",
                                HTTP_IDEMPOTENCY_KEY=key or self.KEY)

    def test_a_non_string_email_is_refused_not_a_500(self):
        for email in (["a@example.com"], 42, {"x": 1}):
            response = self.client.post(URL, {"email": email, "lines": [self.line(self.panel_v)]},
                                        format="json")
            self.assertEqual(response.status_code, 400, (email, response.content))
        self.assertNothingCreated()

    def test_a_retried_checkout_returns_the_first_order(self):
        first = self.keyed([self.line(self.panel_v, 2)])
        self.assertEqual(first.status_code, 201, first.content)
        again = self.keyed([self.line(self.panel_v, 2)])
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(again.json()["id"], first.json()["id"])
        self.assertEqual(again.json()["order_number"], first.json()["order_number"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderLineItem.objects.count(), 1)

    def test_a_replay_never_shows_the_staff_serializer(self):
        self.keyed([self.line(self.panel_v)])
        body = self.keyed([self.line(self.panel_v)]).json()
        self.assertIn("total_amount", body)
        for key in ("line_items", "metadata", "ip_address", "internal_notes", "external_id"):
            self.assertNotIn(key, body)

    def test_the_raw_key_is_never_stored(self):
        self.keyed([self.line(self.panel_v)])
        stored = Order.objects.get().external_id
        self.assertTrue(stored.startswith("storefront:"))
        self.assertNotIn(self.KEY, stored)

    def test_same_key_different_cart_is_refused(self):
        self.keyed([self.line(self.panel_v, 1)])
        response = self.keyed([self.line(self.panel_v, 3)])
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], "idempotency_key_reused")
        self.assertEqual(Order.objects.count(), 1)

    def test_same_key_different_email_is_refused(self):
        self.keyed([self.line(self.panel_v)])
        response = self.keyed([self.line(self.panel_v)], email="someone-else@example.com")
        self.assertEqual(response.status_code, 409, response.content)

    def test_email_case_and_line_order_do_not_make_a_new_checkout(self):
        self.keyed([self.line(self.panel_v), self.line(self.cable_v, 2)])
        response = self.keyed([self.line(self.cable_v, 2), self.line(self.panel_v)],
                              email="Guest@Example.com")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Order.objects.count(), 1)

    def test_a_resubmit_with_corrected_details_is_refused_not_replayed(self):
        # Answering the old order would silently drop the correction.
        self.keyed([self.line(self.panel_v)], shipping_address="1 Main St")
        for change in ({"shipping_address": "2 Main St"}, {"coupon_code": "SAVE10"},
                       {"po_number": "PO-9"}, {"notes": "gate code 4"}):
            response = self.keyed([self.line(self.panel_v)],
                                  **{"shipping_address": "1 Main St", **change})
            self.assertEqual(response.status_code, 409, (change, response.content))
            self.assertEqual(response.json()["code"], "idempotency_key_reused")
        self.assertEqual(Order.objects.count(), 1)

    def test_a_replay_is_answered_even_after_the_line_sold_out(self):
        first = self.keyed([self.line(self.panel_v, 2)])
        ProductVariant.objects.filter(pk=self.panel_v.pk).update(
            stock=0, stock_status=ProductVariant.StockStatus.OUT_OF_STOCK)
        again = self.keyed([self.line(self.panel_v, 2)])
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(again.json()["id"], first.json()["id"])

    def test_different_keys_make_different_orders(self):
        self.keyed([self.line(self.panel_v)])
        self.keyed([self.line(self.panel_v)], key="a" * 16)
        self.assertEqual(Order.objects.count(), 2)

    def test_no_key_always_creates(self):
        self.post([self.line(self.panel_v)])
        self.post([self.line(self.panel_v)])
        self.assertEqual(Order.objects.count(), 2)

    def test_a_key_past_the_window_starts_a_new_order(self):
        self.keyed([self.line(self.panel_v)])
        Order.objects.update(created_at=timezone.now() - guest_checkout.IDEMPOTENCY_WINDOW
                             - timedelta(minutes=1))
        response = self.keyed([self.line(self.panel_v)])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Order.objects.count(), 2)

    def test_malformed_keys_are_refused_and_create_nothing(self):
        for key in ("short", "has space in it here", "x" * 201, "bad/char-0123456789"):
            response = self.keyed([self.line(self.panel_v)], key=key)
            self.assertEqual(response.status_code, 400, (key, response.content))
            self.assertEqual(response.json()["code"], "idempotency_key_invalid")
        self.assertNothingCreated()

    def test_body_external_id_is_ignored_on_guest_checkout(self):
        # Only the header is a key; a body external_id must not reach the order.
        self.post([self.line(self.panel_v)], external_id="planted-ref")
        self.assertEqual(Order.objects.get().external_id, "")

    def test_a_refused_keyed_checkout_can_be_retried_with_the_same_key(self):
        refused = self.keyed([self.line(self.panel_v, 999)])
        self.assertEqual(refused.status_code, 400, refused.content)
        ok = self.keyed([self.line(self.panel_v, 1)])
        self.assertEqual(ok.status_code, 201, ok.content)

    # --- side effects -----------------------------------------------------

    def test_order_placed_receivers_see_the_priced_order(self):
        seen = []

        def receiver(sender, order, **kwargs):
            seen.append((order.grand_total, order.line_items.count()))

        signals.order_placed.connect(receiver, sender=Order, dispatch_uid="test-guest-lines")
        self.addCleanup(signals.order_placed.disconnect, dispatch_uid="test-guest-lines", sender=Order)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post([self.line(self.panel_v, 2)])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(seen, [(Decimal("500.00"), 1)])

    def test_outbox_consumers_build_from_the_priced_order(self):
        """Durable consumers build at emit time: emit must come after the lines."""
        from core import outbox

        built = []

        def build(signal, sender, obj, kwargs):
            order = kwargs["order"]
            built.append((order.grand_total, order.line_items.count()))
            return []

        consumer = outbox.OutboxConsumer(
            name="test.guest_lines", signals=("order_placed",), build=build,
            handle=lambda event: outbox.Done(),
        )
        outbox.register(consumer, "tests")
        self.addCleanup(outbox._builtin.pop, "test.guest_lines", None)
        response = self.post([self.line(self.panel_v, 2)])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(built, [(Decimal("500.00"), 1)])

    def test_query_count_does_not_grow_with_lines(self):
        variants = [_variant(self.cable, f"CB-Q-{i}", retail="5.00") for i in range(10)]

        def count(lines):
            Order.objects.all().delete()
            with CaptureQueriesContext(connection) as ctx:
                response = self.post(lines)
            self.assertEqual(response.status_code, 201, response.content)
            return len(ctx.captured_queries)

        count([self.line(variants[0])])  # the first order also creates the number sequence
        one = count([self.line(variants[0])])
        ten = count([self.line(v) for v in variants])
        self.assertEqual(one, ten)
        # ~20 of these are the bearer resolving the storefront identity (it
        # re-converges its group on every request); the checkout itself is ~20.
        self.assertLessEqual(ten, 45)

    def test_storefront_still_cannot_read_the_order_it_created(self):
        order_id = self.post([self.line(self.panel_v)]).json()["id"]
        self.assertEqual(self.client.get(f"{URL}{order_id}/").status_code, 403)
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/orders/order-lines/").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/customers/customers/").status_code, 403)


class GuestCheckoutStaffPathTests(TestCase):
    """Staff creates are unchanged: no lines needed, the full serializer answers."""

    def test_staff_create_still_header_only(self):
        from django.contrib.auth.models import User

        from core.models import UserProfile

        company = Company.objects.create(name="StaffCo")
        user = User.objects.create_superuser("boss", "boss@example.com", "pw")
        UserProfile.objects.get_or_create(user=user, defaults={"company": company})
        client = APIClient()
        client.force_authenticate(user)
        customer = Customer.objects.create(
            first_name="A", last_name="B", email="a@example.com", company=company,
        )
        response = client.post(URL, {"company": str(company.pk), "customer": str(customer.pk)},
                               format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIn("line_items", response.json())


@override_settings(API_KEY=API_KEY)
class GuestCheckoutConcurrentKeyTests(TransactionTestCase):
    """Two first submits with one key at once make one order (Postgres only)."""

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("needs Postgres (advisory locks, concurrent writers)")
        # No cache.clear(): CI's parallel workers share one Redis, and a flush
        # resets other tests' throttle counters mid-test. The guest-order rate
        # is off in the test settings anyway.
        self.company = Company.objects.create(name="SoloCo")
        product = Product.objects.create(title="Solar Panel", part_number="SP-1", slug="sp-1")
        self.variant = _variant(product, "SP-1-A", retail="250.00", stock=20)
        self.auth = f"Bearer {issue_api_jwt(lifetime_s=3600, sub='storefront')}"

    def test_concurrent_submits_with_one_key_make_one_order(self):
        import threading

        from django.db import connections

        barrier = threading.Barrier(2)
        statuses = []

        def submit():
            try:
                client = APIClient(HTTP_AUTHORIZATION=self.auth)
                barrier.wait()
                response = client.post(
                    URL, {"email": "guest@example.com",
                          "lines": [{"variant": str(self.variant.pk), "quantity": 1}]},
                    format="json", HTTP_IDEMPOTENCY_KEY=CONCURRENT_KEY,
                )
                statuses.append(response.status_code)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(statuses), [200, 201])
        self.assertEqual(Order.objects.count(), 1)


    def test_a_submit_waiting_too_long_for_its_key_answers_in_progress(self):
        import psycopg2
        from django.db import connection as conn

        from orders import guest_checkout as gc

        ref = gc.idempotency_ref(HELD_KEY)
        params = conn.get_connection_params()
        holder = psycopg2.connect(**params)
        try:
            with holder.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                            [f"golden.guest_checkout:{ref}"])
            client = APIClient(HTTP_AUTHORIZATION=self.auth)
            with mock.patch.object(gc, "LOCK_WAIT", "200ms"):
                response = client.post(
                    URL, {"email": "guest@example.com",
                          "lines": [{"variant": str(self.variant.pk), "quantity": 1}]},
                    format="json", HTTP_IDEMPOTENCY_KEY=HELD_KEY,
                )
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(response.json()["code"], "checkout_in_progress")
        self.assertEqual(Order.objects.count(), 0)


class GuestCheckoutCorsTests(TestCase):
    """A cross-origin storefront may send Idempotency-Key (OP #3431)."""

    @override_settings(CORS_ALLOWED_ORIGINS=["https://shop.example.com"])
    def test_preflight_allows_the_idempotency_key_header(self):
        response = self.client.options(
            URL,
            HTTP_ORIGIN="https://shop.example.com",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization, content-type, idempotency-key",
        )
        allowed = response.headers.get("access-control-allow-headers", "").lower()
        self.assertIn("idempotency-key", allowed)
        self.assertIn("authorization", allowed)
