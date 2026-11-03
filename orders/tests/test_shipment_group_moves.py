"""Moving lines between shipment groups on the order and quote editors.

The page moves a line (drag and drop, or the row's shipment menu) by
relocating its row and rewriting the row's hidden ``sg_index``: the position
of the target group in the posted ``sg_{i}_*`` list. These tests post what
the GET page renders, change only that index (and the group list, for a new
group), and check where the line ends up, so they exercise the same round
trip the browser does.

They also pin the two server-side guards:

* a group id from another document is never honoured (no renaming it, no
  pulling lines into it), whether it arrives as ``sg_{i}_id`` or in a line's
  hidden ``shipment_group`` field;
* an order line whose fulfillment has started stays in its group, whatever
  the POST says, and its group is not deleted under it.
"""

from __future__ import annotations

import re
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import Company, UserProfile
from customers.models import Customer
from orders.erp_views import order_line_move_locks
from orders.models import (
    Order,
    OrderLineItem,
    Quote,
    QuoteLineItem,
    ShipmentGroup,
    SubOrder,
)
from orders.tests.test_order_create_requires_line import harvest_inputs


class ShipmentMoveTestBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="ada@example.com", company=self.company,
        )
        self.user = User.objects.create_user(
            username="staff", password="pw", is_staff=True,
        )
        UserProfile.objects.create(user=self.user, company=self.company)
        self.client.force_login(self.user)

    def add_line(self, parent, group, sku, **extra):
        model = OrderLineItem if isinstance(parent, Order) else QuoteLineItem
        parent_field = "order" if isinstance(parent, Order) else "quote"
        return model.objects.create(
            **{parent_field: parent},
            shipment_group=group,
            sku=sku,
            item_name=f"Item {sku}",
            quantity=Decimal("3"),
            unit_price_ex_tax=Decimal("10.00"),
            line_total_ex_tax=Decimal("30.00"),
            **extra,
        )

    def rendered(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode(), harvest_inputs(response.content.decode())

    @staticmethod
    def row_prefix(data, line):
        """The formset prefix of the row that holds ``line`` (e.g. line_items-2)."""
        for name, value in data.items():
            if name.endswith("-id") and value == str(line.pk):
                return name[: -len("-id")]
        raise AssertionError(f"line {line.sku} is not on the page")

    def post(self, url, data):
        data = dict(data, _action="save_continue")
        response = self.client.post(url, data)
        self.assertEqual(
            response.status_code, 302,
            [str(m) for m in response.context["messages"]] if response.context else "",
        )
        return response


class OrderLineMoveTests(ShipmentMoveTestBase):
    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(
            order_number="SO-MOVE-1", customer=self.customer, company=self.company,
        )
        self.g1 = ShipmentGroup.objects.create(order=self.order, name="Shipment 1", sort_order=0)
        self.g2 = ShipmentGroup.objects.create(order=self.order, name="Shipment 2", sort_order=1)
        self.a = self.add_line(self.order, self.g1, "A")
        self.b = self.add_line(self.order, self.g1, "B")
        self.c = self.add_line(self.order, self.g2, "C")
        self.url = reverse("erp-order-edit", args=[self.order.pk])

    def test_line_moved_to_another_group_is_saved_there(self):
        _, data = self.rendered(self.url)
        data[f"{self.row_prefix(data, self.a)}-sg_index"] = "1"
        self.post(self.url, data)
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.c.refresh_from_db()
        self.assertEqual(self.a.shipment_group, self.g2)
        self.assertEqual(self.b.shipment_group, self.g1)
        self.assertEqual(self.c.shipment_group, self.g2)
        self.assertEqual(self.order.shipment_groups.count(), 2)

    def test_line_moved_into_a_new_group(self):
        # Split Ship adds sg_2_* client-side; the line is dropped into it.
        _, data = self.rendered(self.url)
        data.update({
            "sg_count": "3", "sg_2_id": "", "sg_2_name": "Shipment 3",
            "sg_2_fulfillment_type": "drop_ship", "sg_2_supplier": "",
            "sg_2_warehouse": "",
        })
        data[f"{self.row_prefix(data, self.b)}-sg_index"] = "2"
        self.post(self.url, data)
        new_group = self.order.shipment_groups.get(name="Shipment 3")
        self.b.refresh_from_db()
        self.assertEqual(self.b.shipment_group, new_group)
        self.assertEqual(new_group.sort_order, 2)

    def test_emptied_group_is_kept_and_can_then_be_deleted(self):
        # Moving the only line out of a group leaves it empty, as before.
        _, data = self.rendered(self.url)
        data[f"{self.row_prefix(data, self.c)}-sg_index"] = "0"
        self.post(self.url, data)
        self.c.refresh_from_db()
        self.assertEqual(self.c.shipment_group, self.g1)
        self.assertTrue(ShipmentGroup.objects.filter(pk=self.g2.pk).exists())
        self.assertFalse(self.g2.order_line_items.exists())

        # Deleting the empty group (dropped from the posted list) removes it.
        _, data = self.rendered(self.url)
        data["sg_count"] = "1"
        for key in [k for k in data if k.startswith("sg_1_")]:
            del data[key]
        self.post(self.url, data)
        self.assertFalse(ShipmentGroup.objects.filter(pk=self.g2.pk).exists())
        self.assertEqual(
            set(self.order.line_items.values_list("shipment_group", flat=True)),
            {self.g1.pk},
        )

    def test_malformed_sg_index_falls_back_to_the_first_group(self):
        _, data = self.rendered(self.url)
        data["sg_count"] = "2"
        data[f"{self.row_prefix(data, self.c)}-sg_index"] = "99"
        self.post(self.url, data)
        self.c.refresh_from_db()
        self.assertEqual(self.c.shipment_group, self.g1)

    def test_only_the_chosen_fulfillment_controls_show(self):
        # .d-flex is display:flex !important, so the hidden set must use
        # d-none; an inline display:none on it showed both sets.
        ShipmentGroup.objects.filter(pk=self.g2.pk).update(fulfillment_type="internal")
        html, _ = self.rendered(self.url)
        self.assertEqual(html.count('class="sg-internal-controls d-flex flex-wrap align-items-center gap-2 d-none"'), 1)
        self.assertEqual(html.count('class="sg-dropship-controls d-flex flex-wrap align-items-center gap-2 d-none"'), 1)
        self.assertNotRegex(html, r'class="sg-(internal|dropship)-controls[^"]*"\s+style=')

    def test_page_offers_moves_for_unlocked_lines(self):
        html, _ = self.rendered(self.url)
        self.assertEqual(html.count('class="form-select form-select-sm li-move-select"'), 4)  # 3 lines + blank row
        self.assertEqual(html.count('<i class="fas fa-grip-vertical li-drag-handle'), 4)
        self.assertNotIn("data-move-lock=", html)
        self.assertEqual(html.count('class="sg-empty-row"'), 2)


class ForeignGroupTests(ShipmentMoveTestBase):
    """A group id from another document must never be honoured."""

    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(
            order_number="SO-MOVE-2", customer=self.customer, company=self.company,
        )
        self.g1 = ShipmentGroup.objects.create(order=self.order, name="Shipment 1", sort_order=0)
        self.g2 = ShipmentGroup.objects.create(order=self.order, name="Shipment 2", sort_order=1)
        self.a = self.add_line(self.order, self.g1, "A")
        self.url = reverse("erp-order-edit", args=[self.order.pk])

        other = Order.objects.create(
            order_number="SO-OTHER", customer=self.customer, company=self.company,
        )
        self.foreign = ShipmentGroup.objects.create(order=other, name="Theirs", sort_order=5)
        self.foreign_line = self.add_line(other, self.foreign, "X")
        quote = Quote.objects.create(customer=self.customer, company=self.company)
        self.foreign_quote_group = ShipmentGroup.objects.create(
            quote=quote, name="Quote group", sort_order=0,
        )

    def assert_foreign_untouched(self):
        self.foreign.refresh_from_db()
        self.assertEqual(self.foreign.name, "Theirs")
        self.assertEqual(self.foreign.sort_order, 5)
        self.assertEqual(
            list(self.foreign.order_line_items.values_list("pk", flat=True)),
            [self.foreign_line.pk],
        )

    def test_foreign_group_id_in_the_group_list_is_treated_as_new(self):
        _, data = self.rendered(self.url)
        data["sg_1_id"] = str(self.foreign.pk)
        data["sg_1_name"] = "Hijacked"
        data[f"{self.row_prefix(data, self.a)}-sg_index"] = "1"
        self.post(self.url, data)

        self.assert_foreign_untouched()
        self.a.refresh_from_db()
        self.assertEqual(self.a.shipment_group.order, self.order)
        self.assertEqual(self.a.shipment_group.name, "Hijacked")
        self.assertNotEqual(self.a.shipment_group_id, self.foreign.pk)
        # The real second group was dropped from the list, so it is deleted.
        self.assertFalse(ShipmentGroup.objects.filter(pk=self.g2.pk).exists())

    def test_quote_group_id_on_an_order_is_refused(self):
        _, data = self.rendered(self.url)
        data["sg_0_id"] = str(self.foreign_quote_group.pk)
        self.post(self.url, data)
        self.foreign_quote_group.refresh_from_db()
        self.assertIsNone(self.foreign_quote_group.order_id)
        self.assertEqual(self.foreign_quote_group.name, "Quote group")
        self.a.refresh_from_db()
        self.assertEqual(self.a.shipment_group.order, self.order)

    def test_garbage_group_id_does_not_500(self):
        _, data = self.rendered(self.url)
        data["sg_0_id"] = "not-a-uuid"
        data["sg_count"] = "two"
        self.post(self.url, data)
        self.a.refresh_from_db()
        self.assertEqual(self.a.shipment_group.order, self.order)

    def test_foreign_group_in_a_lines_hidden_field_is_ignored(self):
        _, data = self.rendered(self.url)
        prefix = self.row_prefix(data, self.a)
        data[f"{prefix}-shipment_group"] = str(self.foreign.pk)
        data[f"{prefix}-item_name"] = "Item A, edited"  # force the row to save
        self.post(self.url, data)
        self.a.refresh_from_db()
        self.assertEqual(self.a.item_name, "Item A, edited")
        self.assertEqual(self.a.shipment_group, self.g1)
        self.assert_foreign_untouched()

    def test_line_form_drops_a_foreign_group(self):
        from orders.forms import OrderLineItemForm

        form = OrderLineItemForm(
            data={
                "sku": "A", "item_name": "Item A", "quantity": "1",
                "unit_price_ex_tax": "1", "shipment_group": str(self.foreign.pk),
            },
            instance=self.a,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data["shipment_group"])

        own = OrderLineItemForm(
            data={
                "sku": "A", "item_name": "Item A", "quantity": "1",
                "unit_price_ex_tax": "1", "shipment_group": str(self.g2.pk),
            },
            instance=self.a,
        )
        self.assertTrue(own.is_valid(), own.errors)
        self.assertEqual(own.cleaned_data["shipment_group"], self.g2)


class LockedLineTests(ShipmentMoveTestBase):
    """An order line whose fulfillment has started cannot change shipment."""

    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(
            order_number="SO-MOVE-3", customer=self.customer, company=self.company,
        )
        self.g1 = ShipmentGroup.objects.create(order=self.order, name="Shipment 1", sort_order=0)
        self.g2 = ShipmentGroup.objects.create(order=self.order, name="Shipment 2", sort_order=1)
        self.shipped = self.add_line(self.order, self.g1, "S", quantity_shipped=Decimal("2"))
        self.free = self.add_line(self.order, self.g1, "F")
        self.url = reverse("erp-order-edit", args=[self.order.pk])

    def test_page_shows_the_lock_and_why(self):
        html, _ = self.rendered(self.url)
        self.assertIn('data-move-lock="Already shipped (2 of 3)."', html)
        self.assertIn("fa-lock", html)
        self.assertIn("Locked to this shipment. Already shipped (2 of 3).", html)
        # Only the free line and the blank row get a handle and a menu.
        self.assertEqual(html.count('<i class="fas fa-grip-vertical li-drag-handle'), 2)
        self.assertEqual(html.count('class="form-select form-select-sm li-move-select"'), 2)

    def test_posted_move_of_a_locked_line_is_ignored(self):
        _, data = self.rendered(self.url)
        data[f"{self.row_prefix(data, self.shipped)}-sg_index"] = "1"
        data[f"{self.row_prefix(data, self.free)}-sg_index"] = "1"
        self.post(self.url, data)
        self.shipped.refresh_from_db()
        self.free.refresh_from_db()
        self.assertEqual(self.shipped.shipment_group, self.g1)
        self.assertEqual(self.free.shipment_group, self.g2)

    def test_locked_line_ignores_its_hidden_group_field_too(self):
        _, data = self.rendered(self.url)
        prefix = self.row_prefix(data, self.shipped)
        data[f"{prefix}-shipment_group"] = str(self.g2.pk)
        data[f"{prefix}-sg_index"] = "1"
        data[f"{prefix}-item_name"] = "Shipped item, renamed"
        self.post(self.url, data)
        self.shipped.refresh_from_db()
        self.assertEqual(self.shipped.item_name, "Shipped item, renamed")
        self.assertEqual(self.shipped.shipment_group, self.g1)

    def test_group_holding_a_locked_line_is_not_deleted(self):
        # A POST that drops group 1 from the list (only group 2 left).
        _, data = self.rendered(self.url)
        data["sg_count"] = "1"
        data["sg_0_id"] = str(self.g2.pk)
        data["sg_0_name"] = "Shipment 2"
        for key in [k for k in data if k.startswith("sg_1_")]:
            del data[key]
        self.post(self.url, data)
        self.assertTrue(ShipmentGroup.objects.filter(pk=self.g1.pk).exists())
        self.shipped.refresh_from_db()
        self.free.refresh_from_db()
        self.assertEqual(self.shipped.shipment_group, self.g1)
        self.assertEqual(self.free.shipment_group, self.g2)

    def test_quote_lines_are_never_locked(self):
        quote = Quote.objects.create(customer=self.customer, company=self.company)
        group = ShipmentGroup.objects.create(quote=quote, name="Shipment 1")
        self.add_line(quote, group, "Q")
        html, _ = self.rendered(reverse("erp-quote-edit", args=[quote.pk]))
        self.assertNotIn("data-move-lock=", html)


class MoveLockReasonTests(ShipmentMoveTestBase):
    """order_line_move_locks: which lines lock, and the reason shown."""

    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(
            order_number="SO-MOVE-4", customer=self.customer, company=self.company,
        )
        self.group = ShipmentGroup.objects.create(order=self.order, name="Shipment 1")

    def test_unstarted_lines_are_free(self):
        line = self.add_line(self.order, self.group, "FREE")
        self.assertEqual(order_line_move_locks(self.order), {})
        self.assertEqual(order_line_move_locks(None), {})
        self.assertEqual(order_line_move_locks(Order(company=self.company)), {})
        self.assertNotIn(line.pk, order_line_move_locks(self.order))

    def test_fulfillment_status_locks(self):
        line = self.add_line(
            self.order, self.group, "PS",
            fulfillment_status=OrderLineItem.FulfillmentStatus.PICK_ASSIGNED,
        )
        lock = order_line_move_locks(self.order)[line.pk]
        self.assertEqual(lock["reason"], "Fulfillment has started (Pick Assigned).")
        self.assertEqual(lock["group_id"], self.group.pk)

    def test_shipped_suborder_locks(self):
        sub = SubOrder.objects.create(
            order=self.order, sub_order_number="SO-MOVE-4-1",
            status=SubOrder.Status.SHIPPED,
        )
        line = self.add_line(self.order, self.group, "SUB", sub_order=sub)
        self.assertEqual(
            order_line_move_locks(self.order)[line.pk]["reason"],
            "Its shipment SO-MOVE-4-1 is shipped.",
        )

    def test_pending_suborder_does_not_lock(self):
        sub = SubOrder.objects.create(order=self.order, sub_order_number="SO-MOVE-4-2")
        line = self.add_line(self.order, self.group, "SUBP", sub_order=sub)
        self.assertNotIn(line.pk, order_line_move_locks(self.order))

    def test_purchase_order_line_locks_unless_cancelled(self):
        from products.models import Product, ProductVariant
        from purchasing.models import POLineItem, PurchaseOrder, Supplier

        line = self.add_line(self.order, self.group, "PO")
        supplier = Supplier.objects.create(company_name="Supply Ltd", company=self.company)
        po = PurchaseOrder.objects.create(supplier=supplier, company=self.company)
        variant = ProductVariant.objects.create(
            product=Product.objects.create(title="Widget", company=self.company), sku="W-1",
        )
        POLineItem.objects.create(
            purchase_order=po, variant=variant, item_name="Widget",
            quantity_ordered=Decimal("3"), unit_cost=Decimal("4.00"), order_line_item=line,
        )
        reason = order_line_move_locks(self.order)[line.pk]["reason"]
        self.assertIn(f"purchase order {po.po_number}", reason)

        PurchaseOrder.objects.filter(pk=po.pk).update(status=PurchaseOrder.Status.CANCELLED)
        self.assertNotIn(line.pk, order_line_move_locks(self.order))

    def test_pick_ticket_line_locks(self):
        from fulfillment.models import PickTicket, PickTicketLine

        line = self.add_line(self.order, self.group, "PT")
        ticket = PickTicket.objects.create(order=self.order, shipment_group=self.group)
        PickTicketLine.objects.create(
            pick_ticket=ticket, order_line_item=line, quantity=Decimal("3"),
        )
        reason = order_line_move_locks(self.order)[line.pk]["reason"]
        self.assertIn(f"pick ticket {ticket.ticket_number}", reason)


class QuoteLineMoveTests(ShipmentMoveTestBase):
    def setUp(self):
        super().setUp()
        self.quote = Quote.objects.create(customer=self.customer, company=self.company)
        self.g1 = ShipmentGroup.objects.create(quote=self.quote, name="Shipment 1", sort_order=0)
        self.g2 = ShipmentGroup.objects.create(quote=self.quote, name="Shipment 2", sort_order=1)
        self.a = self.add_line(self.quote, self.g1, "QA")
        self.b = self.add_line(self.quote, self.g2, "QB")
        self.url = reverse("erp-quote-edit", args=[self.quote.pk])

    def test_line_moved_to_another_group_is_saved_there(self):
        _, data = self.rendered(self.url)
        data[f"{self.row_prefix(data, self.a)}-sg_index"] = "1"
        self.post(self.url, data)
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual(self.a.shipment_group, self.g2)
        self.assertEqual(self.b.shipment_group, self.g2)
        self.assertTrue(ShipmentGroup.objects.filter(pk=self.g1.pk).exists())

    def test_line_moved_into_a_new_group(self):
        _, data = self.rendered(self.url)
        data.update({
            "sg_count": "3", "sg_2_id": "", "sg_2_name": "Shipment 3",
            "sg_2_fulfillment_type": "internal",
        })
        data[f"{self.row_prefix(data, self.b)}-sg_index"] = "2"
        self.post(self.url, data)
        self.b.refresh_from_db()
        self.assertEqual(self.b.shipment_group.name, "Shipment 3")
        self.assertEqual(self.b.shipment_group.quote, self.quote)

    def test_order_group_id_on_a_quote_is_refused(self):
        other = Order.objects.create(
            order_number="SO-MOVE-5", customer=self.customer, company=self.company,
        )
        foreign = ShipmentGroup.objects.create(order=other, name="Theirs")
        _, data = self.rendered(self.url)
        data["sg_1_id"] = str(foreign.pk)
        data[f"{self.row_prefix(data, self.a)}-sg_index"] = "1"
        self.post(self.url, data)
        foreign.refresh_from_db()
        self.assertEqual(foreign.name, "Theirs")
        self.assertIsNone(foreign.quote_id)
        self.a.refresh_from_db()
        self.assertEqual(self.a.shipment_group.quote, self.quote)
        self.assertNotEqual(self.a.shipment_group_id, foreign.pk)


def rows_in_order(data, prefixes):
    """``data`` with the rows ``prefixes`` posted in that order, as a browser
    posts them after the rows were dragged into it (fields go in document
    order). Every other field keeps its place, ahead of the rows."""
    def owner(name):
        return next((p for p in prefixes if name.startswith(p + "-")), None)

    out = {k: v for k, v in data.items() if owner(k) is None}
    for prefix in prefixes:
        out.update({k: v for k, v in data.items() if owner(k) == prefix})
    return out


class LineOrderTests(ShipmentMoveTestBase):
    """Line order within a shipment is the order of the rows on screen."""

    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(
            order_number="SO-ORDER-1", customer=self.customer, company=self.company,
        )
        self.g1 = ShipmentGroup.objects.create(order=self.order, name="Shipment 1", sort_order=0)
        self.g2 = ShipmentGroup.objects.create(order=self.order, name="Shipment 2", sort_order=1)
        self.a = self.add_line(self.order, self.g1, "A")
        self.b = self.add_line(self.order, self.g1, "B")
        self.c = self.add_line(self.order, self.g1, "C")
        self.d = self.add_line(self.order, self.g2, "D")
        self.url = reverse("erp-order-edit", args=[self.order.pk])

    def skus_on_page(self):
        """The SKUs of the editor's rows, top to bottom."""
        html, _ = self.rendered(self.url)
        return [sku for sku in re.findall(r'name="line_items-\d+-sku"[^>]*value="([^"]*)"', html) if sku]

    def test_lines_reordered_within_a_shipment_keep_that_order(self):
        _, data = self.rendered(self.url)
        p = {line.sku: self.row_prefix(data, line) for line in (self.a, self.b, self.c, self.d)}
        self.post(self.url, rows_in_order(data, [p["C"], p["A"], p["B"], p["D"]]))
        self.assertEqual(self.skus_on_page(), ["C", "A", "B", "D"])
        self.c.refresh_from_db()
        self.a.refresh_from_db()
        self.assertLess(self.c.sort_order, self.a.sort_order)

    def test_a_line_dropped_between_lines_of_another_shipment(self):
        _, data = self.rendered(self.url)
        p = {line.sku: self.row_prefix(data, line) for line in (self.a, self.b, self.c, self.d)}
        data[f"{p['A']}-sg_index"] = "1"
        self.post(self.url, rows_in_order(data, [p["B"], p["C"], p["A"], p["D"]]))
        self.a.refresh_from_db()
        self.assertEqual(self.a.shipment_group, self.g2)
        self.assertEqual(
            list(self.g2.order_line_items.order_by("sort_order").values_list("sku", flat=True)),
            ["A", "D"],
        )
        self.assertEqual(self.skus_on_page(), ["B", "C", "A", "D"])

    def test_lines_never_reordered_show_in_the_order_they_were_added(self):
        # Every line above has sort_order 0 (saved before the editor kept
        # order): created_at decides, the same way on every load.
        self.assertEqual(
            set(OrderLineItem.objects.filter(order=self.order).values_list("sort_order", flat=True)), {0},
        )
        self.assertEqual(self.skus_on_page(), ["A", "B", "C", "D"])

    def test_a_locked_line_keeps_its_shipment_but_others_move_around_it(self):
        OrderLineItem.objects.filter(pk=self.b.pk).update(
            fulfillment_status=OrderLineItem.FulfillmentStatus.IN_PROGRESS,
        )
        _, data = self.rendered(self.url)
        p = {line.sku: self.row_prefix(data, line) for line in (self.a, self.b, self.c, self.d)}
        self.post(self.url, rows_in_order(data, [p["C"], p["B"], p["A"], p["D"]]))
        self.b.refresh_from_db()
        self.assertEqual(self.b.shipment_group, self.g1)
        self.assertEqual(self.skus_on_page(), ["C", "B", "A", "D"])

    def test_quote_lines_keep_their_order_too(self):
        quote = Quote.objects.create(customer=self.customer, company=self.company)
        group = ShipmentGroup.objects.create(quote=quote, name="Shipment 1", sort_order=0)
        first = self.add_line(quote, group, "QA")
        second = self.add_line(quote, group, "QB")
        url = reverse("erp-quote-edit", args=[quote.pk])
        _, data = self.rendered(url)
        self.post(url, rows_in_order(data, [self.row_prefix(data, second), self.row_prefix(data, first)]))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertLess(second.sort_order, first.sort_order)


class PostInputTests(ShipmentMoveTestBase):
    """What a crafted shipment-group POST can and cannot do (review 2026-10-17)."""

    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(
            order_number="SO-MOVE-7", customer=self.customer, company=self.company,
        )
        self.g1 = ShipmentGroup.objects.create(order=self.order, name="Shipment 1", sort_order=0)
        self.a = self.add_line(self.order, self.g1, "A")
        self.url = reverse("erp-order-edit", args=[self.order.pk])

    def test_a_huge_sg_count_is_clamped(self):
        from orders.erp_views import MAX_SHIPMENT_GROUPS

        _, data = self.rendered(self.url)
        data["sg_count"] = "100000"
        self.post(self.url, data)
        self.assertLessEqual(self.order.shipment_groups.count(), MAX_SHIPMENT_GROUPS)

    def test_another_tenants_warehouse_or_supplier_is_never_attached(self):
        from inventory.models import Warehouse
        from purchasing.models import Supplier

        other = Company.objects.create(name="Other Co")
        theirs = Warehouse.objects.create(company=other, name="Theirs", warehouse_code="THEIRS")
        their_supplier = Supplier.objects.create(company=other, company_name="Their Supplier")
        _, data = self.rendered(self.url)
        data["sg_0_warehouse"] = str(theirs.pk)
        data["sg_0_supplier"] = str(their_supplier.pk)
        self.post(self.url, data)
        self.g1.refresh_from_db()
        self.assertIsNone(self.g1.warehouse_id)
        self.assertIsNone(self.g1.supplier_id)

    def test_own_warehouse_is_attached(self):
        from inventory.models import Warehouse

        mine = Warehouse.objects.create(company=self.company, name="Mine", warehouse_code="MINE")
        _, data = self.rendered(self.url)
        data["sg_0_warehouse"] = str(mine.pk)
        self.post(self.url, data)
        self.g1.refresh_from_db()
        self.assertEqual(self.g1.warehouse_id, mine.pk)

    def test_garbage_warehouse_and_supplier_ids_do_not_500(self):
        _, data = self.rendered(self.url)
        data["sg_0_warehouse"] = "not-a-uuid"
        data["sg_0_supplier"] = "also-not"
        self.post(self.url, data)
        self.g1.refresh_from_db()
        self.assertIsNone(self.g1.warehouse_id)


class LockedLineDeletionTests(ShipmentMoveTestBase):
    """A locked line cannot be removed either, and a save never detaches a
    line from its sub-order."""

    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(
            order_number="SO-MOVE-8", customer=self.customer, company=self.company,
        )
        self.g1 = ShipmentGroup.objects.create(order=self.order, name="Shipment 1", sort_order=0)
        self.g2 = ShipmentGroup.objects.create(order=self.order, name="Shipment 2", sort_order=1)
        self.shipped = self.add_line(self.order, self.g1, "S", quantity_shipped=Decimal("2"))
        self.free = self.add_line(self.order, self.g1, "F")
        self.url = reverse("erp-order-edit", args=[self.order.pk])

    def test_page_hides_remove_on_a_locked_row(self):
        html, _ = self.rendered(self.url)
        self.assertIn('btn-delete-row d-none" title="Remove" disabled', html)
        # The free line and the blank row keep a live Remove button.
        self.assertEqual(html.count('class="btn btn-link text-danger p-0 btn-delete-row" title="Remove"'), 2)

    def test_deleting_a_locked_line_is_refused(self):
        _, data = self.rendered(self.url)
        data[f"{self.row_prefix(data, self.shipped)}-DELETE"] = "on"
        data["_action"] = "save_continue"
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(OrderLineItem.objects.filter(pk=self.shipped.pk).exists())
        self.assertTrue(any("cannot be removed" in str(m) and "Already shipped" in str(m)
                            for m in response.context["messages"]))

    def test_deleting_a_free_line_still_works(self):
        _, data = self.rendered(self.url)
        data[f"{self.row_prefix(data, self.free)}-DELETE"] = "on"
        self.post(self.url, data)
        self.assertFalse(OrderLineItem.objects.filter(pk=self.free.pk).exists())
        self.assertTrue(OrderLineItem.objects.filter(pk=self.shipped.pk).exists())

    def test_a_groupless_locked_line_takes_the_posted_group(self):
        legacy = self.add_line(self.order, None, "L", quantity_shipped=Decimal("1"))
        self.assertIsNone(order_line_move_locks(self.order)[legacy.pk]["group_id"])
        _, data = self.rendered(self.url)
        data[f"{self.row_prefix(data, legacy)}-sg_index"] = "1"
        self.post(self.url, data)
        legacy.refresh_from_db()
        self.assertEqual(legacy.shipment_group, self.g2)

    def test_saving_the_editor_keeps_each_lines_sub_order(self):
        # sub_order was in the line form's fields without being rendered, so
        # every save posted it blank and detached the line (review 2026-10-17).
        sub = SubOrder.objects.create(order=self.order, sub_order_number="SO-MOVE-8-1",
                                      status=SubOrder.Status.PICKED)
        OrderLineItem.objects.filter(pk=self.free.pk).update(sub_order=sub)
        _, data = self.rendered(self.url)
        self.assertNotIn(f"{self.row_prefix(data, self.free)}-sub_order", data)
        self.post(self.url, data)
        self.free.refresh_from_db()
        self.assertEqual(self.free.sub_order_id, sub.pk)
        # And so the sub-order lock still holds on the next edit.
        self.assertIn("is picked", order_line_move_locks(self.order)[self.free.pk]["reason"])
