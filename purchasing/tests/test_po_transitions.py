"""Guarded PO status transitions (purchasing leg of §3 state machines).

PurchaseOrder had no transition map: any status was PUT/PATCHable, and
terminal states (closed/cancelled) were mutable. Pins the same contract as
the order-side transitions — legal moves 200, illegal moves 400 with the
``{"status": [...]}`` envelope, terminal states immutable.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from purchasing.models import PurchaseOrder, Supplier
from purchasing.views import PurchaseOrderViewSet


def company_user(company, *, username, uid):
    user = make_staff_user(username=username, password="pw")
    user.userprofile = UserProfile.objects.create(company=company)
    return user


def post_transition(pk, user, target):
    factory = APIRequestFactory()
    request = factory.post("/api/v1/transition/", {"status": target}, format="json")
    force_authenticate(request, user=user)
    view = PurchaseOrderViewSet.as_view({"post": "transition"})
    return view(request, pk=str(pk))


class PurchaseOrderTransitionTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.supplier = Supplier.objects.create(
            company_name="Acme Corp", supplier_code="ACME-T", company=self.company,
        )
        self.user = company_user(self.company, username="po-staff", uid="uid-po")
        self.po = PurchaseOrder.objects.create(
            company=self.company, supplier=self.supplier,
            status=PurchaseOrder.Status.DRAFT,
        )

    def test_legal_move_succeeds(self):
        self.po.transition_to(PurchaseOrder.Status.SENT)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.Status.SENT)

    def test_approval_chain_walks(self):
        for nxt in (
            PurchaseOrder.Status.PENDING_APPROVAL,
            PurchaseOrder.Status.APPROVED,
            PurchaseOrder.Status.SENT,
            PurchaseOrder.Status.ACKNOWLEDGED,
            PurchaseOrder.Status.SHIPPED,
            PurchaseOrder.Status.RECEIVED,
            PurchaseOrder.Status.CLOSED,
        ):
            self.po.transition_to(nxt)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.Status.CLOSED)

    def test_illegal_move_raises(self):
        with self.assertRaises(ValidationError):
            self.po.transition_to(PurchaseOrder.Status.RECEIVED)

    def test_terminal_closed_is_immutable(self):
        self.po.status = PurchaseOrder.Status.CLOSED
        self.po.save(update_fields=["status", "updated_at"])
        with self.assertRaises(ValidationError):
            self.po.transition_to(PurchaseOrder.Status.DRAFT)

    def test_terminal_cancelled_is_immutable(self):
        self.po.status = PurchaseOrder.Status.CANCELLED
        self.po.save(update_fields=["status", "updated_at"])
        with self.assertRaises(ValidationError):
            self.po.transition_to(PurchaseOrder.Status.DRAFT)

    def test_api_legal_move_returns_200(self):
        response = post_transition(self.po.pk, self.user, "sent")
        self.assertEqual(response.status_code, 200)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.Status.SENT)

    def test_api_illegal_move_returns_400_field_envelope(self):
        response = post_transition(self.po.pk, self.user, "received")
        self.assertEqual(response.status_code, 400)
        payload = response.data
        self.assertIsInstance(payload, dict)
        self.assertIn("status", payload)
        self.assertNotIn("detail", payload)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.Status.DRAFT)

    def test_api_terminal_state_is_immutable(self):
        self.po.status = PurchaseOrder.Status.CLOSED
        self.po.save(update_fields=["status", "updated_at"])
        response = post_transition(self.po.pk, self.user, "draft")
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.data)
