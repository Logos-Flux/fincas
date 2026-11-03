"""CRM Phase 3 step 5: quotes and orders on engagements (spec 1.6, section 7).

The backfill tests build pre-engagement quotes with ``bulk_create`` (no
``save()``, so no engagement), the state the step-5 migration finds them in.
"""

from __future__ import annotations

import importlib
import io
import itertools
import os
import warnings
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.apps import apps as live_apps
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from crm import backfill
from crm.backfill import (
    UnmappedStagesError,
    backfill_quote_engagements,
    normalise_stage,
    reverse_quote_engagements,
)
from crm.engagements import ensure_for_quote, lose, move
from crm.models import Engagement, EngagementStageChange, Pipeline, Task
from customers.models import Customer
from orders.convert import convert_quote_to_order
from orders.duplicate import duplicate_quote
from orders.models import Order, Quote, QuoteLineItem

from .helpers import profile

_numbers = itertools.count(1)


class _QuoteFixtures(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other_company = Company.objects.create(name="Tenant B")
        cls.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace", email="ada@example.com", company=cls.company,
        )
        cls.other_customer = Customer.objects.create(
            first_name="Rex", last_name="Rival", email="rex@example.com", company=cls.other_company,
        )
        cls.rep = profile(cls.company, "rep")
        cls.pipeline = Pipeline.objects.get(company=cls.company, key="sales")

    def legacy(self, **fields):
        """A pre-engagement quote row (as the backfill finds it)."""
        fields.setdefault("company", self.company)
        fields.setdefault("customer", self.customer)
        for name in Quote.MOVED_TO_ENGAGEMENT:
            if name in fields:
                fields[f"legacy_{name}"] = fields.pop(name)
        quote = Quote(quote_number=f"QT-L{next(_numbers):05d}", **fields)
        Quote.objects.bulk_create([quote])
        return quote

    def chain(self, *statuses, **head_fields):
        """A revision chain v1..vN; ``original_quote`` points at the root and
        every older row is superseded by the next (both links, as a revision
        writer sets them)."""
        rows = []
        for n, status in enumerate(statuses, start=1):
            extra = head_fields if n == len(statuses) else {}
            rows.append(self.legacy(
                status=status, revision_number=n,
                original_quote=rows[0] if rows else None, **extra,
            ))
        for older, newer in zip(rows, rows[1:], strict=False):
            Quote.objects.filter(pk=older.pk).update(superseded_by=newer)
        return rows

    def run_backfill(self, **kwargs):
        return backfill_quote_engagements(**kwargs)

    def eng(self, quote):
        return Quote.objects.select_related("engagement__stage").get(pk=quote.pk).engagement


class StageNormalisationTests(TestCase):
    def test_table(self):
        cases = {
            "Closed Won": "closed_won", "closed-won": "closed_won", "closedwon": "closed_won",
            "WON": "closed_won", " lost ": "closed_lost", "Closed  Lost": "closed_lost",
            "Proposal Sent": "proposal_sent", "proposal": "proposal_sent",
            " Negotiating ": "negotiation", "qualified": "qualification",
            "Needs-Analysis": "needs_analysis", "verbal": "verbal_commit",
            "": "prospecting", None: "prospecting", "prospect": "prospecting",
            "bogus stage": "bogus_stage",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalise_stage(raw), expected)


