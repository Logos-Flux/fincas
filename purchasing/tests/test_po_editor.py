"""The purchase-order editor saves what it shows (R1.11, R1.12).

Found by the release harness on 2026-10-17 (docs/release-testing.md):

- ``POLineItemForm`` required ``purchasing_uom``, which the editor never
  renders, so no purchase order could be created from the back office at
  all; the failed POST re-rendered with no message.
- "Create PO" from a shipment group prefilled the lines, but the hidden
  variant select rendered no option (the queryset was ``none()`` for an
  initial value), so every prefilled row posted with no variant; the
  blank extra row, carrying the JS's ``0.00`` totals, counted as filled.
- The vendor picker offered every active supplier while the shipment
  group's save keeps only the document company's (R1.10).
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from core.models import Company
from customers.models import Customer
from orders.models import Order, OrderLineItem, ShipmentGroup
from products.models import Product, ProductVariant
from purchasing.models import POLineItem, PurchaseOrder, Supplier

from .test_recalculate import company_user

PREFIX = "line_items"


def management(total, initial=0):
    return {
        f"{PREFIX}-TOTAL_FORMS": str(total),
        f"{PREFIX}-INITIAL_FORMS": str(initial),
        f"{PREFIX}-MIN_NUM_FORMS": "0",
        f"{PREFIX}-MAX_NUM_FORMS": "1000",
    }


def row(i, **fields):
    return {f"{PREFIX}-{i}-{name}": value for name, value in fields.items()}


class PurchaseOrderEditorTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.supplier = Supplier.objects.create(
            company_name="Keystone Drop-Ship Partners", supplier_code="SUPP-002",
            company=self.company, is_active=True,
        )
        product = Product.objects.create(title="Actuator", part_number="ACT-500W", slug="act-500w")
        self.variant = ProductVariant.objects.create(product=product, sku="ACT-500W")
        self.user = company_user(self.company, username="po-editor", uid="uid-po-editor")
        self.client.force_login(self.user)
        self.url = reverse("erp-purchaseorder-create")

    def header(self, **extra):
        data = {
            "po_type": "drop_ship", "supplier": str(self.supplier.pk),
            "company": str(self.company.pk), "status": "draft",
            "currency": "USD", "exchange_rate": "1",
            "receiving_status": "not_received", "payment_status": "unpaid",
            "ship_to_type": "warehouse",
            "subtotal": "200.00", "tax_total": "0", "shipping_cost": "0", "other_charges": "0",
            "discount_amount": "0", "discount_percent": "0", "grand_total": "200.00",
            "amount_paid": "0", "balance_due": "200.00",
            "_action": "save",
        }
        data.update(extra)
        return data

    # -- R1.12 ---------------------------------------------------------------

    def test_a_line_without_purchasing_uom_saves_with_the_model_default(self):
        data = self.header()
        data.update(management(2))
        # What the editor posts: the picked variant, name, quantity, cost and
        # the JS-computed totals; no purchasing_uom (it is not rendered), plus
        # the blank extra row with the JS's zero totals.
        data.update(row(0, variant=str(self.variant.pk), item_name="Actuator", our_sku="ACT-500W",
                        quantity_ordered="2", unit_cost="100.00", discount_percent="0",
                        line_total="200.00"))
        data.update(row(1, variant="", item_name="", our_sku="", quantity_ordered="1",
                        unit_cost="", discount_percent="0", line_total="0.00"))
        response = self.client.post(self.url, data)
        self.assertEqual(
            response.status_code, 302,
            [str(m) for m in response.context["messages"]] if response.context else "",
        )
        self.assertRedirects(response, reverse("erp-purchaseorder-list"))
        po = PurchaseOrder.objects.get()
        line = po.line_items.get()
        self.assertEqual(line.variant_id, self.variant.pk)
        self.assertEqual(line.purchasing_uom, "each")
        self.assertEqual(line.our_sku, "ACT-500W")
        self.assertEqual(line.quantity_ordered, Decimal("2"))

    def test_a_posted_uom_is_kept(self):
        data = self.header()
        data.update(management(1))
        data.update(row(0, variant=str(self.variant.pk), item_name="Actuator",
                        quantity_ordered="4", unit_cost="50.00", purchasing_uom="case",
                        line_total="200.00"))
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(POLineItem.objects.get().purchasing_uom, "case")

    def test_a_failed_save_says_why(self):
        data = self.header()
        data.update(management(1))
        data.update(row(0, variant="", item_name="Mystery part", quantity_ordered="",
                        unit_cost="", line_total="0.00"))
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        texts = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("Line 1" in t and "variant" in t for t in texts), texts)
        self.assertEqual(PurchaseOrder.objects.count(), 0)

    # -- R1.11 ---------------------------------------------------------------

    def _order_with_group(self):
        customer = Customer.objects.create(
            first_name="Riley", last_name="Castellano", email="riley@example.com", company=self.company,
        )
        order = Order.objects.create(order_number="SO-R1-1", customer=customer, company=self.company)
        group = ShipmentGroup.objects.create(
            order=order, name="Drop ship", sort_order=0, supplier=self.supplier,
            fulfillment_type="drop_ship",
        )
        OrderLineItem.objects.create(
            order=order, shipment_group=group, variant=self.variant, sku="ACT-500W",
            item_name="Actuator", quantity=Decimal("2"), unit_price_ex_tax=Decimal("120.00"),
            unit_cost=Decimal("100.00"), line_total_ex_tax=Decimal("240.00"),
        )
        return order, group

    def test_create_po_from_a_shipment_group_prefills_variant_quantity_and_cost(self):
        order, group = self._order_with_group()
        response = self.client.get(
            f"{self.url}?order={order.pk}&shipment_group={group.pk}&supplier={self.supplier.pk}"
        )
        self.assertEqual(response.status_code, 200)
        formset = response.context["line_item_formset"]
        first = formset.forms[0]
        self.assertEqual(str(first["variant"].value()), str(self.variant.pk))
        # The hidden select carries the option, so the browser posts it back.
        self.assertIn(f'value="{self.variant.pk}" selected', str(first["variant"]))
        self.assertEqual(first["quantity_ordered"].value(), Decimal("2"))
        self.assertEqual(first["unit_cost"].value(), Decimal("100.00"))
        self.assertEqual(first["our_sku"].value(), "ACT-500W")
        html = response.content.decode()
        self.assertIn(f'name="{PREFIX}-0-our_sku"', html)

    def test_the_prefilled_page_saves_as_presented(self):
        order, group = self._order_with_group()
        get = self.client.get(f"{self.url}?order={order.pk}&shipment_group={group.pk}&supplier={self.supplier.pk}")
        formset = get.context["line_item_formset"]
        data = self.header(order=str(order.pk), shipment_group=str(group.pk))
        data.update(management(len(formset.forms)))
        for i, form in enumerate(formset.forms):
            for name in ("variant", "order_line_item", "item_name", "our_sku", "quantity_ordered",
                         "unit_cost", "discount_percent", "line_total"):
                value = form[name].value()
                data[f"{PREFIX}-{i}-{name}"] = "" if value is None else str(value)
        # The JS writes zero totals into the untouched blank row.
        data[f"{PREFIX}-{len(formset.forms) - 1}-line_total"] = "0.00"
        data[f"{PREFIX}-{len(formset.forms) - 1}-discount_percent"] = "0"
        response = self.client.post(f"{self.url}?order={order.pk}&shipment_group={group.pk}", data)
        self.assertEqual(
            response.status_code, 302,
            [str(m) for m in response.context["messages"]] if response.context else "",
        )
        po = PurchaseOrder.objects.get()
        self.assertEqual(po.shipment_group_id, group.pk)
        self.assertEqual(po.order_id, order.pk)
        line = po.line_items.get()
        self.assertEqual(line.variant_id, self.variant.pk)
        self.assertEqual(line.quantity_ordered, Decimal("2"))
        self.assertEqual(line.unit_cost, Decimal("100.00"))
        # The PO line names the order line it was raised for (R4.07), which
        # locks that line to its shipment on the order editor (R4.08).
        order_line = order.line_items.get()
        self.assertEqual(line.order_line_item_id, order_line.pk)
        from orders.line_locks import order_line_move_locks

        self.assertIn(po.po_number, order_line_move_locks(order)[order_line.pk]["reason"])

    def test_a_group_line_with_no_product_is_named_not_prefilled(self):
        order, group = self._order_with_group()
        OrderLineItem.objects.create(
            order=order, shipment_group=group, variant=None, sku="MISC-FAB",
            item_name="Fabrication", quantity=Decimal("1"), unit_price_ex_tax=Decimal("50.00"),
            line_total_ex_tax=Decimal("50.00"),
        )
        response = self.client.get(f"{self.url}?order={order.pk}&shipment_group={group.pk}")
        texts = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("MISC-FAB" in t and "no product linked" in t for t in texts), texts)
        formset = response.context["line_item_formset"]
        prefilled = [f for f in formset.forms if f["variant"].value()]
        self.assertEqual(len(prefilled), 1)

    # -- R1.10 ---------------------------------------------------------------

    def test_the_vendor_picker_offers_only_the_companys_suppliers(self):
        rival = Company.objects.create(name="Rival, Inc")
        Supplier.objects.create(company_name="Keystone Rival Supply", supplier_code="RIV-1",
                                company=rival, is_active=True)
        Supplier.objects.create(company_name="Keystone Unbound", supplier_code="UNB-1",
                                company=None, is_active=True)
        url = reverse("erp-supplier-search-ajax")
        results = self.client.get(url, {"q": "Keystone"}).json()["results"]
        self.assertEqual([r["code"] for r in results], ["SUPP-002"])
        # Naming the document's company narrows further, never widens.
        results = self.client.get(url, {"q": "Keystone", "company": str(rival.pk)}).json()["results"]
        self.assertEqual(results, [])
        results = self.client.get(url, {"q": "Keystone", "company": "not-a-uuid"}).json()["results"]
        self.assertEqual(results, [])
