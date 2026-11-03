"""Milestone 2 Track D — print views (pick ticket).

Covers testing-plan §5 "Print views": the pick-ticket print template
renders clean without nav chrome. There is no packing-slip template in
the repo (see findings report) — only pick_ticket_print.html.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import Company
from customers.models import Customer
from fulfillment.models import PickTicket
from inventory.models import Warehouse
from orders.models import Order


class PickTicketPrintTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Print Corp")
        customer = Customer.objects.create(
            first_name="Pam",
            last_name="Print",
            email="pam@example.com",
            company=self.company,
        )
        order = Order.objects.create(
            customer=customer, company=self.company
        )
        warehouse = Warehouse.objects.create(
            warehouse_code="PRINT-WH",
            name="Print Warehouse",
            default_for_company=self.company,
        )
        self.ticket = PickTicket.objects.create(
            warehouse=warehouse, order=order, notes="Fragile — top load only"
        )
        user = User.objects.create_user(
            username="print-staff", password="pw", is_staff=True
        )
        self.client.force_login(user)

    def test_print_view_renders_key_content(self):
        response = self.client.get(
            reverse("erp-pickticket-print", kwargs={"pk": self.ticket.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.ticket.ticket_number)
        self.assertContains(response, "Print Warehouse")
        self.assertContains(response, "Fragile")

    def test_print_view_has_no_nav_chrome(self):
        """The print template is standalone — no sidebar/nav/footer chrome
        that would bleed onto the printed page."""
        html = self.client.get(
            reverse("erp-pickticket-print", kwargs={"pk": self.ticket.pk})
        ).content.decode()
        for chrome in ("sidebar", "main-nav", "topnav", "<nav", "erp/base"):
            self.assertNotIn(chrome, html)

    def test_print_view_requires_login(self):
        self.client.logout()
        response = self.client.get(
            reverse("erp-pickticket-print", kwargs={"pk": self.ticket.pk})
        )
        self.assertIn(response.status_code, (301, 302))
