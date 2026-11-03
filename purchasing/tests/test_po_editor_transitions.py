"""The PO editor moves status only along ``PurchaseOrder.VALID_TRANSITIONS`` (R4.06).

Observed by the release harness on 2026-10-17 (docs/release-testing.md):
the editor's Status field wrote any status directly, so the transition map
guarded only the API. The editor now offers the legal moves in the action
bar's status control (``set_status_*``), the form's status field is
disabled, and an illegal move is refused with a message.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from core.models import Company
from products.models import Product, ProductVariant
from purchasing.models import POLineItem, PurchaseOrder, Supplier

from .test_po_editor import management, row
from .test_recalculate import company_user


class PurchaseOrderEditorTransitionTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.supplier = Supplier.objects.create(
            company_name="Keystone", supplier_code="SUPP-002", company=self.company, is_active=True,
        )
        product = Product.objects.create(title="Actuator", part_number="ACT-500W", slug="act-500w")
        self.variant = ProductVariant.objects.create(product=product, sku="ACT-500W")
        self.po = PurchaseOrder.objects.create(company=self.company, supplier=self.supplier)
        self.line = POLineItem.objects.create(
            purchase_order=self.po, variant=self.variant, item_name="Actuator", our_sku="ACT-500W",
            quantity_ordered=Decimal("10"), unit_cost=Decimal("90"),
        )
        self.user = company_user(self.company, username="po-editor", uid="uid-po-editor")
        self.client.force_login(self.user)
        self.url = reverse("erp-purchaseorder-edit", args=[self.po.pk])

    def post(self, action, **extra):
        data = {
            "po_type": "drop_ship", "supplier": str(self.supplier.pk), "company": str(self.company.pk),
            "currency": "USD", "exchange_rate": "1", "receiving_status": self.po.receiving_status,
            "payment_status": "unpaid", "ship_to_type": "warehouse",
            "subtotal": "900.00", "tax_total": "0", "shipping_cost": "0", "other_charges": "0",
            "discount_amount": "0", "discount_percent": "0", "grand_total": "900.00",
            "amount_paid": "0", "balance_due": "900.00", "_action": action,
        }
        data.update(management(1, initial=1))
        data.update(row(0, id=str(self.line.pk), variant=str(self.variant.pk), item_name="Actuator",
                        our_sku="ACT-500W", quantity_ordered="10", unit_cost="90.0000",
                        line_total="900.00", purchasing_uom="each"))
        data.update(extra)
        return self.client.post(self.url, data)

    def test_the_status_control_offers_only_the_legal_moves(self):
        page = self.client.get(self.url).content.decode()
        for value in PurchaseOrder.VALID_TRANSITIONS["draft"]:
            self.assertIn(f'value="set_status_{value}"', page)
        self.assertNotIn('value="set_status_closed"', page)
        self.assertNotIn('value="set_status_received"', page)
        # The form's own status field is disabled.
        self.assertRegex(page, r'<select[^>]*name="status"[^>]*disabled')

    def test_an_illegal_move_is_refused_with_a_message(self):
        response = self.post("set_status_closed")
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        shown = " ".join(str(m) for m in response.wsgi_request._messages)
        self.assertIn("Status not changed", shown)
        self.assertIn("Cannot transition from 'draft' to 'closed'", shown)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.Status.DRAFT)

    def test_the_status_field_in_the_post_is_ignored(self):
        response = self.post("save_continue", status="closed")
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.Status.DRAFT)

    def test_a_legal_chain_lands_on_closed(self):
        for target in ("sent", "acknowledged", "shipped", "received"):
            self.post(f"set_status_{target}")
            self.po.refresh_from_db()
            self.assertEqual(self.po.status, target)
        page = self.client.get(self.url).content.decode()
        self.assertIn('value="set_status_closed"', page)
        self.assertNotIn('value="set_status_draft"', page)
        self.post("set_status_closed")
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.Status.CLOSED)
        # Closed: no transition control, no cancel button.
        page = self.client.get(self.url).content.decode()
        self.assertNotIn("status-transition-select", page)
        self.assertNotIn('value="cancel_order"', page)

    def test_cancel_goes_through_the_transition_map(self):
        self.post("cancel_order")
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.Status.CANCELLED)
        # From shipped, cancel is not a legal move.
        other = PurchaseOrder.objects.create(company=self.company, supplier=self.supplier, status="shipped")
        self.po = other
        self.line = POLineItem.objects.create(
            purchase_order=other, variant=self.variant, item_name="Actuator", our_sku="ACT-500W",
            quantity_ordered=Decimal("10"), unit_cost=Decimal("90"),
        )
        self.url = reverse("erp-purchaseorder-edit", args=[other.pk])
        self.assertNotIn('value="cancel_order"', self.client.get(self.url).content.decode())
        self.post("cancel_order")
        other.refresh_from_db()
        self.assertEqual(other.status, "shipped")

    def test_header_charges_allocate_as_landed_cost_on_save(self):
        self.post("save_continue", shipping_cost="100.00", other_charges="20.00")
        self.po.refresh_from_db()
        self.line.refresh_from_db()
        self.assertEqual(self.po.shipping_cost, Decimal("100.00"))
        self.assertEqual(self.line.freight_allocation, Decimal("100.00"))
        self.assertEqual(self.line.other_landed_costs, Decimal("20.00"))
        self.assertEqual(self.line.landed_cost_per_unit, Decimal("102.0000"))
        self.assertEqual(self.po.grand_total, Decimal("1020.00"))
