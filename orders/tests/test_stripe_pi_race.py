"""Two creates racing on one Stripe PaymentIntent (no Idempotency-Key).

The checkout's own create and a webhook backstop can both pass the caller's
"does an order exist for this intent?" check before either has written a row.
The partial unique index ``uniq_order_stripe_pi`` then refuses the loser's
INSERT. ``OrderViewSet.create`` must answer the loser with the winning order
(200), not a 500, and must not mask any other integrity error.
"""

from contextlib import ExitStack
from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework.validators import UniqueValidator

from core.models import Company
from customers.models import Customer
from orders.models import Order
from orders.views import OrderViewSet

from .test_recalculate import company_user

URL = "/api/v1/orders/orders/"


class StripePaymentIntentRaceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Race Co")
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="race@example.com", company=self.company,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=company_user(self.company))

    def _post(self, pi_id, **headers):
        return self.client.post(
            URL,
            {"customer": str(self.customer.pk), "stripe_payment_intent_id": pi_id},
            format="json",
            **headers,
        )

    def test_losing_create_resolves_to_the_winner(self):
        first = self._post("pi_race_1")
        self.assertEqual(first.status_code, 201, first.data)

        # The loser passed the caller-side check before the winner committed,
        # so nothing short of the unique index stops its INSERT.
        second = self._post("pi_race_1")

        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(Order.objects.filter(stripe_payment_intent_id="pi_race_1").count(), 1)

    def _race(self, pi_id, winner_company=None, postgres_message=False, **headers):
        """POST while the winner is committed but invisible to every
        pre-INSERT check, so only the unique index stops the loser.

        That window cannot be reproduced in one test transaction, so the two
        checks that would otherwise see the winner (the view's own lookup and
        the serializer's unique validator) are told it is not there yet. The
        INSERT then hits the real index (SQLite's message names the column),
        or, with ``postgres_message``, raises Postgres's wording (which names
        the index).
        """
        winner = Order.objects.create(
            company=winner_company or self.company,
            customer=self.customer if winner_company is None else None,
            stripe_payment_intent_id=pi_id,
        )
        real_lookup = OrderViewSet._order_for_intent
        calls = []

        def lookup(view, intent_id):
            calls.append(intent_id)
            return None if len(calls) == 1 else real_lookup(view, intent_id)

        patches = [
            patch.object(OrderViewSet, "_order_for_intent", lookup),
            patch.object(UniqueValidator, "__call__", return_value=None),
        ]
        if postgres_message:
            conflict = IntegrityError(
                'duplicate key value violates unique constraint "uniq_order_stripe_pi"'
            )
            patches.append(patch.object(OrderViewSet, "perform_create", side_effect=conflict))
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return winner, self._post(pi_id, **headers)

    def test_conflict_on_the_real_index_returns_the_winner(self):
        winner, loser = self._race("pi_race_2")
        self.assertEqual(loser.status_code, 200, loser.data)
        self.assertEqual(str(loser.data["id"]), str(winner.pk))
        self.assertEqual(Order.objects.filter(stripe_payment_intent_id="pi_race_2").count(), 1)

    def test_index_conflict_inside_the_idempotency_key_block_returns_the_winner(self):
        # The INSERT fails inside create()'s outer atomic(); the savepoint
        # must leave that transaction usable for the recovery SELECT.
        winner, loser = self._race("pi_race_8", HTTP_IDEMPOTENCY_KEY="webhook-8")
        self.assertEqual(loser.status_code, 200, loser.data)
        self.assertEqual(str(loser.data["id"]), str(winner.pk))

    def test_postgres_index_conflict_returns_the_winner(self):
        winner, loser = self._race("pi_race_7", postgres_message=True)
        self.assertEqual(loser.status_code, 200)
        self.assertEqual(str(loser.data["id"]), str(winner.pk))

    def test_race_inside_an_idempotency_key_block_also_resolves(self):
        first = self._post("pi_race_5", HTTP_IDEMPOTENCY_KEY="checkout-1")
        self.assertEqual(first.status_code, 201)
        second = self._post("pi_race_5", HTTP_IDEMPOTENCY_KEY="webhook-1")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["id"], first.data["id"])

    def test_blank_intent_ids_never_collide(self):
        self.assertEqual(self._post("").status_code, 201)
        self.assertEqual(self._post("").status_code, 201)

    def test_unrelated_integrity_error_still_raises(self):
        error = IntegrityError("FOREIGN KEY constraint failed")
        with patch.object(OrderViewSet, "perform_create", side_effect=error):
            with self.assertRaises(IntegrityError):
                self._post("pi_race_3")

    def test_conflict_with_another_companys_order_is_not_disclosed(self):
        other = Company.objects.create(name="Other Co")
        theirs = Order.objects.create(company=other, stripe_payment_intent_id="pi_race_4")
        response = self._post("pi_race_4")
        # Refused by the serializer's unique validator; never their order.
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(str(theirs.pk), str(response.data))

    def test_index_conflict_with_another_companys_order_still_raises(self):
        other = Company.objects.create(name="Other Co")
        with self.assertRaises(IntegrityError):
            self._race("pi_race_6", winner_company=other)