class QuoteBackfillTests(_QuoteFixtures):
    def test_revision_chain_yields_one_engagement(self):
        v1, v2, v3 = self.chain("superseded", "superseded", "sent", sales_stage="negotiation")
        summary = self.run_backfill()
        self.assertEqual(summary.created["open"], 1)
        eng = self.eng(v3)
        self.assertEqual({self.eng(v1).pk, self.eng(v2).pk}, {eng.pk})
        self.assertEqual((eng.status, eng.stage.key, eng.origin), ("open", "negotiation", "backfill"))
        self.assertEqual(eng.party_id, self.customer.party_id)
        self.assertEqual(eng.metadata["backfill"]["head"], str(v3.pk))
        self.assertEqual(eng.metadata["backfill"]["chain_root"], str(v1.pk))
        self.assertTrue(eng.number.startswith("EN-"))
        changes = list(eng.stage_changes.all())
        self.assertEqual([(c.from_status, c.to_status, c.source) for c in changes],
                         [("", "open", "backfill")])
        self.assertEqual(changes[0].changed_at, v1.created_at)

    def test_chain_linked_only_by_superseded_by_is_one_engagement(self):
        old = self.legacy(status="sent")
        new = self.legacy(status="lost", sales_stage="negotiation", loss_reason="price")
        Quote.objects.filter(pk=old.pk).update(superseded_by=new)
        self.run_backfill()
        eng = self.eng(new)
        self.assertEqual(self.eng(old).pk, eng.pk)
        # The head is the row not superseded: lost at Negotiation, for price.
        self.assertEqual((eng.status, eng.stage.key, eng.loss_reason), ("lost", "negotiation", "price"))

    def test_outcome_from_status_and_conversion_not_only_stage(self):
        order = Order.objects.create(customer=self.customer, company=self.company)
        converted = self.legacy(status="sent", converted_to_order=order,
                                converted_at=timezone.now())
        accepted = self.legacy(status="accepted", sales_stage="proposal_sent")
        expired = self.legacy(status="expired")
        cancelled = self.legacy(status="cancelled", sales_stage="Qualified")
        stage_lost = self.legacy(status="sent", sales_stage="Closed Lost")
        stage_won = self.legacy(status="sent", sales_stage="closed_won")
        self.run_backfill()
        expect = {
            converted: ("won", "prospecting", ""),
            accepted: ("won", "proposal_sent", ""),
            expired: ("lost", "prospecting", "expired"),
            cancelled: ("lost", "qualification", "cancelled"),
            stage_lost: ("lost", None, "unknown"),
            stage_won: ("won", None, ""),
        }
        for quote, (status, stage, reason) in expect.items():
            eng = self.eng(quote)
            with self.subTest(quote=quote.status):
                self.assertEqual(eng.status, status)
                self.assertEqual(eng.stage.key if eng.stage_id else None, stage)
                self.assertEqual(eng.loss_reason, reason)
                self.assertIsNotNone(eng.closed_at)
        order.refresh_from_db()
        self.assertEqual(order.engagement_id, self.eng(converted).pk)
        # Closed with a known stage: a creation row and a closing row.
        rows = self.eng(converted).stage_changes.order_by("changed_at", "from_status")
        self.assertEqual([(r.from_status, r.to_status) for r in rows], [("", "open"), ("open", "won")])
        # Closed with no stage: one row straight to the outcome.
        rows = self.eng(stage_won).stage_changes.all()
        self.assertEqual([(r.from_status, r.to_status, r.to_stage_id) for r in rows],
                         [("", "won", None)])

    def test_dirty_stages_map_by_the_table(self):
        raw = {"Closed Won": ("won", None), "closedlost": ("lost", None),
               " Negotiating ": ("open", "negotiation"), "Proposal-Sent": ("open", "proposal_sent"),
               "qualified": ("open", "qualification"), "VERBAL": ("open", "verbal_commit")}
        quotes = {value: self.legacy(status="sent", sales_stage=value) for value in raw}
        summary = self.run_backfill()
        self.assertEqual(summary.unmapped, {})
        for value, (status, stage) in raw.items():
            eng = self.eng(quotes[value])
            with self.subTest(raw=value):
                self.assertEqual((eng.status, eng.stage.key if eng.stage_id else None), (status, stage))

    def test_unmapped_stage_refuses_and_writes_nothing(self):
        self.legacy(status="sent", sales_stage="Demo booked")
        self.legacy(status="sent", sales_stage="negotiation")
        with self.assertRaises(UnmappedStagesError) as ctx:
            self.run_backfill()
        self.assertIn("'Demo booked' x1", str(ctx.exception))
        self.assertIn(backfill.ACCEPT_UNMAPPED_ENV, str(ctx.exception))
        self.assertFalse(Engagement.objects.exists())
        self.assertFalse(Quote.objects.filter(engagement__isnull=False).exists())

    def test_accept_unmapped_open_starts_at_first_stage_closed_keeps_null(self):
        open_quote = self.legacy(status="sent", sales_stage="Demo booked")
        lost_quote = self.legacy(status="lost", sales_stage="Demo booked")
        summary = self.run_backfill(accept_unmapped=True)
        self.assertEqual(summary.unmapped["Demo booked"], 2)
        eng = self.eng(open_quote)
        self.assertEqual((eng.status, eng.stage.key), ("open", "new"))
        self.assertEqual(eng.metadata["unmapped_stage"], "Demo booked")
        eng = self.eng(lost_quote)
        self.assertEqual((eng.status, eng.stage_id, eng.loss_reason), ("lost", None, "unknown"))
        self.assertEqual(eng.metadata["unmapped_stage"], "Demo booked")

    def test_untouched_drafts_are_left_for_ensure_for_quote(self):
        draft = self.legacy(status="draft")
        summary = self.run_backfill()
        self.assertEqual((summary.untouched, sum(summary.created.values())), (1, 0))
        self.assertIsNone(self.eng(draft))
        # A draft with any pipeline data is backfilled.
        repped = self.legacy(status="draft", sales_rep=self.rep)
        self.run_backfill()
        eng = self.eng(repped)
        self.assertEqual((eng.owner_id, eng.status), (self.rep.pk, "open"))

    def test_idempotent_and_attaches_new_revisions(self):
        v1, v2 = self.chain("superseded", "sent")
        first = self.run_backfill()
        counts = (Engagement.objects.count(), EngagementStageChange.objects.count())
        second = self.run_backfill()
        self.assertEqual((sum(first.created.values()), sum(second.created.values())), (1, 0))
        self.assertEqual(second.already_linked, 1)
        self.assertEqual((Engagement.objects.count(), EngagementStageChange.objects.count()), counts)
        # A revision written around save() later is attached, not a new thread.
        v3 = self.legacy(status="sent", revision_number=3, original_quote=v1)
        third = self.run_backfill()
        self.assertEqual((third.attached_quotes, sum(third.created.values())), (1, 0))
        self.assertEqual(self.eng(v3).pk, self.eng(v2).pk)

    def test_reverse_removes_what_it_created(self):
        order = Order.objects.create(customer=self.customer, company=self.company)
        quote = self.legacy(status="sent", converted_to_order=order, follow_up_date=date(2026, 1, 5))
        open_quote = self.legacy(status="sent", follow_up_date=date(2026, 1, 5))
        self.run_backfill()
        self.assertEqual(Task.objects.count(), 1)
        kept = ensure_for_quote(Quote.objects.get(pk=self.legacy(status="draft").pk))
        removed = reverse_quote_engagements()
        self.assertEqual(removed, 2)
        self.assertEqual(list(Engagement.objects.all()), [kept])  # origin=quote survives
        self.assertFalse(Task.objects.exists())
        self.assertFalse(EngagementStageChange.objects.exclude(engagement=kept).exists())
        for row in (quote, open_quote):
            self.assertIsNone(self.eng(row))
        order.refresh_from_db()
        self.assertIsNone(order.engagement_id)
        # ...and forward again.
        self.assertEqual(sum(self.run_backfill().created.values()), 2)

    def test_reverse_refuses_once_people_worked_on_the_thread(self):
        quote = self.legacy(status="sent", sales_stage="qualification")
        self.run_backfill()
        move(self.eng(quote), self.pipeline.stages.get(key="negotiation"))
        with self.assertRaises(backfill.ReverseRefused):
            reverse_quote_engagements()
        with self.assertRaises(CommandError):
            call_command("backfill_crm", "--step", "5", "--reverse", stdout=io.StringIO())
        self.assertEqual(Engagement.objects.count(), 1)
        call_command("backfill_crm", "--step", "5", "--reverse", "--force", stdout=io.StringIO())
        self.assertFalse(Engagement.objects.exists())

    def test_a_chain_never_spans_tenants(self):
        mine = self.legacy(status="sent")
        theirs = self.legacy(status="sent", company=self.other_company,
                             customer=self.other_customer, original_quote=mine, revision_number=2)
        self.run_backfill()
        self.assertNotEqual(self.eng(mine).pk, self.eng(theirs).pk)
        self.assertEqual(self.eng(theirs).company_id, self.other_company.pk)

    def test_follow_up_date_becomes_the_next_action(self):
        rep = profile(self.company, "nyc", timezone="America/New_York")
        quote = self.legacy(status="sent", sales_rep=rep, follow_up_date=date(2026, 3, 9))
        self.run_backfill()
        eng = self.eng(quote)
        task = Task.objects.get(engagement=eng)
        self.assertEqual((task.kind, task.status, task.assignee_id, task.source),
                         ("follow_up", "open", rep.pk, backfill.TASK_SOURCE))
        self.assertEqual(task.due_tz, "America/New_York")
        self.assertEqual(eng.next_task_id, task.pk)
        self.assertEqual(eng.next_action_due_at, task.due_at)
        with self.assertWarns(DeprecationWarning):
            self.assertEqual(Quote.objects.get(pk=quote.pk).follow_up_date, date(2026, 3, 9))
        out = io.StringIO()
        call_command("reconcile_engagements", "--check", stdout=out)
        self.assertIn("drift: 0", out.getvalue())

    def test_legacy_values_are_kept_in_metadata(self):
        quote = self.legacy(status="sent", campaign="spring", lead_source="Trade Show",
                            deal_size_category="large", win_probability=40,
                            weighted_value=Decimal("12.50"), grand_total=Decimal("100.00"))
        self.run_backfill()
        eng = self.eng(quote)
        legacy = eng.metadata["quote_legacy"]
        self.assertEqual((legacy["campaign"], legacy["deal_size_category"], legacy["weighted_value"]),
                         ("spring", "large", "12.50"))
        self.assertEqual((eng.probability, eng.expected_value, eng.source),
                         (40, Decimal("100.00"), "trade_show"))

    def test_dry_run_writes_nothing_and_lists_raw_values(self):
        self.legacy(status="sent", sales_stage="Demo booked")
        self.legacy(status="sent", sales_stage="negotiation")
        out = io.StringIO()
        call_command("backfill_crm", "--step", "5", "--dry-run", stdout=out)
        self.assertIn("'Demo booked' x1", out.getvalue())
        self.assertIn("'negotiation' x1", out.getvalue())
        self.assertFalse(Engagement.objects.exists())
        with self.assertRaises(CommandError):
            call_command("backfill_crm", "--step", "5", stdout=io.StringIO())
        call_command("backfill_crm", "--step", "5", "--accept-unmapped", stdout=io.StringIO())
        self.assertEqual(Engagement.objects.count(), 2)

    def test_migration_reads_the_accept_switch(self):
        migration = importlib.import_module("crm.migrations.0010_backfill_quote_engagements")
        self.legacy(status="sent", sales_stage="Demo booked")
        with mock.patch("builtins.print"):
            with self.assertRaises(UnmappedStagesError):
                migration.forwards(live_apps, None)
            with mock.patch.dict(os.environ, {backfill.ACCEPT_UNMAPPED_ENV: "1"}):
                migration.forwards(live_apps, None)
            self.assertEqual(Engagement.objects.count(), 1)
            migration.backwards(live_apps, None)
        self.assertFalse(Engagement.objects.exists())
        with override_settings(GOLDEN_CRM_BACKFILL_ACCEPT_UNMAPPED=True):
            self.assertTrue(backfill.accept_unmapped_default())


