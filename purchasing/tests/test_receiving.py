"""Receiving a purchase order from the back office and the API (R4.03, R4.04).

Found by the release harness on 2026-10-17 (docs/release-testing.md):
receiving existed only as plain CRUD on ``POReceipt`` / ``POReceiptLine``
(nothing moved the PO lines or stock on hand) and the PO editor had no
Receive action. ``purchasing.services.receive_purchase_order`` is the one
path now: the Receive screen and ``POST .../purchase-orders/<pk>/receive/``
both call it.
"""

from __future__ import annotations

import re
from decimal import Decimal

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounting.models import FiscalPeriod, JournalEntry
from accounting.tests.test_posting import make_posting_rules
from core.models import Company
from inventory.models import Warehouse, WarehouseStock
from products.models import Product, ProductVariant
from purchasing.models import POLineItem, POReceipt, PurchaseOrder, Supplier
from purchasing.services import ReceiptRefused, receive_purchase_order, remaining

from .test_recalculate import company_user


def once_token(html):
    match = re.search(r'name="_once" value="([0-9a-f]+)"', html)
    return match.group(1) if match else ""


class ReceivingBase(TestCase):
    def setUp(self):
        cache.clear()
        self.company = Company.objects.create(name="Example Supply, Inc")
        make_posting_rules(self.company)
        self.supplier = Supplier.objects.create(
            company_name="Northwind Fasteners", supplier_code="SUP-R4", company=self.company, is_active=True,
        )
        self.warehouse = Warehouse.objects.create(
            warehouse_code="WH-EAST", name="East", company=self.company, default_for_company=self.company,
        )
        act = Product.objects.create(title="Actuator", part_number="ACT-500W", slug="act-500w")
        brk = Product.objects.create(title="Bracket", part_number="BRK-90-HD", slug="brk-90-hd")
        self.act = ProductVariant.objects.create(product=act, sku="ACT-500W")
        self.brk = ProductVariant.objects.create(product=brk, sku="BRK-90-HD")
        self.po = PurchaseOrder.objects.create(
            company=self.company, supplier=self.supplier, status=PurchaseOrder.Status.DRAFT,
        )
        self.line_act = POLineItem.objects.create(
            purchase_order=self.po, variant=self.act, item_name="Actuator", our_sku="ACT-500W",
            quantity_ordered=Decimal("10"), unit_cost=Decimal("90"),
        )
        self.line_brk = POLineItem.objects.create(
            purchase_order=self.po, variant=self.brk, item_name="Bracket", our_sku="BRK-90-HD",
            quantity_ordered=Decimal("50"), unit_cost=Decimal("2.5"),
        )
        self.user = company_user(self.company, username="receiver", uid="uid-receiver")
        self.client.force_login(self.user)
        self.page = reverse("erp-purchaseorder-receive", args=[self.po.pk])
        self.edit = reverse("erp-purchaseorder-edit", args=[self.po.pk])

    def tearDown(self):
        cache.clear()

    def on_hand(self, variant):
        return sum((s.quantity for s in WarehouseStock.objects.filter(variant=variant)), Decimal("0"))

    def refresh(self):
        self.po.refresh_from_db()
        self.line_act.refresh_from_db()
        self.line_brk.refresh_from_db()


