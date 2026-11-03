"""CRM Phase 3 step 6: historical EmailLogs onto the thread (spec 2.5, E9).

The rows are EmailLogs as written before step 4's send hook: created
directly, so no Communication exists for them.
"""

from __future__ import annotations

import importlib
import io
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal

from django.apps import apps as live_apps
from django.test import TestCase

from communications.backfill import (
    backfill_email_communications,
    link_document_emails,
    reverse_email_communications,
)
from communications.models import Communication, EmailLog
from core.models import Company
from customers.models import Customer
from orders.models import Order, Quote, SalesInvoice, SubOrder


def _log(document, document_type, **fields):
    fields.setdefault("recipient_email", "ada@example.com")
    fields.setdefault("status", EmailLog.Status.SENT)
    return EmailLog.objects.create(
        document_type=document_type, document_id=document.pk,
        document_number=getattr(document, "quote_number", "") or "DOC", subject=f"Your {document_type}",
        template_name=document_type, triggered_by="manual", **fields,
    )


class EmailBackfillTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace", email="ada@example.com", company=cls.company,
        )
        cls.quote = Quote.objects.create(customer=cls.customer, company=cls.company)
        cls.order = Order.objects.create(
            customer=cls.customer, company=cls.company, subtotal_ex_tax=100, grand_total=100,
        )
        cls.invoice = SalesInvoice.objects.create(
            order=cls.order, company=cls.company, invoice_date=date.today(),
            subtotal_ex_tax=Decimal("100"), grand_total=Decimal("100"), balance_due=Decimal("100"),
        )
        cls.sub_order = SubOrder.objects.create(order=cls.order, sub_order_number="SO-1-1", status="shipped")

    def test_every_document_type_is_linked_once(self):
        logs = {
            "quote": _log(self.quote, "quote"),
            "sales_order": _log(self.order, "sales_order"),
            "invoice": _log(self.invoice, "invoice"),
            "shipping_notification": _log(self.sub_order, "shipping_notification"),
        }
        po = _log(self.order, "purchase_order", recipient_email="supplier@example.com")
        summary = backfill_email_communications()
        self.assertEqual(sum(summary.linked.values()), 4)
        self.assertEqual(summary.skipped_purchase_orders, 1)
        for document_type, log in logs.items():
            with self.subTest(document_type=document_type):
                comm = Communication.objects.get(email_log=log)
                self.assertEqual((comm.kind, comm.direction, comm.provider, comm.is_editable),
                                 ("email", "outbound", "golden", False))
                self.assertEqual(comm.party, self.customer.party)
                self.assertEqual(comm.occurred_at, log.created_at)
                self.assertEqual(comm.thread_key, f"golden:{document_type}:{log.document_id}")
                self.assertEqual(comm.metadata["backfill_step"], 6)
                self.assertEqual(comm.participants.get().address_normalized, "ada@example.com")
        self.assertEqual(Communication.objects.get(email_log=logs["quote"]).engagement, self.quote.engagement)
        self.assertFalse(Communication.objects.filter(email_log=po).exists())

    def test_failed_and_bounced_logs_are_failed_communications(self):
        failed = _log(self.quote, "quote", status=EmailLog.Status.FAILED)
        bounced = _log(self.quote, "quote", status=EmailLog.Status.BOUNCED)
        backfill_email_communications()
        for log in (failed, bounced):
            self.assertEqual(Communication.objects.get(email_log=log).status, "failed")

    def test_a_vanished_document_is_counted_and_skipped(self):
        gone = Order.objects.create(customer=self.customer, company=self.company, grand_total=1)
        log = _log(gone, "sales_order")
        Order.objects.filter(pk=gone.pk).delete()
        summary = backfill_email_communications()
        self.assertEqual(summary.missing_document, 1)
        self.assertFalse(Communication.objects.filter(email_log=log).exists())

    def test_idempotent_and_reverse_keeps_send_site_rows(self):
        old = _log(self.quote, "quote")
        backfill_email_communications()
        self.assertEqual(backfill_email_communications().logs, 0)
        self.assertEqual(Communication.objects.filter(email_log=old).count(), 1)
        from communications.email_link import log_document_email

        new = log_document_email(self.quote, document_type="quote", document_id=self.quote.pk,
                                 document_number=self.quote.quote_number, recipient_email="ada@example.com",
                                 subject="Again", template_name="quote", status="sent", triggered_by="manual")
        reverse_email_communications()
        self.assertFalse(Communication.objects.filter(email_log=old).exists())
        self.assertTrue(Communication.objects.filter(email_log=new).exists())

    def test_an_email_logged_before_its_quote_had_an_engagement_moves_onto_it(self):
        legacy = Quote(company=self.company, customer=self.customer, quote_number="QT-LEGACY-1")
        Quote.objects.bulk_create([legacy])  # pre-step-5: no engagement
        log = _log(legacy, "quote")
        backfill_email_communications()
        comm = Communication.objects.get(email_log=log)
        self.assertIsNone(comm.engagement_id)
        legacy.refresh_from_db()
        legacy.save()  # ensure_for_quote, lazily
        self.assertEqual(link_document_emails(), 1)
        comm.refresh_from_db()
        self.assertEqual(comm.engagement, legacy.engagement)
        self.assertEqual(link_document_emails(), 0)

    def test_dry_run_and_the_migration(self):
        _log(self.quote, "quote")
        self.assertEqual(sum(backfill_email_communications(dry_run=True).linked.values()), 1)
        self.assertFalse(Communication.objects.exists())
        module = importlib.import_module("communications.migrations.0003_backfill_email_communications")
        out = io.StringIO()
        with redirect_stdout(out):
            module.forwards(live_apps, None)
        self.assertIn("crm step 6 (emails): communications created: 1 (quote 1)", out.getvalue())
        with redirect_stdout(out):
            module.backwards(live_apps, None)
        self.assertFalse(Communication.objects.exists())