class EnsureForQuoteTests(_QuoteFixtures):
    def create(self, **fields):
        fields.setdefault("company", self.company)
        fields.setdefault("customer", self.customer)
        return Quote.objects.create(**fields)

    def test_new_quote_gets_an_engagement(self):
        quote = self.create(sales_rep=self.rep, grand_total=Decimal("80.00"))
        eng = quote.engagement
        self.assertEqual((eng.origin, eng.status, eng.stage.key), ("quote", "open", "qualification"))
        self.assertEqual((eng.party_id, eng.owner_id), (self.customer.party_id, self.rep.pk))
        self.assertEqual(eng.expected_value, Decimal("80.00"))
        self.assertIn(quote.quote_number, eng.title)
        self.assertEqual(eng.stage_changes.get().source, "system")
        sent = self.create(status="sent")
        self.assertEqual(sent.engagement.stage.key, "proposal_sent")

    def test_revision_stays_on_its_chain(self):
        v1 = self.create()
        v2 = self.create(original_quote=v1, revision_number=2)
        self.assertEqual(v2.engagement_id, v1.engagement_id)
        self.assertEqual(Engagement.objects.count(), 1)

    def test_revision_never_joins_another_tenants_engagement(self):
        theirs = Quote.objects.create(company=self.other_company, customer=self.other_customer)
        with self.assertRaises(ValidationError):
            self.create(original_quote=theirs, revision_number=2)
        self.assertEqual(Engagement.objects.filter(company=self.company).count(), 0)

    def test_duplicated_order_leaves_the_thread(self):
        from orders.duplicate import duplicate_order

        quote = self.create(status="sent")
        QuoteLineItem.objects.create(quote=quote, item_name="W", sku="W-1", quantity=Decimal("1"),
                                     unit_price_ex_tax=Decimal("5.00"))
        order, _ = convert_quote_to_order(quote)
        self.assertIsNone(duplicate_order(order).engagement_id)

    def test_revision_of_a_legacy_original_ensures_the_original(self):
        original = self.legacy(status="draft")
        revision = self.create(original_quote=original, revision_number=2)
        self.assertEqual(self.eng(original).pk, revision.engagement_id)

    def test_duplicate_starts_a_new_engagement(self):
        source = self.create(status="sent")
        copy = duplicate_quote(source)
        self.assertNotEqual(copy.engagement_id, source.engagement_id)
        self.assertEqual(Engagement.objects.count(), 2)
        # ...unless the caller names the thread.
        same = duplicate_quote(source, engagement=source.engagement)
        self.assertEqual(same.engagement_id, source.engagement_id)
        other = Quote.objects.create(company=self.other_company, customer=self.other_customer)
        with self.assertRaises(ValidationError):
            duplicate_quote(source, engagement=other.engagement)

    def test_duplicate_does_not_copy_legacy_pipeline_values(self):
        source = self.legacy(status="lost", sales_stage="negotiation", loss_reason="price",
                             campaign="spring")
        copy = duplicate_quote(source)
        self.assertEqual((copy.legacy_sales_stage, copy.legacy_loss_reason, copy.legacy_campaign),
                         ("prospecting", "", ""))
        self.assertEqual(copy.engagement.status, "open")

    def test_legacy_quote_ensured_lazily_on_save_with_its_outcome(self):
        lost = self.legacy(status="lost", loss_reason="budget")
        expired = self.legacy(status="expired")
        accepted = self.legacy(status="accepted")
        for quote in (lost, expired, accepted):
            Quote.objects.get(pk=quote.pk).save(update_fields=["internal_notes", "updated_at"])
        self.assertEqual((self.eng(lost).status, self.eng(lost).loss_reason), ("lost", "budget"))
        self.assertEqual(self.eng(expired).loss_reason, "expired")
        self.assertEqual(self.eng(accepted).status, "won")

    def test_company_without_a_pipeline_is_seeded(self):
        company = Company.objects.create(name="Bare")
        Engagement.objects.filter(company=company).delete()
        for model in ("Stage", "Pipeline"):
            live_apps.get_model("crm", model).objects.filter(company=company).delete()
        customer = Customer.objects.create(first_name="B", last_name="C", email="b@example.com",
                                           company=company)
        quote = Quote.objects.create(company=company, customer=customer)
        self.assertEqual(quote.engagement.pipeline.key, "sales")

    def test_conversion_wins_and_the_order_joins_the_thread(self):
        quote = self.create(status="sent")
        sibling = self.create(original_quote=quote, revision_number=2)
        QuoteLineItem.objects.create(quote=quote, item_name="W", sku="W-1", quantity=Decimal("1"),
                                     unit_price_ex_tax=Decimal("5.00"))
        order, _ = convert_quote_to_order(quote)
        quote.refresh_from_db()
        self.assertEqual((quote.engagement.status, order.engagement_id),
                         ("won", quote.engagement_id))
        self.assertEqual(Order.objects.get(pk=order.pk).engagement_id, quote.engagement_id)
        sibling.refresh_from_db()
        self.assertEqual(sibling.status, "draft")  # nothing else is cancelled for the user
        # Converting an already-won thread's other quote does not fail.
        order2, _ = convert_quote_to_order(sibling)
        self.assertEqual(order2.engagement_id, quote.engagement_id)

    def test_conversion_keeps_a_lost_thread_lost(self):
        quote = self.create(status="sent")
        lose(quote.engagement, "price")
        convert_quote_to_order(quote)
        self.assertEqual(self.eng(quote).status, "lost")


