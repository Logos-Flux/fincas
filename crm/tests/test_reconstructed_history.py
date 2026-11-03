"""Reconstructed engagement history is disclosed (release harness R11.03 and
R11.05: ``R11-history-no-reconstructed-marker``, ``R11-system-history-not-disclosed``).

A legacy quote ensured lazily gets a ``backfill`` creation row (it is the
deferred end of the step-5 backfill, not an observed change); the
engagement's timeline marks backfill and import rows; the stage report's
footnote counts them; and ``reconcile_engagements --fix`` relabels the rows
an older ``ensure_for_quote`` wrote as actor-less ``system`` rows.
"""

from __future__ import annotations

import io
from datetime import timedelta

from django.core.management import CommandError, call_command
from django.urls import reverse
from django.utils import timezone

from core.models import UserProfile
from core.tests.helpers import make_staff_user
from crm import engagements
from crm.engagements import ensure_for_quote
from crm.models import Engagement, EngagementStageChange
from orders.models import Quote

from .test_quote_engagements import _QuoteFixtures

SYSTEM = EngagementStageChange.Source.SYSTEM
BACKFILL = EngagementStageChange.Source.BACKFILL


class _Fixtures(_QuoteFixtures):
    def creation_row(self, eng):
        return eng.stage_changes.filter(from_stage__isnull=True, from_status="").get()

    def old_shape(self, days_ago=30):
        """The rows the pre-2026-10-17 ensure wrote for a legacy quote: an
        actor-less ``system`` creation row noting the quote, written long
        after the quote. Returns ``(quote, engagement)``."""
        quote = self.legacy(status="draft")
        then = timezone.now() - timedelta(days=days_ago)
        Quote._base_manager.filter(pk=quote.pk).update(created_at=then)
        eng = engagements.create(
            company=self.company, party=self.customer.party, pipeline=self.pipeline,
            source=SYSTEM, origin=Engagement.Origin.QUOTE, note=f"Quote {quote.quote_number}",
        )
        Quote._base_manager.filter(pk=quote.pk).update(engagement=eng)
        return quote, eng

    def staff_client(self):
        user = make_staff_user("hist-viewer", password="pw")
        UserProfile.objects.update_or_create(user=user, defaults={"company": self.company})
        self.client.force_login(user)
        return user


class LazyEnsureWritesBackfillTests(_Fixtures):
    def test_a_legacy_quote_ensured_lazily_gets_a_backfill_creation_row(self):
        quote = self.legacy(status="draft")
        eng = ensure_for_quote(quote)
        row = self.creation_row(eng)
        self.assertEqual(row.source, BACKFILL)
        self.assertIsNone(row.changed_by_id)
        self.assertTrue(row.is_reconstructed)

    def test_a_new_quote_gets_an_observed_system_row(self):
        quote = Quote.objects.create(company=self.company, customer=self.customer)
        row = self.creation_row(quote.engagement)
        self.assertEqual(row.source, SYSTEM)
        self.assertFalse(row.is_reconstructed)

    def test_a_decided_legacy_quote_closes_with_a_backfill_row_too(self):
        quote = self.legacy(status="accepted")
        eng = ensure_for_quote(quote)
        self.assertEqual(set(eng.stage_changes.values_list("source", flat=True)), {BACKFILL})


class TimelineMarkerTests(_Fixtures):
    def test_reconstructed_rows_are_marked_on_the_engagement_page(self):
        quote = self.legacy(status="draft")
        eng = ensure_for_quote(quote)
        Quote._base_manager.filter(pk=quote.pk).update(engagement=eng)
        engagements.move(eng, self.pipeline.stages.get(key="proposal_sent"), by=self.rep, source="erp")
        self.staff_client()
        response = self.client.get(reverse("erp-crm-engagement-detail", args=[eng.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "reconstructed · backfill", count=1)
        self.assertContains(response, "2 changes")


class ReconcileRelabelsLazyRowsTests(_Fixtures):
    def test_check_reports_fix_relabels_and_is_idempotent(self):
        quote, eng = self.old_shape()
        fresh = Quote.objects.create(company=self.company, customer=self.customer)  # observed: untouched
        out = io.StringIO()
        with self.assertRaises(CommandError) as ctx:
            call_command("reconcile_engagements", "--check", stdout=out)
        self.assertIn("1 reconstructed history row(s) written as system", str(ctx.exception))
        out = io.StringIO()
        call_command("reconcile_engagements", "--fix", stdout=out)
        self.assertIn("1 reconstructed history row(s) relabelled", out.getvalue())
        self.assertEqual(self.creation_row(eng).source, BACKFILL)
        self.assertEqual(self.creation_row(fresh.engagement).source, SYSTEM)
        out = io.StringIO()
        call_command("reconcile_engagements", "--fix", stdout=out)
        self.assertIn("0 reconstructed history row(s) relabelled", out.getvalue())
        call_command("reconcile_engagements", "--check", stdout=io.StringIO())

    def test_a_system_row_written_with_the_quote_is_not_touched(self):
        quote, eng = self.old_shape(days_ago=0)  # the quote and its row are seconds apart
        call_command("reconcile_engagements", "--check", stdout=io.StringIO())
        self.assertEqual(self.creation_row(eng).source, SYSTEM)


class StageReportDisclosesTests(_Fixtures):
    def test_the_footnote_counts_the_relabelled_engagement(self):
        self.old_shape()
        self.staff_client()
        url = reverse("erp-report-crm-stages")
        before = self.client.get(url)
        self.assertEqual(before.context["no_history_excluded"], 0)  # the bug: system reads as observed
        call_command("reconcile_engagements", "--fix", stdout=io.StringIO())
        after = self.client.get(url)
        self.assertEqual(after.context["no_history_excluded"], 1)
        self.assertContains(after, 'data-testid="no-history-footnote"')