class EmailBackfillHistoricalModelsTests(TestCase):
    """Issue #88: ``communications.0003`` runs on its own state's models.

    A database upgrading across releases reaches 0003 before later migrations
    add columns to the live models (``Party.owner_team``, ``Engagement.owner_team``,
    ``UserProfile.team``). The step must select none of them, and still build
    the same communication as the send sites.
    """

    LATER_COLUMNS = ("owner_team_id", '"team_id"')

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model

        from core.models import UserProfile

        cls.company = Company.objects.create(name="Tenant A")
        cls.user = get_user_model().objects.create_user("clerk", password=None)
        cls.profile = UserProfile.objects.create(user=cls.user, company=cls.company)
        cls.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace", email="ada@example.com", company=cls.company,
        )
        cls.quote = Quote.objects.create(customer=cls.customer, company=cls.company)

    def _state_apps(self):
        from django.db.migrations.loader import MigrationLoader
        from django.test.utils import override_settings

        # The test settings disable migrations; the graph is only read here.
        with override_settings(MIGRATION_MODULES={}):
            loader = MigrationLoader(None, ignore_no_migrations=True)
        return loader.project_state(("communications", "0003_backfill_email_communications")).apps

    def test_the_migration_selects_no_column_added_later(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        log = _log(self.quote, "quote", sent_by=self.user)
        state_apps = self._state_apps()
        module = importlib.import_module("communications.migrations.0003_backfill_email_communications")
        with CaptureQueriesContext(connection) as queries, redirect_stdout(io.StringIO()):
            module.forwards(state_apps, None)
        offending = [q["sql"] for q in queries if any(c in q["sql"] for c in self.LATER_COLUMNS)]
        self.assertEqual(offending, [])

        comm = Communication.objects.get(email_log=log)
        self.assertEqual((comm.kind, comm.direction, comm.provider, comm.is_editable, comm.status),
                         ("email", "outbound", "golden", False, "completed"))
        self.assertEqual(comm.engagement, self.quote.engagement)
        self.assertEqual(comm.party, self.customer.party)
        self.assertEqual(comm.performed_by, self.profile)
        self.assertEqual(comm.metadata["backfill_step"], 6)
        participant = comm.participants.get()
        self.assertEqual((participant.party, participant.address_normalized),
                         (self.customer.party, "ada@example.com"))
        self.assertIsNotNone(participant.party_email_id)

    def test_historical_and_live_paths_build_the_same_communication(self):
        from communications.email_link import link_email_log

        fields = ("company_id", "kind", "direction", "status", "subject", "occurred_at", "provider",
                  "thread_key", "engagement_id", "party_id", "performed_by_id", "is_editable", "metadata")
        live_log = _log(self.quote, "quote", sent_by=self.user)
        live = link_email_log(live_log, self.quote)
        historical_log = _log(self.quote, "quote", sent_by=self.user)
        with redirect_stdout(io.StringIO()):
            backfill_email_communications(self._state_apps())
        backfilled = Communication.objects.get(email_log=historical_log)
        live.refresh_from_db()

        def shape(comm):
            values = {f: getattr(comm, f) for f in fields}
            values["occurred_at"] = None  # the two logs were written a moment apart
            values["metadata"] = {k: v for k, v in comm.metadata.items() if k != "backfill_step"}
            participant = comm.participants.get()
            return values, (participant.role, participant.party_id, participant.party_email_id,
                            participant.address_normalized)

        self.assertEqual(shape(backfilled), shape(live))