class CompatPropertyTests(_QuoteFixtures):
    def quote(self, **fields):
        return Quote.objects.create(company=self.company, customer=self.customer, **fields)

    def read(self, quote, name):
        with self.assertWarns(DeprecationWarning):
            return getattr(Quote.objects.get(pk=quote.pk), name)

    def test_sales_stage_maps_back_to_the_old_vocabulary(self):
        quote = self.quote()
        self.assertEqual(self.read(quote, "sales_stage"), "qualification")
        move(quote.engagement, quote.engagement.pipeline.stages.get(key="new"))
        self.assertEqual(self.read(quote, "sales_stage"), "prospecting")  # not in the old enum
        move(quote.engagement, quote.engagement.pipeline.stages.get(key="negotiation"))
        self.assertEqual(self.read(quote, "sales_stage"), "negotiation")
        lose(quote.engagement, "price")
        self.assertEqual(self.read(quote, "sales_stage"), "closed_lost")
        self.assertEqual(self.read(quote, "loss_reason"), "price")

    def test_probability_and_close_date(self):
        quote = self.quote()
        Engagement.objects.filter(pk=quote.engagement_id).update(
            expected_close_date=date(2026, 12, 1))
        self.assertEqual(self.read(quote, "win_probability"), 20)  # qualification default
        self.assertEqual(self.read(quote, "estimated_close_date"), date(2026, 12, 1))
        self.assertIsNone(self.read(quote, "follow_up_date"))
        self.assertEqual(self.read(quote, "loss_reason"), "")

    def test_untouched_legacy_quote_reads_its_column(self):
        quote = self.legacy(status="draft", follow_up_date=date(2026, 2, 2))
        self.assertEqual(self.read(quote, "sales_stage"), "prospecting")
        self.assertEqual(self.read(quote, "follow_up_date"), date(2026, 2, 2))

    def test_properties_are_read_only(self):
        with self.assertRaises(AttributeError):
            Quote(company=self.company, customer=self.customer, sales_stage="negotiation")

    def test_compat_value_does_not_warn(self):
        quote = self.quote()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self.assertEqual(quote.compat_value("sales_stage"), "qualification")


