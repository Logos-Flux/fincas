"""``shipment-groups/`` and the pick ticket made from one, as a service account.

A restricted integration key splits an order and raises its pick ticket with
exactly the model permissions that describe those writes (docs/identity-
unification.md 2.6), inside its own tenant.
"""

from decimal import Decimal

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import Company
from core.service_accounts import ensure_service_account, issue_api_key
from customers.models import Customer
from fulfillment.models import PickTicket
from inventory.models import Warehouse
from orders.models import Order, OrderLineItem, ShipmentGroup
from purchasing.models import Supplier

ROLES = [
    {"name": "fulfilment", "label": "Fulfilment", "kind": "service",
     "permissions": ["orders.view_order", "orders.view_orderlineitem", "orders.change_orderlineitem",
                     "orders.view_shipmentgroup", "orders.add_shipmentgroup",
                     "orders.change_shipmentgroup", "fulfillment.view_pickticket",
                     "fulfillment.add_pickticket", "fulfillment.add_pickticketline",
                     "inventory.view_warehousestock"]},
    # Everything a ticket needs except reading tickets.
    {"name": "blind-picker", "label": "Blind picker", "kind": "service",
     "permissions": ["orders.view_order", "fulfillment.add_pickticket",
                     "fulfillment.add_pickticketline", "inventory.view_warehousestock"]},
    # Reads everything above, writes nothing; and one permission short of a ticket.
    {"name": "reader", "label": "Reader", "kind": "service",
     "permissions": ["orders.view_order", "orders.view_orderlineitem", "orders.view_shipmentgroup",
                     "fulfillment.view_pickticket", "fulfillment.add_pickticket"]},
]

GROUPS = "/api/v1/orders/shipment-groups/"
FOR_SHIPMENT = "/api/v1/fulfillment/pick-tickets/for-shipment/"


