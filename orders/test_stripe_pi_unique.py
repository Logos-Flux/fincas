"""`uniq_order_stripe_pi`: one Order per Stripe PaymentIntent.

A partial unique index on ``stripe_payment_intent_id WHERE <> ''``: two orders
may not share a non-empty intent id, while any number of orders not paid
through Stripe (blank id) may coexist. Test settings build tables from model
state, so this exercises the constraint exactly as declared on the model.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import TestCase

from core.models import Company
from orders.models import Order


class StripePaymentIntentUniqueTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="PI Test Co")

    def _order(self, pi):
        return Order.objects.create(company=self.company, stripe_payment_intent_id=pi)

    def test_duplicate_non_empty_intent_is_rejected(self):
        self._order("pi_123")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._order("pi_123")

    def test_blank_intents_do_not_collide(self):
        self._order("")
        self._order("")
        self.assertEqual(Order.objects.filter(stripe_payment_intent_id="").count(), 2)

    def test_distinct_intents_coexist(self):
        self._order("pi_a")
        self._order("pi_b")
        self.assertEqual(Order.objects.exclude(stripe_payment_intent_id="").count(), 2)

    def test_constraint_is_declared_with_the_inequality_predicate(self):
        constraint = next(c for c in Order._meta.constraints if c.name == "uniq_order_stripe_pi")
        self.assertEqual(constraint.fields, ("stripe_payment_intent_id",))
        # Postgres stores `<>` verbatim; `~Q(...)` would store `NOT (... = ...)`
        # and stop matching an index created elsewhere as `<> ''`.
        where = str(Order.objects.filter(constraint.condition).query).split("WHERE", 1)[1]
        self.assertIn("<>", where)
        self.assertNotIn("NOT", where.upper())