class ReceiveServiceTests(ReceivingBase):
    def test_a_partial_receipt_adds_stock_and_marks_the_order_receiving(self):
        receipt = receive_purchase_order(self.po, {self.line_act: Decimal("4")}, received_by=self.user.userprofile)
        self.refresh()
        self.assertEqual(receipt.status, POReceipt.Status.POSTED)
        self.assertEqual(receipt.warehouse, self.warehouse)
        self.assertEqual(receipt.lines.count(), 1)
        self.assertEqual(self.line_act.quantity_received, Decimal("4"))
        self.assertEqual(self.line_act.receiving_status, POLineItem.ReceivingStatus.PARTIAL)
        self.assertEqual(self.line_brk.quantity_received, Decimal("0"))
        self.assertEqual(self.po.receiving_status, PurchaseOrder.ReceivingStatus.PARTIAL)
        self.assertEqual(self.po.status, PurchaseOrder.Status.RECEIVING)
        self.assertEqual(self.on_hand(self.act), Decimal("4"))
        self.assertEqual(self.on_hand(self.brk), Decimal("0"))
        # F-092: the storefront's number follows the warehouse.
        self.act.refresh_from_db(fields=["stock"])
        self.assertEqual(self.act.stock, 4)
        # The goods entry posted under the purchase-receipt rule, balanced.
        entry = JournalEntry.objects.get(posting_key=f"purchase:{receipt.receipt_number}")
        debit, credit = entry.totals()
        self.assertEqual(debit, credit)
        self.assertEqual(debit, Decimal("360.00"))  # 4 of 10 at 90

    def test_the_second_receipt_completes_the_order_and_can_close_it(self):
        receive_purchase_order(self.po, {self.line_act: Decimal("4")})
        receipt = receive_purchase_order(
            self.po, {self.line_act: Decimal("6"), self.line_brk: Decimal("50")}, close_when_complete=True,
        )
        self.refresh()
        self.assertEqual(self.line_act.receiving_status, POLineItem.ReceivingStatus.RECEIVED)
        self.assertEqual(self.line_brk.receiving_status, POLineItem.ReceivingStatus.RECEIVED)
        self.assertEqual(self.po.receiving_status, PurchaseOrder.ReceivingStatus.RECEIVED)
        self.assertEqual(self.po.status, PurchaseOrder.Status.CLOSED)
        self.assertEqual(self.on_hand(self.act), Decimal("10"))
        self.assertEqual(self.on_hand(self.brk), Decimal("50"))
        self.assertEqual(self.po.receipts.count(), 2)
        self.assertEqual(remaining(self.line_act), Decimal("0"))
        self.assertTrue(JournalEntry.objects.filter(posting_key=f"purchase:{receipt.receipt_number}").exists())

    def test_without_close_the_complete_order_is_received_not_closed(self):
        receive_purchase_order(self.po, {self.line_act: Decimal("10"), self.line_brk: Decimal("50")})
        self.refresh()
        self.assertEqual(self.po.status, PurchaseOrder.Status.RECEIVED)

    def test_refusals_store_nothing(self):
        for quantities, reason in (
            ({}, "Nothing received"),
            ({self.line_act: Decimal("0")}, "Nothing received"),
            ({self.line_act: Decimal("11")}, "more than the 10 still to come"),
            ({self.line_act: Decimal("-1")}, "negative"),
        ):
            with self.assertRaisesMessage(ReceiptRefused, reason):
                receive_purchase_order(self.po, quantities)
        self.po.status = PurchaseOrder.Status.CLOSED
        self.po.save(update_fields=["status"])
        with self.assertRaisesMessage(ReceiptRefused, "closed"):
            receive_purchase_order(self.po, {self.line_act: Decimal("1")})
        self.assertEqual(POReceipt.objects.count(), 0)
        self.assertEqual(self.on_hand(self.act), Decimal("0"))
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_a_closed_period_refuses_before_anything_is_written(self):
        import datetime

        today = datetime.date.today()
        FiscalPeriod.objects.create(
            company=self.company, name=f"FY{today.year}", status=FiscalPeriod.Status.CLOSED,
            start_date=datetime.date(today.year, 1, 1), end_date=datetime.date(today.year, 12, 31),
        )
        with self.assertRaises(ValidationError):
            receive_purchase_order(self.po, {self.line_act: Decimal("4")})
        self.refresh()
        self.assertEqual(POReceipt.objects.count(), 0)
        self.assertEqual(self.line_act.quantity_received, Decimal("0"))
        self.assertEqual(self.on_hand(self.act), Decimal("0"))

    def test_the_same_idempotency_key_returns_the_original_receipt(self):
        first = receive_purchase_order(self.po, {self.line_act: Decimal("4")}, idempotency_key="k-1")
        again = receive_purchase_order(self.po, {self.line_act: Decimal("4")}, idempotency_key="k-1")
        self.assertEqual(first.pk, again.pk)
        self.refresh()
        self.assertEqual(self.line_act.quantity_received, Decimal("4"))
        self.assertEqual(self.on_hand(self.act), Decimal("4"))

    def test_a_warehouse_of_another_company_is_refused(self):
        other = Company.objects.create(name="Other")
        theirs = Warehouse.objects.create(warehouse_code="WH-X", name="Theirs", company=other)
        with self.assertRaisesMessage(ReceiptRefused, "another company"):
            receive_purchase_order(self.po, {self.line_act: Decimal("1")}, warehouse=theirs)