@override_settings(API_KEY="", GOLDEN_ROLES=ROLES)
class ShipmentGroupApiTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.other = Company.objects.create(name="Other Tenant LLC")
        self.order = self._order(self.company, "ada-sg@example.com")
        self.group = ShipmentGroup.objects.create(order=self.order, name="Shipment 1")
        self.line = OrderLineItem.objects.create(
            order=self.order, shipment_group=self.group, item_name="Widget", sku="W-1",
            quantity=Decimal("4"), unit_price_ex_tax=Decimal("25.00"),
        )
        self.warehouse = Warehouse.objects.create(
            warehouse_code="SG-MAIN", name="Main", company=self.company,
        )
        self.their_order = self._order(self.other, "eve-sg@example.com")
        self.their_group = ShipmentGroup.objects.create(order=self.their_order, name="Theirs")
        self.their_supplier = Supplier.objects.create(
            company_name="Their Vendor", supplier_type="distributor", company=self.other,
        )
        self.api = self._api("fulfil-key", "fulfilment")

    def _order(self, company, email):
        customer = Customer.objects.create(
            first_name="A", last_name="B", email=email, company=company,
        )
        return Order.objects.create(customer=customer, company=company)

    def _api(self, name, role):
        svc = ensure_service_account(name, [role], company=self.company)
        _, raw = issue_api_key(svc, name=name)
        return APIClient(HTTP_AUTHORIZATION=f"Api-Key {raw}")

    def _create(self, api=None, **body):
        body = {"order": str(self.order.pk), "name": "Drop", "fulfillment_type": "drop_ship", **body}
        return (api or self.api).post(GROUPS, body, format="json")

    def test_split_an_order_and_move_a_line(self):
        response = self._create(fulfillment_type="internal", warehouse=str(self.warehouse.pk))
        self.assertEqual(response.status_code, 201, response.content)
        new_group = response.json()["id"]
        response = self.api.patch(
            f"/api/v1/orders/order-lines/{self.line.pk}/", {"shipment_group": new_group}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        listed = self.api.get(GROUPS, {"order": str(self.order.pk)}).json()["results"]
        self.assertEqual([g["name"] for g in listed], ["Shipment 1", "Drop"])
        detail = self.api.get(f"/api/v1/orders/orders/{self.order.pk}/").json()
        self.assertEqual(len(detail["shipment_groups"]), 2)

    def test_only_this_tenants_orders_and_sources(self):
        self.assertEqual(self._create(order=str(self.their_order.pk)).status_code, 400)
        self.assertEqual(self._create(supplier=str(self.their_supplier.pk)).status_code, 400)
        self.assertEqual(self.api.get(f"{GROUPS}{self.their_group.pk}/").status_code, 404)
        listed = self.api.get(GROUPS).json()["results"]
        self.assertEqual([g["id"] for g in listed], [str(self.group.pk)])
        self.assertEqual(ShipmentGroup.objects.count(), 2)

    def test_a_shipment_stays_on_its_order_and_is_never_deleted(self):
        second = self._order(self.company, "bob-sg@example.com")
        url = f"{GROUPS}{self.group.pk}/"
        response = self.api.patch(url, {"order": str(second.pk)}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(self.api.patch(url, {"name": "Warehouse"}, format="json").status_code, 200)
        self.assertIn(self.api.delete(url).status_code, (403, 405))
        self.assertTrue(ShipmentGroup.objects.filter(pk=self.group.pk).exists())
        self.assertEqual(self._create(order=None).status_code, 400)

    def test_a_line_joins_only_a_shipment_of_its_own_order(self):
        second = self._order(self.company, "cy-sg@example.com")
        elsewhere = ShipmentGroup.objects.create(order=second, name="Elsewhere")
        response = self.api.patch(
            f"/api/v1/orders/order-lines/{self.line.pk}/",
            {"shipment_group": str(elsewhere.pk)}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("another order", response.json()["shipment_group"][0])

    def test_a_reader_writes_nothing(self):
        reader = self._api("read-key", "reader")
        self.assertEqual(reader.get(GROUPS).status_code, 200)
        self.assertEqual(self._create(api=reader).status_code, 403)
        url = f"{GROUPS}{self.group.pk}/"
        self.assertEqual(reader.patch(url, {"name": "X"}, format="json").status_code, 403)
        # add_pickticket and view are short of the ticket's lines and the bins.
        body = {"order": str(self.order.pk), "shipment_group": str(self.group.pk)}
        self.assertEqual(reader.post(FOR_SHIPMENT, body, format="json").status_code, 403)
        self.assertEqual(PickTicket.objects.count(), 0)

    def test_pick_ticket_for_a_shipment_is_made_once_and_locks_its_lines(self):
        body = {"order": str(self.order.pk), "shipment_group": str(self.group.pk)}
        response = self.api.post(FOR_SHIPMENT, body, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        ticket = response.json()
        self.assertEqual(ticket["warehouse"], str(self.warehouse.pk))
        self.assertEqual([line["sku"] for line in ticket["lines"]], ["W-1"])
        again = self.api.post(FOR_SHIPMENT, body, format="json")
        self.assertEqual((again.status_code, again.json()["id"]), (200, ticket["id"]))
        self.assertEqual(PickTicket.objects.count(), 1)

        new_group = self._create().json()["id"]
        response = self.api.patch(
            f"/api/v1/orders/order-lines/{self.line.pk}/", {"shipment_group": new_group}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("pick ticket", response.json()["shipment_group"][0])

    def test_pick_ticket_refuses_another_tenants_order_or_a_mismatched_shipment(self):
        cases = [
            {"order": str(self.their_order.pk), "shipment_group": str(self.their_group.pk)},
            {"order": str(self.order.pk), "shipment_group": str(self.their_group.pk)},
            {"order": str(self.order.pk)},
            {"order": "not-a-uuid", "shipment_group": str(self.group.pk)},
        ]
        for body in cases:
            with self.subTest(body=body):
                response = self.api.post(FOR_SHIPMENT, body, format="json")
                self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(PickTicket.objects.count(), 0)

    def test_an_existing_ticket_is_not_shown_to_a_key_that_cannot_read_tickets(self):
        body = {"order": str(self.order.pk), "shipment_group": str(self.group.pk)}
        self.assertEqual(self.api.post(FOR_SHIPMENT, body, format="json").status_code, 201)
        blind = self._api("blind-key", "blind-picker")
        self.assertEqual(blind.post(FOR_SHIPMENT, body, format="json").status_code, 403)

    def test_the_default_warehouse_is_the_orders_company_never_anothers(self):
        Warehouse.objects.create(warehouse_code="AAA", name="AAA Theirs", company=self.other)
        body = {"order": str(self.order.pk), "shipment_group": str(self.group.pk)}
        ticket = self.api.post(FOR_SHIPMENT, body, format="json").json()
        self.assertEqual(ticket["warehouse"], str(self.warehouse.pk))

    def test_a_warehouse_of_another_company_is_refused_even_for_a_superuser(self):
        from django.contrib.auth.models import User

        theirs = Warehouse.objects.create(warehouse_code="THR", name="Theirs", company=self.other)
        root = APIClient()
        root.force_authenticate(User.objects.create_superuser("root-sg", password=None))
        body = {"order": str(self.order.pk), "shipment_group": str(self.group.pk),
                "warehouse": str(theirs.pk)}
        response = root.post(FOR_SHIPMENT, body, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(self.api.post(FOR_SHIPMENT, body, format="json").status_code, 400)
        self.assertEqual(root.post(FOR_SHIPMENT, [1, 2], format="json").status_code, 400)
        self.assertEqual(PickTicket.objects.count(), 0)

    def test_a_company_less_source_is_nobodys(self):
        nobody = Warehouse.objects.create(warehouse_code="NOB", name="Nobody's")
        response = self._create(fulfillment_type="internal", warehouse=str(nobody.pk))
        self.assertEqual(response.status_code, 400, response.content)

    def test_a_line_cannot_change_order(self):
        second = self._order(self.company, "dee-sg@example.com")
        response = self.api.patch(
            f"/api/v1/orders/order-lines/{self.line.pk}/", {"order": str(second.pk)}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.line.refresh_from_db()
        self.assertEqual(self.line.order_id, self.order.pk)

    def test_move_lines_is_all_or_nothing(self):
        free = OrderLineItem.objects.create(
            order=self.order, shipment_group=self.group, item_name="Free", sku="F-1",
            quantity=Decimal("1"), unit_price_ex_tax=Decimal("1.00"),
        )
        body = {"order": str(self.order.pk), "shipment_group": str(self.group.pk)}
        self.api.post(FOR_SHIPMENT, body, format="json")  # both lines are now on the ticket
        late = OrderLineItem.objects.create(
            order=self.order, shipment_group=self.group, item_name="Late", sku="L-1",
            quantity=Decimal("1"), unit_price_ex_tax=Decimal("1.00"),
        )
        target = self._create().json()["id"]
        url = f"{GROUPS}{target}/move-lines/"
        response = self.api.post(url, {"lines": [str(late.pk), str(free.pk)]}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn(str(free.pk), response.json()["lines"])
        late.refresh_from_db()
        self.assertEqual(late.shipment_group_id, self.group.pk)
        response = self.api.post(url, {"lines": [str(late.pk)]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["moved"], [str(late.pk)])
        reader = self._api("read-key-2", "reader")
        self.assertEqual(reader.post(url, {"lines": [str(late.pk)]}, format="json").status_code, 403)

    def test_a_purchase_order_cannot_point_at_another_tenants_order(self):
        with override_settings(GOLDEN_ROLES=ROLES + [
            {"name": "buyer", "label": "Buyer", "kind": "service",
             "permissions": ["purchasing.add_purchaseorder", "purchasing.view_purchaseorder"]},
        ]):
            buyer = self._api("buyer-key", "buyer")
            mine = Supplier.objects.create(
                company_name="My Vendor", supplier_type="distributor", company=self.company,
            )
            for extra in ({"order": str(self.their_order.pk)},
                          {"shipment_group": str(self.their_group.pk)}):
                with self.subTest(extra=extra):
                    response = buyer.post(
                        "/api/v1/purchasing/purchase-orders/",
                        {"supplier": str(mine.pk), **extra}, format="json",
                    )
                    self.assertEqual(response.status_code, 400, response.content)
