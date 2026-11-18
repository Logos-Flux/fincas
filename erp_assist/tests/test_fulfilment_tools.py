"""Working an order's fulfilment through the assistant: default vendors,
split shipments, a drop-ship purchase order and a pick ticket, against the
real DRF API (same in-process bridge as ``test_create_documents``).
"""

from __future__ import annotations

from decimal import Decimal

from erp_assist import mcp_adapter
from erp_assist.core import ERPValidationError
from erp_assist.core import fulfillment as fulfillment_core
from erp_assist.core import orders as orders_core
from erp_assist.core import purchasing as purchasing_core
from erp_assist.tests.test_create_documents import _AssistCase, run
from fulfillment.models import PickTicket
from inventory.models import Warehouse, WarehouseStock
from orders.models import Order, OrderLineItem, ShipmentGroup
from products.models import Product, ProductVariant, SupplierProduct
from purchasing.models import POLineItem, PurchaseOrder, Supplier


class _FulfilmentCase(_AssistCase):
    def setUp(self):
        super().setUp()
        self.acme = Supplier.objects.create(
            company_name="Acme Wholesale", supplier_type="distributor", company=self.company,
        )
        self.bolt = Supplier.objects.create(
            company_name="Bolt Direct", supplier_type="manufacturer", company=self.company,
        )
        self.warehouse = Warehouse.objects.create(
            warehouse_code="MAIN", name="Main", company=self.company,
        )
        product = Product.objects.create(title="Gadget", part_number="G-1", slug="gadget-1")
        self.gadget = ProductVariant.objects.create(
            product=product, sku="G-1-A", name="Gadget A", retail_price=Decimal("90.00"),
        )
        WarehouseStock.objects.create(
            warehouse=self.warehouse, variant=self.variant, bin_location="A-01",
        )
        self.order = Order.objects.create(customer=self.customer, company=self.company)
        self.first = ShipmentGroup.objects.create(
            order=self.order, name="Shipment 1", fulfillment_type="internal",
            warehouse=self.warehouse,
        )
        self.widget_line = OrderLineItem.objects.create(
            order=self.order, shipment_group=self.first, variant=self.variant,
            item_name="Widget A", sku="W-1-A", quantity=Decimal("2"),
            unit_price_ex_tax=Decimal("25.00"),
        )
        self.gadget_line = OrderLineItem.objects.create(
            order=self.order, shipment_group=self.first, variant=self.gadget,
            item_name="Gadget A", sku="G-1-A", quantity=Decimal("3"),
            unit_price_ex_tax=Decimal("90.00"),
        )
        self.freight_line = OrderLineItem.objects.create(
            order=self.order, shipment_group=self.first, item_name="Freight", sku="MISC",
            quantity=Decimal("1"), unit_price_ex_tax=Decimal("40.00"),
        )

    @property
    def order_id(self):
        return str(self.order.pk)


class DefaultSupplierTests(_FulfilmentCase):
    def test_no_listing_says_ask(self):
        result = run(purchasing_core.get_default_supplier(self.client, str(self.gadget.pk)))
        self.assertIsNone(result["default"])
        self.assertEqual(result["candidates"], [])
        self.assertIn("Ask", result["message"])

    def test_candidates_without_a_primary_are_not_a_default(self):
        SupplierProduct.objects.create(variant=self.gadget, supplier=self.acme, cost=Decimal("50"))
        result = run(purchasing_core.get_default_supplier(self.client, str(self.gadget.pk)))
        self.assertIsNone(result["default"])
        self.assertEqual([c["supplier_name"] for c in result["candidates"]], ["Acme Wholesale"])

    def test_set_default_adds_the_listing_and_keeps_one_primary(self):
        SupplierProduct.objects.create(
            variant=self.gadget, supplier=self.acme, cost=Decimal("50"), is_primary=True,
        )
        with self.assertRaises(ERPValidationError):  # new listing needs a cost
            run(purchasing_core.set_default_supplier(
                self.client, variant=str(self.gadget.pk), supplier=str(self.bolt.pk),
            ))
        result = run(purchasing_core.set_default_supplier(
            self.client, variant=str(self.gadget.pk), supplier=str(self.bolt.pk),
            cost="47.50", supplier_sku="BD-77",
        ))
        self.assertEqual(result["default"]["supplier_name"], "Bolt Direct")
        self.assertEqual(result["default"]["cost"], "47.50")
        primaries = SupplierProduct.objects.filter(variant=self.gadget, is_primary=True)
        self.assertEqual([sp.supplier_id for sp in primaries], [self.bolt.pk])

        # Back to the first supplier: its existing listing is reused, no cost needed.
        result = run(purchasing_core.set_default_supplier(
            self.client, variant=str(self.gadget.pk), supplier=str(self.acme.pk),
        ))
        self.assertEqual(result["default"]["supplier_name"], "Acme Wholesale")
        self.assertEqual(SupplierProduct.objects.filter(variant=self.gadget).count(), 2)
        self.assertEqual(SupplierProduct.objects.filter(is_primary=True).count(), 1)

    def test_two_primaries_are_reported_not_resolved(self):
        for supplier in (self.acme, self.bolt):
            SupplierProduct.objects.create(
                variant=self.gadget, supplier=supplier, cost=Decimal("50"), is_primary=True,
            )
        result = run(purchasing_core.get_default_supplier(self.client, str(self.gadget.pk)))
        self.assertIsNone(result["default"])
        self.assertIn("2 suppliers", result["message"])