class QuoteApiCompatTests(_QuoteFixtures):
    def setUp(self):
        self.client = APIClient()
        user = make_staff_user("quotes-api", "q@example.com", "pw")
        UserProfile.objects.create(user=user, company=self.company)
        self.client.force_authenticate(user=user)
        self.quote = Quote.objects.create(company=self.company, customer=self.customer)

    def url(self, quote=None):
        return reverse("quote-detail", args=[(quote or self.quote).pk])

    def test_detail_keeps_compat_keys_and_adds_engagement(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["engagement"], str(self.quote.engagement_id))
        self.assertEqual(body["sales_stage"], "qualification")
        self.assertEqual(body["win_probability"], 20)
        self.assertFalse([k for k in body if k.startswith("legacy_")])
        for gone in ("weighted_value", "campaign", "lead_source", "lost_at"):
            self.assertNotIn(gone, body)
        self.assertEqual(response["Deprecation"], "true")
        self.assertIn('rel="deprecation"', response["Link"])

    def test_writes_to_moved_fields_are_400_moved_to_engagement(self):
        for payload in ({"sales_stage": "negotiation"}, {"weighted_value": "1.00"},
                        {"follow_up_date": "2026-01-01", "status_note": "x"}):
            response = self.client.patch(self.url(), payload, format="json")
            with self.subTest(payload=payload):
                self.assertEqual(response.status_code, 400)
                body = response.json()
                self.assertEqual(body["code"], "moved_to_engagement")
                self.assertEqual(body["engagement"], str(self.quote.engagement_id))
                self.assertTrue(body["engagement_url"])
        response = self.client.post(
            reverse("quote-list"),
            {"customer": str(self.customer.pk), "company": str(self.company.pk),
             "win_probability": 50},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "moved_to_engagement")

    def test_api_refuses_a_revision_of_another_tenants_quote(self):
        theirs = Quote.objects.create(company=self.other_company, customer=self.other_customer)
        response = self.client.post(
            reverse("quote-list"),
            {"customer": str(self.customer.pk), "original_quote": str(theirs.pk),
             "revision_number": 2},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("original_quote", response.json())

    def test_sales_stage_filter_translates_to_the_engagement(self):
        won = Quote.objects.create(company=self.company, customer=self.customer, status="sent")
        from crm.engagements import win

        win(won.engagement)
        listed = self.client.get(reverse("quote-list"), {"sales_stage": "closed_won"}).json()
        self.assertEqual([row["id"] for row in listed["results"]], [str(won.pk)])
        self.assertEqual(listed["results"][0]["sales_stage"], "closed_won")
        listed = self.client.get(reverse("quote-list"), {"sales_stage": "qualification"}).json()
        self.assertEqual([row["id"] for row in listed["results"]], [str(self.quote.pk)])


class ReconcileQuotesTests(_QuoteFixtures):
    def test_check_flags_quotes_without_an_engagement_and_fix_ensures(self):
        root = self.legacy(status="draft")
        revision = self.legacy(status="draft", original_quote=root, revision_number=2)
        with self.assertRaises(CommandError):
            call_command("reconcile_engagements", "--check", stdout=io.StringIO())
        out = io.StringIO()
        call_command("reconcile_engagements", "--fix", stdout=out)
        self.assertIn("Fixed: 0 engagement(s), 2 quote(s)", out.getvalue())
        self.assertIsNotNone(self.eng(root))
        self.assertEqual(self.eng(revision).pk, self.eng(root).pk)
        call_command("reconcile_engagements", "--check", stdout=io.StringIO())
        call_command("check_engagements", stdout=io.StringIO())

    def test_revision_off_its_chain_is_reported_not_moved(self):
        root = Quote.objects.create(company=self.company, customer=self.customer)
        stray = Quote.objects.create(company=self.company, customer=self.customer)
        Quote.objects.filter(pk=stray.pk).update(original_quote=root)
        out = io.StringIO()
        with self.assertRaises(CommandError):
            call_command("reconcile_engagements", "--check", stdout=out)
        self.assertIn("revision on another engagement than its original", out.getvalue())

    def test_clean_after_normal_flows(self):
        quote = Quote.objects.create(company=self.company, customer=self.customer, status="sent")
        Quote.objects.create(company=self.company, customer=self.customer, original_quote=quote,
                             revision_number=2)
        duplicate_quote(quote)
        out = io.StringIO()
        call_command("reconcile_engagements", "--check", stdout=out)
        self.assertIn("quote without an engagement: 0", out.getvalue())

    def test_check_tenancy_flags_cross_tenant_quote(self):
        quote = Quote.objects.create(company=self.company, customer=self.customer)
        other = Quote.objects.create(company=self.other_company, customer=self.other_customer)
        Quote.objects.filter(pk=quote.pk).update(engagement=other.engagement)
        out = io.StringIO()
        with self.assertRaises(CommandError):
            call_command("check_tenancy", "--app", "orders", stdout=out)
        self.assertIn("orders.Quote: 1 row(s) whose company differs from engagement.company",
                      out.getvalue())


class ReportedAgeTests(_QuoteFixtures):
    """Backfilled timestamps come from the quotes, not from the run."""

    def test_closed_at_and_activity_from_the_chain(self):
        lost_at = timezone.now() - timedelta(days=30)
        quote = self.legacy(status="lost", lost_at=lost_at)
        self.run_backfill()
        eng = self.eng(quote)
        self.assertEqual(eng.closed_at, lost_at)
        self.assertEqual(eng.last_activity_at, quote.updated_at)
