"""User-visible text is US English; stored values keep their historical
spelling (CLAUDE.md "Copy"). The 2026-10-15 relabel migrations are
state-only, so this pins that no value moved with its label."""

from django.test import SimpleTestCase


class StoredSpellingTests(SimpleTestCase):
    def test_relabelled_choices_keep_their_stored_values(self):
        from crm.models import Task, TaskEvent
        from orders.models import Order, Quote, SalesInvoice, SubOrder
        from parties.models import Party

        for enum in (Order.Status, Quote.Status, SalesInvoice.Status, SubOrder.Status, Task.Status,
                     TaskEvent.Event):
            with self.subTest(enum=enum.__qualname__):
                self.assertEqual(enum.CANCELLED.value, "cancelled")
                self.assertEqual(enum.CANCELLED.label, "Canceled")
        self.assertEqual(Party.Kind.ORGANISATION.value, "organisation")
        self.assertEqual(Party.Kind.ORGANISATION.label, "Organization")