class ReceiveScreenTests(ReceivingBase):
    def test_the_editor_links_to_receive_and_the_screen_defaults_to_what_is_left(self):
        page = self.client.get(self.edit).content.decode()
        self.assertIn(self.page, page)
        self.assertIn("Receive", page)
        screen = self.client.get(self.page)
        self.assertEqual(screen.status_code, 200)
        html = screen.content.decode()
        self.assertIn(f'name="line-{self.line_act.pk}-quantity_received"', html)
        self.assertIn('value="10"', html)
        self.assertIn('value="50"', html)

    def test_posting_a_partial_receipt_then_the_rest(self):
        token = once_token(self.client.get(self.page).content.decode())
        response = self.client.post(self.page, {
            "_once": token, f"line-{self.line_act.pk}-quantity_received": "4",
            f"line-{self.line_brk.pk}-quantity_received": "0", "warehouse": "", "notes": "4 of 10",
        })
        self.assertRedirects(response, self.edit, fetch_redirect_response=False)
        self.refresh()
        self.assertEqual(self.line_act.quantity_received, Decimal("4"))
        self.assertEqual(self.po.receiving_status, PurchaseOrder.ReceivingStatus.PARTIAL)
        self.assertEqual(self.on_hand(self.act), Decimal("4"))
        receipt = POReceipt.objects.get()
        self.assertEqual(receipt.metadata["idempotency_key"], f"erp-once:{token}")
        # A double click: the same token records nothing more.
        self.client.post(self.page, {"_once": token, f"line-{self.line_act.pk}-quantity_received": "4"})
        self.assertEqual(POReceipt.objects.count(), 1)
        self.assertEqual(self.on_hand(self.act), Decimal("4"))
        # The rest, with close requested.
        html = self.client.get(self.page).content.decode()
        self.assertIn('value="6"', html)
        token = once_token(html)
        self.client.post(self.page, {
            "_once": token, f"line-{self.line_act.pk}-quantity_received": "6",
            f"line-{self.line_brk.pk}-quantity_received": "50", "close_when_complete": "on",
        })
        self.refresh()
        self.assertEqual(self.po.status, PurchaseOrder.Status.CLOSED)
        self.assertEqual(self.po.receiving_status, PurchaseOrder.ReceivingStatus.RECEIVED)
        self.assertEqual(self.on_hand(self.brk), Decimal("50"))
        # Closed: no Receive link on the editor, the screen says so.
        self.assertNotIn(self.page, self.client.get(self.edit).content.decode())
        self.assertContains(self.client.get(self.page), "nothing can be received")

    def test_an_over_receipt_is_a_message_and_stores_nothing(self):
        token = once_token(self.client.get(self.page).content.decode())
        response = self.client.post(self.page, {"_once": token, f"line-{self.line_act.pk}-quantity_received": "11"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Not received")
        self.assertContains(response, "more than the 10 still to come")
        self.assertEqual(POReceipt.objects.count(), 0)
        self.assertEqual(self.on_hand(self.act), Decimal("0"))


class ReceiveApiTests(ReceivingBase):
    def test_the_api_receives_through_the_same_service(self):
        api = APIClient()
        api.force_authenticate(user=self.user)
        url = f"/api/v1/purchasing/purchase-orders/{self.po.pk}/receive/"
        response = api.post(url, {
            "lines": [{"po_line_item": str(self.line_act.pk), "quantity_received": "4"}],
            "idempotency_key": "api-1",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["status"], "posted")
        self.refresh()
        self.assertEqual(self.line_act.quantity_received, Decimal("4"))
        self.assertEqual(self.po.status, PurchaseOrder.Status.RECEIVING)
        self.assertEqual(self.on_hand(self.act), Decimal("4"))
        again = api.post(url, {
            "lines": [{"po_line_item": str(self.line_act.pk), "quantity_received": "4"}],
            "idempotency_key": "api-1",
        }, format="json")
        self.assertEqual(again.status_code, 201)
        self.assertEqual(again.json()["id"], response.json()["id"])
        self.assertEqual(POReceipt.objects.count(), 1)
        refused = api.post(url, {
            "lines": [{"po_line_item": str(self.line_act.pk), "quantity_received": "100"}],
        }, format="json")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("still to come", " ".join(refused.json()["lines"]))