class SplitShipmentTests(_FulfilmentCase):
    def _split(self):
        return run(orders_core.create_shipment_group(
            self.client, order=self.order_id, fulfillment_type="drop_ship",
            name="Bolt Direct", supplier=str(self.bolt.pk), line_ids=[str(self.gadget_line.pk)],
        ))

    def test_split_moves_the_named_lines(self):
        groups = self._split()
        self.assertEqual([g["name"] for g in groups], ["Shipment 1", "Bolt Direct"])
        self.assertEqual(
            [[line["sku"] for line in g["lines"]] for g in groups],
            [["W-1-A", "MISC"], ["G-1-A"]],
        )
        self.assertEqual(groups[1]["fulfillment_type"], "drop_ship")
        self.assertEqual(groups[1]["supplier"], str(self.bolt.pk))

    def test_a_line_from_another_order_stops_the_split_before_anything_is_written(self):
        other = Order.objects.create(customer=self.customer, company=self.company)
        stray = OrderLineItem.objects.create(
            order=other, item_name="Stray", sku="S", quantity=Decimal("1"),
            unit_price_ex_tax=Decimal("1.00"),
        )
        with self.assertRaises(ERPValidationError):
            run(orders_core.create_shipment_group(
                self.client, order=self.order_id, fulfillment_type="internal",
                line_ids=[str(stray.pk)],
            ))
        self.assertEqual(ShipmentGroup.objects.filter(order=self.order).count(), 1)

    def test_a_shipment_of_another_order_is_refused_by_the_api(self):
        other = Order.objects.create(customer=self.customer, company=self.company)
        foreign = ShipmentGroup.objects.create(order=other, name="Elsewhere")
        with self.assertRaises(ERPValidationError) as caught:
            run(orders_core.update_order_line(
                self.client, str(self.widget_line.pk), {"shipment_group": str(foreign.pk)},
            ))
        self.assertIn("another order", str(caught.exception))

    def test_a_line_on_a_pick_ticket_cannot_move(self):
        run(fulfillment_core.create_pick_ticket(
            self.client, order=self.order_id, shipment_group=str(self.first.pk),
        ))
        with self.assertRaises(ERPValidationError) as caught:
            self._split()
        self.assertIn("pick ticket", str(caught.exception))
        self.gadget_line.refresh_from_db()
        self.assertEqual(self.gadget_line.shipment_group_id, self.first.pk)

    def test_a_move_is_all_or_nothing(self):
        run(fulfillment_core.create_pick_ticket(
            self.client, order=self.order_id, shipment_group=str(self.first.pk),
        ))
        late = OrderLineItem.objects.create(  # added after the ticket: free to move
            order=self.order, shipment_group=self.first, item_name="Late", sku="LATE",
            quantity=Decimal("1"), unit_price_ex_tax=Decimal("5.00"),
        )
        with self.assertRaises(ERPValidationError) as caught:
            run(orders_core.create_shipment_group(
                self.client, order=self.order_id, fulfillment_type="drop_ship", name="Bolt",
                supplier=str(self.bolt.pk), line_ids=[str(late.pk), str(self.gadget_line.pk)],
            ))
        self.assertIn("was added, but no line moved", str(caught.exception))
        late.refresh_from_db()
        self.assertEqual(late.shipment_group_id, self.first.pk)
        empty = ShipmentGroup.objects.get(order=self.order, name="Bolt")
        groups = run(orders_core.move_order_lines(self.client, str(empty.pk), [str(late.pk)]))
        self.assertEqual([line["sku"] for line in groups[1]["lines"]], ["LATE"])

    def test_bad_fulfillment_type_is_refused(self):
        with self.assertRaises(ERPValidationError):
            run(orders_core.create_shipment_group(
                self.client, order=self.order_id, fulfillment_type="teleport",
            ))


