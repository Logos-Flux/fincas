"""Guarded pick-ticket transitions (fulfillment leg of §3 state machines).

PickTicket had no transition map: any status was PUT/PATCHable and the
shipped ticket — the warehouse's proof of dispatch — was mutable. Pins the
same contract as the order-side transitions: legal moves 200, illegal moves
400 with the ``{"status": [...]}`` envelope, shipped immutable.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from customers.models import Customer
from fulfillment.models import PickTicket
from fulfillment.views import PickTicketViewSet
from orders.models import Order


def company_user(company, *, username, uid):
    user = make_staff_user(username=username, password="pw")
    user.userprofile = UserProfile.objects.create(company=company)
    return user


def post_transition(pk, user, target):
    factory = APIRequestFactory()
    request = factory.post("/api/v1/transition/", {"status": target}, format="json")
    force_authenticate(request, user=user)
    view = PickTicketViewSet.as_view({"post": "transition"})
    return view(request, pk=str(pk))


class PickTicketTransitionTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="pt@example.com", company=self.company,
        )
        self.user = company_user(self.company, username="pt-staff", uid="uid-pt")
        self.order = Order.objects.create(
            customer=self.customer, company=self.company,
        )
        self.ticket = PickTicket.objects.create(
            order=self.order, status=PickTicket.Status.CREATED,
        )

    def test_legal_move_succeeds(self):
        self.ticket.transition_to(PickTicket.Status.ASSIGNED)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, PickTicket.Status.ASSIGNED)

    def test_full_pick_pack_ship_chain_walks(self):
        for nxt in (
            PickTicket.Status.ASSIGNED,
            PickTicket.Status.PICKING,
            PickTicket.Status.PICKED,
            PickTicket.Status.PACKING,
            PickTicket.Status.PACKED,
            PickTicket.Status.SHIPPED,
        ):
            self.ticket.transition_to(nxt)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, PickTicket.Status.SHIPPED)

    def test_illegal_skip_raises(self):
        with self.assertRaises(ValidationError):
            self.ticket.transition_to(PickTicket.Status.SHIPPED)

    def test_terminal_shipped_is_immutable(self):
        self.ticket.status = PickTicket.Status.SHIPPED
        self.ticket.save(update_fields=["status", "updated_at"])
        with self.assertRaises(ValidationError):
            self.ticket.transition_to(PickTicket.Status.PACKED)

    def test_api_legal_move_returns_200(self):
        response = post_transition(self.ticket.pk, self.user, "assigned")
        self.assertEqual(response.status_code, 200)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, PickTicket.Status.ASSIGNED)

    def test_api_illegal_move_returns_400_field_envelope(self):
        response = post_transition(self.ticket.pk, self.user, "shipped")
        self.assertEqual(response.status_code, 400)
        payload = response.data
        self.assertIsInstance(payload, dict)
        self.assertIn("status", payload)
        self.assertNotIn("detail", payload)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, PickTicket.Status.CREATED)

    def test_api_terminal_state_is_immutable(self):
        self.ticket.status = PickTicket.Status.SHIPPED
        self.ticket.save(update_fields=["status", "updated_at"])
        response = post_transition(self.ticket.pk, self.user, "packed")
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.data)
