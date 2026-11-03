"""Quote → Sales Order conversion.

The conversion must be inert: it creates the order and stops. Nothing it
does may email a customer, move stock, or advance fulfillment. It must also
be safe to invoke twice, because the trigger is a button.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import Company, UserProfile
from customers.models import Customer, CustomerAddress
from orders.convert import convert_quote_to_order
from orders.models import Order, Quote, QuoteLineItem, ShipmentGroup


class QuoteToOrderTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="convert@example.com", company=self.company,
        )
        self.address = CustomerAddress.objects.create(
            customer=self.customer, address_line_1="1 Analytical Way",
            city="Acton", state_province="MA", postal_code="01720",
        )
        self.user = UserProfile.objects.create(
            company=self.company,
        )
        self.quote = Quote.objects.create(
            customer=self.customer,
            company=self.company,
            billing_address=self.address,
            shipping_address=self.address,
            customer_po_number="PO-9912",
            payment_terms_proposed="Net 30",
            shipping_terms_proposed="FOB Destination",
            special_conditions="Crated delivery only.",
            customer_notes="Thanks for your business.",
            internal_notes="Watch the lead time.",
            shipping_cost_ex_tax=Decimal("250.00"),
        )

    def _line(self, **kwargs):
        defaults = dict(
            quote=self.quote,
            variant=None,
            sku="MISC",
            item_name="Custom fabrication",
            quantity=Decimal("2"),
            unit_price_ex_tax=Decimal("100.0000"),
            line_total_ex_tax=Decimal("200.00"),
        )
        defaults.update(kwargs)
        return QuoteLineItem.objects.create(**defaults)

    # ── core behaviour ────────────────────────────────────

    def test_creates_an_order_with_a_minted_number(self):
        self._line()
        order, created = convert_quote_to_order(self.quote, user=self.user)
        self.assertTrue(created)
        self.assertTrue(order.order_number.startswith("SO-"))
        self.assertEqual(order.customer, self.customer)
        self.assertEqual(order.company, self.company)

    def test_lands_in_pending_so_nothing_downstream_fires(self):
        self._line()
        order, _ = convert_quote_to_order(self.quote)
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(order.payment_status, Order.PaymentStatus.UNPAID)
        self.assertEqual(
            order.fulfillment_status, Order.FulfillmentStatus.UNFULFILLED
        )
        # The confirmation email is triggered by a view-level status
        # transition, never by creation. Converting must not have sent one.
        self.assertIsNone(order.confirmation_sent_at)
        self.assertIsNone(order.booked_at)

    def test_copies_header_fields_including_the_renamed_ones(self):
        self._line()
        order, _ = convert_quote_to_order(self.quote)
        self.assertEqual(order.billing_address, self.address)
        self.assertEqual(order.shipping_address, self.address)
        self.assertEqual(order.customer_po_number, "PO-9912")
        self.assertEqual(order.include_standard_terms, self.quote.include_standard_terms)
        # Renamed across the two models.
        self.assertEqual(order.customer_message, "Thanks for your business.")
        self.assertEqual(order.payment_terms, "Net 30")
        self.assertEqual(order.terms_text, "Crated delivery only.")
        self.assertEqual(order.internal_notes, "Watch the lead time.")

    def test_shipping_terms_are_stashed_rather_than_dropped(self):
        # Order has no shipping_terms column, but it is a negotiated term.
        self._line()
        order, _ = convert_quote_to_order(self.quote)
        self.assertEqual(
            order.metadata.get("shipping_terms_proposed"), "FOB Destination"
        )

    def test_links_are_set_in_both_directions(self):
        self._line()
        order, _ = convert_quote_to_order(self.quote, user=self.user)
        self.quote.refresh_from_db()
        self.assertEqual(order.quote, self.quote)
        self.assertEqual(self.quote.converted_to_order, order)
        self.assertEqual(self.quote.converted_by, self.user)
        self.assertIsNotNone(self.quote.converted_at)

    def test_quote_is_marked_accepted_and_stays_editable(self):
        self._line()
        convert_quote_to_order(self.quote)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.ACCEPTED)
        # The deal is won on the engagement (CRM Phase 3); the old key reads it.
        self.assertEqual(self.quote.engagement.status, "won")
        with self.assertWarns(DeprecationWarning):
            self.assertEqual(self.quote.sales_stage, Quote.SalesStage.CLOSED_WON)
        self.assertIsNotNone(self.quote.accepted_at)

    # ── idempotency ───────────────────────────────────────

    def test_converting_twice_returns_the_same_order(self):
        self._line()
        first, created_first = convert_quote_to_order(self.quote)
        self.quote.refresh_from_db()
        second, created_second = convert_quote_to_order(self.quote)

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Order.objects.count(), 1)

    # ── line items ────────────────────────────────────────

    def test_copies_line_items_including_variant_less_ones(self):
        self._line(sku="MISC", item_name="Custom fabrication")
        self._line(sku="SOL-2", item_name="Second line", sort_order=1)
        order, _ = convert_quote_to_order(self.quote)

        lines = list(order.line_items.order_by("sort_order", "sku"))
        self.assertEqual(len(lines), 2)
        self.assertEqual({line.sku for line in lines}, {"MISC", "SOL-2"})
        # A freeform quote line stays freeform on the order.
        self.assertIsNone(lines[0].variant_id)

    def test_deselected_lines_are_left_behind(self):
        # customer_selected records what the customer actually took. Billing
        # them for a declined optional extra is the failure mode here.
        self._line(sku="TAKEN")
        self._line(sku="DECLINED", is_optional=True, customer_selected=False)
        order, _ = convert_quote_to_order(self.quote)

        self.assertEqual(
            [line.sku for line in order.line_items.all()], ["TAKEN"]
        )

    def test_optional_lines_the_customer_kept_do_convert(self):
        self._line(sku="EXTRA", is_optional=True, customer_selected=True)
        order, _ = convert_quote_to_order(self.quote)
        self.assertEqual([line.sku for line in order.line_items.all()], ["EXTRA"])

    def test_line_fulfillment_state_starts_clean(self):
        self._line()
        order, _ = convert_quote_to_order(self.quote)
        line = order.line_items.get()
        self.assertIsNone(line.sub_order_id)
        self.assertIsNone(line.purchase_order_line_id)
        self.assertIsNone(line.pick_ticket_line_id)
        self.assertEqual(line.quantity_shipped, Decimal("0.000"))
        self.assertEqual(line.quantity_returned, Decimal("0.000"))

    def test_estimated_ship_date_becomes_requested_ship_date(self):
        self._line(estimated_ship_date=date(2026, 9, 15))
        order, _ = convert_quote_to_order(self.quote)
        self.assertEqual(
            order.line_items.get().requested_ship_date, date(2026, 9, 15)
        )

    # ── totals ────────────────────────────────────────────

    def test_totals_include_shipping(self):
        # recalculate_totals() reads shipping_cost_ex_tax off the order row,
        # so the order must carry it before any line is created.
        self._line(quantity=Decimal("2"), unit_price_ex_tax=Decimal("100.0000"))
        order, _ = convert_quote_to_order(self.quote)
        order.refresh_from_db()
        self.assertEqual(order.shipping_cost_ex_tax, Decimal("250.00"))
        self.assertEqual(order.subtotal_ex_tax, Decimal("200.00"))
        self.assertEqual(order.grand_total, Decimal("450.00"))

    # ── shipment groups ───────────────────────────────────

    def test_shipment_groups_are_cloned_not_stolen(self):
        group = ShipmentGroup.objects.create(
            quote=self.quote, name="Shipment 1", sort_order=0,
        )
        self._line(shipment_group=group)
        order, _ = convert_quote_to_order(self.quote)

        group.refresh_from_db()
        # The quote keeps its own group ...
        self.assertEqual(group.quote_id, self.quote.pk)
        self.assertIsNone(group.order_id)
        # ... and the order gets a distinct copy.
        new_group = order.shipment_groups.get()
        self.assertNotEqual(new_group.pk, group.pk)
        self.assertEqual(new_group.name, "Shipment 1")
        self.assertIsNone(new_group.quote_id)

    def test_lines_point_at_the_orders_own_group(self):
        group = ShipmentGroup.objects.create(quote=self.quote, name="Crate A")
        self._line(shipment_group=group)
        order, _ = convert_quote_to_order(self.quote)

        line = order.line_items.get()
        new_group = order.shipment_groups.get()
        self.assertEqual(line.shipment_group_id, new_group.pk)
        self.assertNotEqual(line.shipment_group_id, group.pk)

    def test_multiple_groups_keep_their_line_assignments(self):
        a = ShipmentGroup.objects.create(quote=self.quote, name="A", sort_order=0)
        b = ShipmentGroup.objects.create(quote=self.quote, name="B", sort_order=1)
        self._line(sku="IN-A", shipment_group=a)
        self._line(sku="IN-B", shipment_group=b, sort_order=1)

        order, _ = convert_quote_to_order(self.quote)
        by_sku = {line.sku: line for line in order.line_items.all()}
        self.assertNotEqual(
            by_sku["IN-A"].shipment_group_id, by_sku["IN-B"].shipment_group_id
        )
        self.assertEqual(by_sku["IN-A"].shipment_group.name, "A")
        self.assertEqual(by_sku["IN-B"].shipment_group.name, "B")

    def test_no_suborders_are_created(self):
        # SubOrder is created later by fulfillment staff, and its number has
        # no auto-mint, so conversion must not invent one.
        self._line()
        order, _ = convert_quote_to_order(self.quote)
        self.assertEqual(order.sub_orders.count(), 0)


class ConvertToOrderViewTests(TestCase):
    """The button path, not just the service function.

    Covers the thing a unit test of convert_quote_to_order() cannot: that
    the quote is SAVED before it converts, so edits made in the same visit
    reach the order instead of being silently dropped.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="view@example.com", company=self.company,
        )
        self.user = User.objects.create_user(
            username="staff", password="pw", is_staff=True,
        )
        UserProfile.objects.create(company=self.company)
        self.client.force_login(self.user)
        self.quote = Quote.objects.create(
            customer=self.customer, company=self.company,
        )
        QuoteLineItem.objects.create(
            quote=self.quote, variant=None, sku="MISC", item_name="Fabrication",
            quantity=Decimal("1"), unit_price_ex_tax=Decimal("500.0000"),
            line_total_ex_tax=Decimal("500.00"),
        )

    def _post(self, **extra):
        """POST the quote edit form the way the browser does."""
        data = {
            "customer": str(self.customer.pk),
            "company": str(self.company.pk),
            "validity_period_days": "7",
            "status": Quote.Status.DRAFT,
            "currency": "USD",
            "exchange_rate": "1",
            "sales_stage": Quote.SalesStage.PROSPECTING,
            "win_probability": "0",
            "weighted_value": "0",
            "subtotal_ex_tax": "500.00",
            "discount_amount": "0",
            "tax_total": "0",
            "shipping_cost_ex_tax": "0",
            "grand_total": "500.00",
            "sg_count": "1",
            "line_items-TOTAL_FORMS": "1",
            "line_items-INITIAL_FORMS": "1",
            "line_items-MIN_NUM_FORMS": "0",
            "line_items-MAX_NUM_FORMS": "1000",
            "_action": "convert_to_order",
        }
        line = self.quote.line_items.get()
        data.update({
            "line_items-0-id": str(line.pk),
            "line_items-0-quote": str(self.quote.pk),
            "line_items-0-variant": "",
            "line_items-0-sku": line.sku,
            "line_items-0-item_name": line.item_name,
            "line_items-0-quantity": "1",
            "line_items-0-unit_price_ex_tax": "500.0000",
            "line_items-0-discount_percent": "",
            "line_items-0-line_total_ex_tax": "500.00",
            "line_items-0-shipment_group": "",
            "line_items-0-sg_index": "0",
        })
        data.update(extra)
        return self.client.post(
            reverse("erp-quote-edit", args=[self.quote.pk]), data
        )

    def test_button_converts_and_redirects_to_the_new_order(self):
        response = self._post()
        self.quote.refresh_from_db()
        self.assertIsNotNone(self.quote.converted_to_order_id)
        order = self.quote.converted_to_order
        self.assertRedirects(
            response,
            reverse("erp-order-edit", args=[order.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(order.status, Order.Status.PENDING)

    def test_edits_made_in_the_same_visit_reach_the_order(self):
        # The line price is changed in the same POST that converts. If the
        # view converted the stale row, the order would carry 500.00.
        self._post(**{
            "line_items-0-unit_price_ex_tax": "725.0000",
            "line_items-0-line_total_ex_tax": "725.00",
        })
        self.quote.refresh_from_db()
        order = self.quote.converted_to_order
        self.assertEqual(
            order.line_items.get().unit_price_ex_tax, Decimal("725.0000")
        )

    def test_a_second_convert_does_not_mint_a_second_order(self):
        self._post()
        self._post()
        self.assertEqual(Order.objects.count(), 1)