class ShipmentPurchaseOrderTests(_FulfilmentCase):
    def setUp(self):
        super().setUp()
        self.drop = ShipmentGroup.objects.create(
            order=self.order, name="Bolt Direct", fulfillment_type="drop_ship",
            supplier=self.bolt, sort_order=1,
        )
        OrderLineItem.objects.filter(pk__in=[self.gadget_line.pk, self.freight_line.pk]).update(
            shipment_group=self.drop
        )
        self.listing = SupplierProduct.objects.create(
            variant=self.gadget, supplier=self.bolt, cost=Decimal("47.50"), supplier_sku="BD-77",
        )

    def _raise(self, **kwargs):
        return run(purchasing_core.create_purchase_order_for_shipment(
            self.client, order=self.order_id, shipment_group=str(self.drop.pk), **kwargs,
        ))

    def test_po_carries_the_catalog_lines_and_names_the_rest(self):
        po = self._raise()
        self.assertEqual(po["status"], "draft")
        self.assertEqual(po["po_type"], "drop_ship")
        self.assertEqual(po["supplier"], str(self.bolt.pk))
        self.assertEqual(po["order"], str(self.order.pk))
        self.assertEqual(po["shipment_group"], str(self.drop.pk))
        (line,) = po["line_items"]
        self.assertEqual(line["order_line_item"], str(self.gadget_line.pk))
        self.assertEqual(line["supplier_product"], str(self.listing.pk))
        self.assertEqual(line["supplier_sku"], "BD-77")
        self.assertEqual(Decimal(line["quantity_ordered"]), Decimal("3"))
        self.assertEqual(Decimal(line["unit_cost"]), Decimal("47.50"))
        self.assertEqual(po["grand_total"], "142.50")
        self.assertEqual([n["sku"] for n in po["lines_not_carried"]], ["MISC"])

    def test_asking_twice_does_not_order_twice(self):
        self._raise()
        with self.assertRaises(ERPValidationError) as caught:
            self._raise()
        self.assertIn("already has purchase order", str(caught.exception))
        self.assertEqual(PurchaseOrder.objects.count(), 1)
        self.assertEqual(POLineItem.objects.count(), 1)

    def test_a_shipment_with_no_supplier_needs_one_named(self):
        self.drop.supplier = None
        self.drop.save()
        with self.assertRaises(ERPValidationError):
            self._raise()
        po = self._raise(supplier=str(self.acme.pk))
        self.assertEqual(po["supplier"], str(self.acme.pk))
        self.assertEqual(Decimal(po["line_items"][0]["unit_cost"]), Decimal("0"))

    def test_the_order_line_cost_wins_over_the_listing(self):
        OrderLineItem.objects.filter(pk=self.gadget_line.pk).update(unit_cost=Decimal("44.00"))
        po = self._raise()
        self.assertEqual(Decimal(po["line_items"][0]["unit_cost"]), Decimal("44"))

    def test_the_listing_cost_is_per_selling_unit(self):
        SupplierProduct.objects.filter(pk=self.listing.pk).update(
            cost=Decimal("120.00"), qty_per_uom=12,
        )
        po = self._raise()
        self.assertEqual(Decimal(po["line_items"][0]["unit_cost"]), Decimal("10"))

    def test_an_empty_draft_left_behind_is_reused(self):
        left = PurchaseOrder.objects.create(
            company=self.company, supplier=self.bolt, order=self.order, shipment_group=self.drop,
        )
        po = self._raise()
        self.assertEqual(po["id"], str(left.pk))
        self.assertEqual(len(po["line_items"]), 1)
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_a_line_already_on_another_po_is_left_off(self):
        other = PurchaseOrder.objects.create(company=self.company, supplier=self.acme)
        POLineItem.objects.create(
            purchase_order=other, variant=self.gadget, item_name="Gadget A",
            quantity_ordered=Decimal("3"), unit_cost=Decimal("1"), order_line_item=self.gadget_line,
        )
        with self.assertRaises(ERPValidationError) as caught:
            self._raise()
        reasons = [n["reason"] for n in caught.exception.details["line_items"]]
        self.assertTrue(any("Already on purchase order" in r for r in reasons), reasons)
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_an_internal_shipment_gets_no_purchase_order(self):
        with self.assertRaises(ERPValidationError) as caught:
            run(purchasing_core.create_purchase_order_for_shipment(
                self.client, order=self.order_id, shipment_group=str(self.first.pk),
                supplier=str(self.bolt.pk),
            ))
        self.assertIn("pick ticket", str(caught.exception))

    def test_a_shipment_of_only_free_text_lines_raises_nothing(self):
        OrderLineItem.objects.filter(pk=self.gadget_line.pk).update(shipment_group=self.first)
        with self.assertRaises(ERPValidationError):
            self._raise()
        self.assertEqual(PurchaseOrder.objects.count(), 0)


class PickTicketTests(_FulfilmentCase):
    def test_ticket_has_the_shipment_lines_and_bins_and_is_made_once(self):
        ticket = run(fulfillment_core.create_pick_ticket(
            self.client, order=self.order_id, shipment_group=str(self.first.pk),
        ))
        self.assertFalse(ticket["already_existed"])
        self.assertTrue(ticket["ticket_number"].startswith("PT-"))
        self.assertEqual(ticket["warehouse"], str(self.warehouse.pk))
        self.assertEqual(ticket["status"], "created")
        self.assertEqual(
            [(line["sku"], Decimal(line["quantity"]), line["bin_location"]) for line in ticket["lines"]],
            [("W-1-A", Decimal("2"), "A-01"), ("G-1-A", Decimal("3"), ""), ("MISC", Decimal("1"), "")],
        )
        again = run(fulfillment_core.create_pick_ticket(
            self.client, order=self.order_id, shipment_group=str(self.first.pk),
        ))
        self.assertTrue(again["already_existed"])
        self.assertEqual(again["id"], ticket["id"])
        self.assertEqual(PickTicket.objects.count(), 1)

    def test_list_by_order_and_step_through_statuses(self):
        ticket = run(fulfillment_core.create_pick_ticket(
            self.client, order=self.order_id, shipment_group=str(self.first.pk),
        ))
        found = run(fulfillment_core.list_pick_tickets(self.client, order=self.order_id))
        self.assertEqual([t["id"] for t in found], [ticket["id"]])
        moved = run(fulfillment_core.update_pick_ticket(
            self.client, ticket["id"], {"notes": "Fragile", "status": "assigned"},
        ))
        self.assertEqual((moved["status"], moved["notes"]), ("assigned", "Fragile"))
        with self.assertRaises(ERPValidationError):  # cannot skip to shipped
            run(fulfillment_core.transition_pick_ticket(self.client, ticket["id"], "shipped"))

    def test_a_drop_ship_shipment_gets_no_pick_ticket(self):
        drop = ShipmentGroup.objects.create(
            order=self.order, name="Drop", fulfillment_type="drop_ship", supplier=self.bolt,
        )
        with self.assertRaises(ERPValidationError) as caught:
            run(fulfillment_core.create_pick_ticket(
                self.client, order=self.order_id, shipment_group=str(drop.pk),
            ))
        self.assertIn("purchase order", str(caught.exception))
        self.assertEqual(PickTicket.objects.count(), 0)

    def test_a_shipment_of_another_order_is_refused(self):
        other = Order.objects.create(customer=self.customer, company=self.company)
        with self.assertRaises(ERPValidationError):
            run(fulfillment_core.create_pick_ticket(
                self.client, order=str(other.pk), shipment_group=str(self.first.pk),
            ))
        self.assertEqual(PickTicket.objects.count(), 0)


class AdapterTests(_FulfilmentCase):
    """The MCP layer returns results, and errors as dicts, never raises."""

    def setUp(self):
        super().setUp()
        self._saved = mcp_adapter._shared_client
        mcp_adapter._shared_client = self.client

    def tearDown(self):
        mcp_adapter._shared_client = self._saved
        super().tearDown()

    def test_tools_are_registered_and_answer(self):
        groups = run(mcp_adapter.list_shipment_groups(order=self.order_id))
        self.assertEqual(len(groups), 1)
        result = run(mcp_adapter.get_default_supplier(variant=str(self.gadget.pk)))
        self.assertIsNone(result["default"])
        error = run(mcp_adapter.create_purchase_order_for_shipment(
            order=self.order_id, shipment_group=str(self.first.pk),
        ))
        self.assertEqual(error["error"], "ERPValidationError")
        self.assertIn("no supplier", error["message"])
