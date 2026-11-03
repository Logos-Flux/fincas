"""CRM Phase 3 step 6: the lead and ActivityLog backfills, the compat
activities endpoint and the reconcile checks (spec 1.7, 2.4, section 8).

Legacy rows are built with ``bulk_create``: no ``save()``, so no intake and no
frozen-ActivityLog refusal, the state the step-6 migrations find them in.
"""

from __future__ import annotations

import importlib
import io
import itertools
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.apps import apps as live_apps
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework import serializers as drf_serializers
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APIClient

from communications.models import Communication
from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from crm import engagements
from crm.backfill import (
    ReverseRefused,
    backfill_activity_communications,
    backfill_lead_engagements,
    backfill_quote_engagements,
    reverse_activity_communications,
    reverse_lead_engagements,
)
from crm.models import (
    ActivityLog,
    Engagement,
    EngagementStageChange,
    Lead,
    Pipeline,
    Task,
    TaskEvent,
    Touchpoint,
)
from customers.models import Customer
from customers.pre_tighten import PreTightenSchemaMixin
from orders.models import Quote

from .helpers import person, profile

_numbers = itertools.count(1)
T0 = timezone.make_aware(datetime(2026, 3, 2, 9, 30))


def legacy_lead(company, **fields):
    """A pre-step-6 lead (no intake ran)."""
    fields.setdefault("first_name", "Ada")
    fields.setdefault("last_name", "Lovelace")
    fields.setdefault("email", f"lead{next(_numbers)}@example.com")
    lead = Lead(company=company, lead_number=f"LD-L{next(_numbers):05d}", **fields)
    Lead.objects.bulk_create([lead])
    if "created_at" in fields or "updated_at" in fields:
        Lead.objects.filter(pk=lead.pk).update(
            created_at=fields.get("created_at", lead.created_at),
            updated_at=fields.get("updated_at", lead.updated_at))
        lead.refresh_from_db()
    return lead


def legacy_activity(**fields):
    fields.setdefault("activity_type", "note")
    fields.setdefault("subject", "Called")
    fields.setdefault("activity_date", T0)
    row = ActivityLog(**fields)
    ActivityLog.objects.bulk_create([row])
    return row


def legacy_quote(company, customer, **fields):
    quote = Quote(company=company, customer=customer, quote_number=f"QT-L{next(_numbers):05d}", **fields)
    Quote.objects.bulk_create([quote])
    return quote


class _Step6(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other_company = Company.objects.create(name="Tenant B")
        cls.pipeline = Pipeline.objects.get(company=cls.company, key="sales")
        cls.ada = person(cls.company)
        cls.bob = person(cls.company, "Bob", "Builder")
        cls.rex = person(cls.other_company, "Rex", "Rival")
        cls.rep = profile(cls.company, "rep")
        cls.customer = Customer.objects.create(
            first_name="Cus", last_name="Tomer", email="cus@example.com", company=cls.company,
        )

    def stage(self, key):
        return self.pipeline.stages.get(key=key)


class LeadBackfillTests(_Step6):
    def test_each_status_becomes_the_matching_engagement(self):
        cases = {
            "new": ("open", "new", ""),
            "contacted": ("open", "prospecting", ""),
            "qualified": ("open", "qualification", ""),
            "proposal_sent": ("open", "proposal_sent", ""),
            "converted": ("won", None, ""),
            "closed_junk": ("lost", None, "disqualified"),
        }
        leads = {status: legacy_lead(self.company, status=status, created_at=T0) for status in cases}
        summary = backfill_lead_engagements()
        self.assertEqual(sum(summary.created.values()), len(cases))
        for status, (eng_status, stage, reason) in cases.items():
            with self.subTest(status=status):
                lead = Lead.objects.get(pk=leads[status].pk)
                eng = lead.engagement
                self.assertEqual(eng.status, eng_status)
                self.assertEqual(eng.stage.key if eng.stage_id else None, stage)
                self.assertEqual(eng.loss_reason, reason)
                self.assertEqual((eng.origin, eng.metadata["backfill"]["step"]), ("backfill", 6))
                self.assertEqual(eng.metadata["backfill"]["lead"], str(lead.pk))
                self.assertTrue(eng.number.startswith("EN-"))
                self.assertTrue(eng.stage_changes.filter(source="backfill").exists())
                if eng_status == "open":
                    task = eng.tasks.get()
                    self.assertEqual((task.status, task.source, task.due_at), ("open", "backfill:lead", None))
                    self.assertEqual(eng.next_task, task)
                else:
                    self.assertFalse(eng.tasks.exists())
                    self.assertIsNotNone(eng.closed_at)

    def test_a_lost_lead_keeps_a_known_reason_else_unknown(self):
        known = legacy_lead(self.company, status="closed_lost", loss_reason="Price", loss_notes="too dear")
        dirty = legacy_lead(self.company, status="closed_lost", loss_reason="went quiet")
        backfill_lead_engagements()
        known.refresh_from_db()
        dirty.refresh_from_db()
        self.assertEqual((known.engagement.loss_reason, known.engagement.loss_notes), ("price", "too dear"))
        self.assertEqual(dirty.engagement.loss_reason, "unknown")
        self.assertEqual(dirty.engagement.metadata["lead_legacy"]["loss_reason"], "went quiet")

    def test_engagement_fields_come_from_the_lead(self):
        lead = legacy_lead(self.company, status="qualified", party=self.ada, assigned_to=self.rep,
                           estimated_value=Decimal("950.00"), source="trade_show",
                           follow_up_date=date(2026, 11, 3), created_at=T0)
        backfill_lead_engagements()
        lead.refresh_from_db()
        eng = lead.engagement
        self.assertEqual((eng.party, eng.owner, eng.expected_value, eng.source),
                         (self.ada, self.rep, Decimal("950.00"), "trade_show"))
        self.assertEqual(eng.title, f"{self.ada.display_name} — {lead.lead_number}")
        task = eng.tasks.get()
        self.assertEqual((task.source, task.assignee, task.title),
                         ("backfill:lead.follow_up_date", self.rep, f"Follow up on {lead.lead_number}"))
        self.assertTrue(task.all_day)
        self.assertEqual(task.due_at.astimezone(timezone.get_default_timezone()).date(), date(2026, 11, 3))
        self.assertEqual(eng.stage_changes.get().changed_at, T0)

    def test_a_converted_lead_attaches_to_its_quotes_engagement(self):
        quote = legacy_quote(self.company, self.customer, status="sent")
        backfill_quote_engagements()
        quote.refresh_from_db()
        self.assertIsNotNone(quote.engagement_id)
        lead = legacy_lead(self.company, status="converted", converted_to_quote=quote, party=self.ada)
        summary = backfill_lead_engagements()
        lead.refresh_from_db()
        self.assertEqual(lead.engagement_id, quote.engagement_id)
        self.assertEqual((summary.attached_to_quote, sum(summary.created.values())), (1, 0))

    def test_a_converted_lead_waits_for_an_untouched_quote_then_reconcile_attaches_it(self):
        quote = legacy_quote(self.company, self.customer)  # an untouched draft: step 5 skips it
        backfill_quote_engagements()
        lead = legacy_lead(self.company, status="converted", converted_to_quote=quote)
        summary = backfill_lead_engagements()
        self.assertEqual(summary.waiting_on_quote, 1)
        lead.refresh_from_db()
        self.assertIsNone(lead.engagement_id)
        self.assertFalse(Communication.objects.filter(metadata__lead=str(lead.pk)).exists())
        call_command("reconcile_engagements", "--fix", stdout=io.StringIO())
        lead.refresh_from_db()
        quote.refresh_from_db()
        self.assertEqual(lead.engagement_id, quote.engagement_id)

    def test_repeat_open_enquiries_share_one_engagement(self):
        first = legacy_lead(self.company, status="new", party=self.ada, created_at=T0)
        second = legacy_lead(self.company, status="contacted", party=self.ada,
                             created_at=T0 + timedelta(days=2))
        closed = legacy_lead(self.company, status="closed_lost", party=self.ada,
                             created_at=T0 + timedelta(days=3))
        summary = backfill_lead_engagements()
        first.refresh_from_db()
        second.refresh_from_db()
        closed.refresh_from_db()
        self.assertEqual(first.engagement_id, second.engagement_id)
        self.assertNotEqual(closed.engagement_id, first.engagement_id)  # a closed lead is its own deal
        self.assertEqual(summary.attached_repeat, 1)
        self.assertEqual(Engagement.objects.get(pk=first.engagement_id).last_activity_at.date(),
                         first.engagement.last_activity_at.date())

    def test_an_open_lead_joins_the_partys_existing_open_engagement(self):
        deal = engagements.create(company=self.company, party=self.ada)
        lead = legacy_lead(self.company, status="new", party=self.ada,
                           created_at=timezone.now() + timedelta(minutes=5))
        backfill_lead_engagements()
        lead.refresh_from_db()
        self.assertEqual(lead.engagement, deal)
        deal.refresh_from_db()
        self.assertEqual(deal.last_activity_at, lead.created_at)

    def test_support_requests_and_companyless_leads_get_none(self):
        support = legacy_lead(self.company, status="new", request_type="support", party=self.ada,
                              message="It broke")
        orphan = legacy_lead(None, status="new")
        summary = backfill_lead_engagements()
        support.refresh_from_db()
        orphan.refresh_from_db()
        self.assertIsNone(support.engagement_id)
        self.assertIsNone(orphan.engagement_id)
        self.assertEqual((summary.support, summary.no_company), (1, 1))
        note = Communication.objects.get(metadata__lead=str(support.pk))
        self.assertEqual((note.kind, note.direction, note.engagement_id, note.party, note.body_text),
                         ("note", "internal", None, self.ada, "It broke"))

    def test_messages_and_touchpoints_are_written_as_intake_writes_them(self):
        lead = legacy_lead(self.company, status="new", party=self.ada, message="40 panels",
                           utm_source="google", utm_medium="cpc", referrer_url="https://s.example/",
                           created_at=T0)
        backfill_lead_engagements()
        lead.refresh_from_db()
        note = Communication.objects.get(metadata__lead=str(lead.pk))
        self.assertEqual((note.engagement, note.occurred_at, note.body_text, note.provider),
                         (lead.engagement, T0, "40 panels", "lead_form"))
        touch = Touchpoint.objects.get()
        self.assertEqual((touch.source, touch.engagement, touch.party, touch.occurred_at),
                         ("backfill:lead", lead.engagement, self.ada, T0))
        self.assertEqual((lead.engagement.first_touch, lead.engagement.last_touch), (touch, touch))

    def test_cross_tenant_party_and_owner_are_dropped(self):
        stranger = profile(self.other_company, "stranger")
        lead = legacy_lead(self.company, status="new", party=self.rex, assigned_to=stranger)
        backfill_lead_engagements()
        lead.refresh_from_db()
        self.assertIsNone(lead.engagement.party_id)
        self.assertIsNone(lead.engagement.owner_id)
        call_command("check_tenancy", "--app", "crm", stdout=io.StringIO())

    def test_idempotent_with_summary_lines(self):
        legacy_lead(self.company, status="new")
        legacy_lead(self.company, status="converted")
        first = backfill_lead_engagements()
        self.assertEqual(first.lines()[1], "engagements created: 2 (open 1, won 1)")
        again = backfill_lead_engagements()
        self.assertEqual((again.leads, sum(again.created.values())), (0, 0))
        self.assertEqual(Engagement.objects.count(), 2)

    def test_support_leads_are_written_once_across_runs(self):
        backfilled = legacy_lead(self.company, status="new", request_type="support", utm_source="x")
        live = Lead.objects.create(company=self.company, first_name="Liv", last_name="E",
                                   email="live@example.com", request_type="support", utm_source="y")
        for _ in range(3):
            backfill_lead_engagements()
        for lead in (backfilled, live):
            self.assertEqual(Communication.objects.filter(metadata__lead=str(lead.pk)).count(), 1)
            self.assertEqual(Touchpoint.objects.filter(metadata__lead=str(lead.pk)).count(), 1)

    def test_another_tenants_quote_never_threads_a_lead(self):
        other_customer = Customer.objects.create(first_name="O", last_name="T", email="ot@example.com",
                                                 company=self.other_company)
        theirs = legacy_quote(self.other_company, other_customer, status="sent")
        backfill_quote_engagements()
        theirs.refresh_from_db()
        lead = legacy_lead(self.company, status="converted", converted_to_quote=theirs)
        backfill_lead_engagements()
        lead.refresh_from_db()
        self.assertNotEqual(lead.engagement_id, theirs.engagement_id)
        self.assertEqual((lead.engagement.company, lead.engagement.status), (self.company, "won"))

    def test_dry_run_writes_nothing(self):
        legacy_lead(self.company, status="new", utm_source="x")
        summary = backfill_lead_engagements(dry_run=True)
        self.assertEqual(summary.created["open"], 1)
        self.assertFalse(Engagement.objects.exists())
        self.assertFalse(Communication.objects.exists())
        self.assertFalse(Touchpoint.objects.exists())

    def test_reverse_removes_what_it_created_and_unlinks_what_it_attached(self):
        deal = engagements.create(company=self.company, party=self.bob)
        attached = legacy_lead(self.company, status="new", party=self.bob,
                               created_at=timezone.now() + timedelta(minutes=5))
        created = legacy_lead(self.company, status="new", party=self.ada, utm_source="x")
        backfill_lead_engagements()
        self.assertEqual(reverse_lead_engagements(), 1)
        attached.refresh_from_db()
        created.refresh_from_db()
        self.assertIsNone(attached.engagement_id)
        self.assertIsNone(created.engagement_id)
        self.assertEqual(list(Engagement.objects.all()), [deal])
        self.assertFalse(Communication.objects.exists())
        self.assertFalse(Touchpoint.objects.exists())
        self.assertFalse(Task.objects.exists())
        # Forward again after reverse: the same outcome.
        backfill_lead_engagements()
        attached.refresh_from_db()
        self.assertEqual(attached.engagement, deal)

    def test_reverse_refuses_once_people_worked_the_thread(self):
        lead = legacy_lead(self.company, status="new")
        backfill_lead_engagements()
        lead.refresh_from_db()
        engagements.move(lead.engagement, self.stage("negotiation"))
        with self.assertRaises(ReverseRefused):
            reverse_lead_engagements()
        self.assertEqual(reverse_lead_engagements(force=True), 1)
        self.assertFalse(Engagement.objects.exists())

    def test_the_migration_runs_the_backfill(self):
        legacy_lead(self.company, status="new")
        module = importlib.import_module("crm.migrations.0012_backfill_lead_engagements")
        out = io.StringIO()
        from contextlib import redirect_stdout

        with redirect_stdout(out):
            module.forwards(live_apps, None)
        self.assertIn("crm step 6 (leads): engagements created: 1 (open 1)", out.getvalue())
        with redirect_stdout(out):
            module.backwards(live_apps, None)
        self.assertFalse(Engagement.objects.exists())


class CompanylessActivityBackfillTests(PreTightenSchemaMixin, _Step6):
    """A NULL-company activity exists only before CRM Phase 2 step 11."""

    def test_companyless_rows_are_skipped(self):
        orphan = legacy_activity(company=None)
        summary = backfill_activity_communications()
        self.assertEqual(summary.no_company, 1)
        self.assertFalse(Communication.objects.filter(pk=orphan.pk).exists())


class ActivityBackfillTests(_Step6):
    def test_every_activity_type_lands_with_the_same_id(self):
        rows = {kind: legacy_activity(company=self.company, activity_type=kind, subject=f"{kind} it",
                                      description=f"about {kind}", performed_by=self.rep,
                                      duration_minutes=15, metadata={"src": kind})
                for kind in ("call", "email", "meeting", "note", "task")}
        summary = backfill_activity_communications()
        self.assertEqual((sum(summary.communications.values()), summary.tasks), (4, 1))
        for kind in ("call", "email", "meeting", "note"):
            with self.subTest(kind=kind):
                comm = Communication.objects.get(pk=rows[kind].pk)
                self.assertEqual(comm.kind, kind)
                self.assertEqual(comm.direction, "internal" if kind == "note" else "outbound")
                self.assertEqual((comm.status, comm.provider, comm.subject, comm.body_text),
                                 ("completed", "manual", f"{kind} it", f"about {kind}"))
                self.assertEqual((comm.occurred_at, comm.performed_by), (T0, self.rep))
                self.assertEqual(comm.duration_seconds, 900 if kind in ("call", "meeting") else None)
                self.assertEqual(comm.metadata["src"], kind)
                self.assertIs(comm.metadata["from_activitylog"], True)
                self.assertEqual(comm.metadata["activitylog"]["duration_minutes"], 15)
                self.assertEqual(comm.created_at, rows[kind].created_at)
                comm.full_clean(validate_unique=False)  # the registry accepts the copy
        task = Task.objects.get(pk=rows["task"].pk)
        self.assertEqual((task.status, task.completed_at, task.title, task.description),
                         ("done", T0, "task it", "about task"))
        self.assertEqual((task.assignee, task.completed_by, task.source),
                         (self.rep, self.rep, "backfill:activitylog"))
        self.assertEqual(TaskEvent.objects.get(task=task).event, "completed")

    def test_links_follow_the_lead_and_the_customer(self):
        lead = legacy_lead(self.company, status="new", party=self.ada)
        backfill_lead_engagements()
        lead.refresh_from_db()
        on_lead = legacy_activity(company=self.company, lead=lead, activity_type="call",
                                  activity_date=timezone.now() + timedelta(hours=1))
        party = self.customer.party
        one = engagements.create(company=self.company, party=party)
        on_customer = legacy_activity(company=self.company, customer=self.customer)
        backfill_activity_communications()
        comm = Communication.objects.get(pk=on_lead.pk)
        self.assertEqual((comm.engagement, comm.party), (lead.engagement, self.ada))
        lead.engagement.refresh_from_db()
        self.assertEqual(lead.engagement.last_activity_at, on_lead.activity_date)
        comm = Communication.objects.get(pk=on_customer.pk)
        self.assertEqual((comm.engagement, comm.party), (one, party))

    def test_a_customer_with_two_open_engagements_gets_party_only(self):
        party = self.customer.party
        engagements.create(company=self.company, party=party)
        engagements.create(company=self.company, party=party)
        row = legacy_activity(company=self.company, customer=self.customer)
        backfill_activity_communications()
        comm = Communication.objects.get(pk=row.pk)
        self.assertEqual((comm.engagement_id, comm.party), (None, party))

    def test_cross_tenant_links_are_dropped_and_companyless_rows_skipped(self):
        theirs = legacy_lead(self.other_company, status="new", party=self.rex)
        backfill_lead_engagements()
        cross = legacy_activity(company=self.company, lead=theirs)
        summary = backfill_activity_communications()
        comm = Communication.objects.get(pk=cross.pk)
        self.assertEqual((comm.engagement_id, comm.party_id), (None, None))
        self.assertEqual(summary.dropped_cross_tenant, 2)
        self.assertEqual(summary.no_company, 0)

    def test_idempotent_and_reversible(self):
        note = legacy_activity(company=self.company)
        task = legacy_activity(company=self.company, activity_type="task")
        backfill_activity_communications()
        again = backfill_activity_communications()
        self.assertEqual((again.already, sum(again.communications.values()), again.tasks), (2, 0, 0))
        reverse_activity_communications()
        self.assertFalse(Communication.objects.filter(pk=note.pk).exists())
        self.assertFalse(Task.objects.filter(pk=task.pk).exists())
        self.assertTrue(ActivityLog.objects.filter(pk=note.pk).exists())
        backfill_activity_communications()
        self.assertTrue(Communication.objects.filter(pk=note.pk).exists())

    def test_a_negative_or_missing_duration_never_breaks_the_copy(self):
        bad = legacy_activity(company=self.company, activity_type="call", duration_minutes=-5)
        backfill_activity_communications()
        comm = Communication.objects.get(pk=bad.pk)
        self.assertIsNone(comm.duration_seconds)
        self.assertEqual(comm.metadata["activitylog"]["duration_minutes"], -5)

    def test_the_migration_runs_the_backfill(self):
        legacy_activity(company=self.company)
        module = importlib.import_module("crm.migrations.0013_backfill_activity_communications")
        from contextlib import redirect_stdout

        out = io.StringIO()
        with redirect_stdout(out):
            module.forwards(live_apps, None)
        self.assertIn("communications: 1 (note 1)", out.getvalue())


class _LegacyActivitySerializer(drf_serializers.ModelSerializer):
    """ActivityLogSerializer as it was before step 6 (the snapshot's source)."""

    class Meta:
        model = ActivityLog
        fields = "__all__"


def _json(data):
    return json.loads(JSONRenderer().render(data))


class CompatEndpointTests(_Step6):
    def setUp(self):
        self.user = make_staff_user("compat", "c@example.com", "pw")
        UserProfile.objects.create(user=self.user, company=self.company)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.lead = legacy_lead(self.company, status="new", party=self.ada)
        backfill_lead_engagements()
        self.lead.refresh_from_db()
        self.rows = [
            legacy_activity(company=self.company, lead=self.lead, activity_type="call",
                            subject="Intro call", description="Talked", performed_by=self.rep,
                            duration_minutes=20, metadata={"k": 1},
                            activity_date=T0 + timedelta(hours=1)),
            legacy_activity(company=self.company, customer=self.customer, activity_type="email",
                            subject="Sent spec", activity_date=T0 + timedelta(hours=2)),
            legacy_activity(company=self.company, lead=self.lead, activity_type="meeting",
                            subject="Site visit", duration_minutes=None, metadata=[1, 2],
                            activity_date=T0 + timedelta(hours=3)),
            legacy_activity(company=self.company, activity_type="note", subject="Internal",
                            activity_date=T0 + timedelta(hours=4)),
            legacy_activity(company=self.company, lead=self.lead, activity_type="task",
                            subject="Sent samples", performed_by=self.rep,
                            activity_date=T0 + timedelta(hours=5)),
        ]
        self.theirs = legacy_activity(company=self.other_company, subject="Not yours")
        # The pre-migration payloads, rendered by the old serializer.
        self.snapshot = {str(r.pk): _json(_LegacyActivitySerializer(ActivityLog.objects.get(pk=r.pk)).data)
                         for r in self.rows}
        backfill_activity_communications()

    def test_list_and_detail_return_the_pre_migration_json(self):
        response = self.client.get("/api/v1/crm/activities/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Deprecation"], "true")
        results = _json(response.data)["results"]
        self.assertEqual([r["id"] for r in results],
                         [str(r.pk) for r in sorted(self.rows, key=lambda r: r.activity_date, reverse=True)])
        for item in results:
            with self.subTest(id=item["id"]):
                self.assertEqual(item, self.snapshot[item["id"]])
                detail = self.client.get(f"/api/v1/crm/activities/{item['id']}/")
                self.assertEqual(_json(detail.data), self.snapshot[item["id"]])

    def test_filters_search_and_ordering(self):
        def ids(query):
            return [r["id"] for r in self.client.get(f"/api/v1/crm/activities/?{query}").data["results"]]

        self.assertEqual(set(ids(f"lead={self.lead.pk}")),
                         {str(self.rows[i].pk) for i in (0, 2, 4)})
        self.assertEqual(ids(f"customer={self.customer.pk}"), [str(self.rows[1].pk)])
        self.assertEqual(ids("activity_type=task"), [str(self.rows[4].pk)])
        self.assertEqual(ids("activity_type=call"), [str(self.rows[0].pk)])
        self.assertEqual(set(ids(f"performed_by={self.rep.pk}")), {str(self.rows[0].pk), str(self.rows[4].pk)})
        self.assertEqual(ids("search=site"), [str(self.rows[2].pk)])
        self.assertEqual(ids("ordering=activity_date")[0], str(self.rows[0].pk))

    def test_another_tenants_activity_is_404_and_golden_emails_are_not_activities(self):
        self.assertEqual(self.client.get(f"/api/v1/crm/activities/{self.theirs.pk}/").status_code, 404)
        from communications.ingest import record

        record(self.company, "email", direction="outbound", occurred_at=timezone.now(), provider="golden")
        self.assertEqual(self.client.get("/api/v1/crm/activities/").data["count"], len(self.rows))

    def test_malformed_ids_and_filters_are_404_and_400(self):
        self.assertEqual(self.client.get("/api/v1/crm/activities/not-a-uuid/").status_code, 404)
        for query in ("performed_by=zzz", "lead=zzz", "customer=1", "activity_type=bogus"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"/api/v1/crm/activities/?{query}").status_code, 400)

    def test_lead_detail_reads_through_the_compat_serializer(self):
        response = self.client.get(f"/api/v1/crm/leads/{self.lead.pk}/")
        self.assertEqual(response.status_code, 200)
        got = _json(response.data["activities"])
        self.assertEqual([a["id"] for a in got], [str(self.rows[i].pk) for i in (4, 2, 0)])
        self.assertEqual(got[2], self.snapshot[str(self.rows[0].pk)])

    def test_writes_create_communications_or_tasks(self):
        response = self.client.post("/api/v1/crm/activities/", {
            "activity_type": "call", "subject": "Follow-up call", "lead": str(self.lead.pk),
            "activity_date": "2026-10-01T10:00:00Z", "duration_minutes": 5, "metadata": {"a": 1},
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        comm = Communication.objects.get(pk=response.data["id"])
        self.assertEqual((comm.kind, comm.engagement, comm.party, comm.duration_seconds),
                         ("call", self.lead.engagement, self.ada, 300))
        self.assertEqual(response.data["lead"], str(self.lead.pk))
        self.assertEqual(response.data["metadata"], {"a": 1})
        self.assertEqual(response.data["company"], str(self.company.pk))
        response = self.client.post("/api/v1/crm/activities/", {
            "activity_type": "task", "subject": "Mailed brochure", "lead": str(self.lead.pk),
            "activity_date": "2026-10-02T10:00:00Z", "performed_by": str(self.rep.pk),
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        task = Task.objects.get(pk=response.data["id"])
        self.assertEqual((task.status, task.engagement, task.completed_by), ("done", self.lead.engagement, self.rep))
        self.assertEqual(datetime.fromisoformat(response.data["activity_date"]),
                         datetime.fromisoformat("2026-10-02T10:00:00+00:00"))
        self.assertEqual(task.completed_at, datetime.fromisoformat("2026-10-02T10:00:00+00:00"))
        self.assertEqual(response.data["activity_type"], "task")
        self.assertFalse(ActivityLog.objects.filter(pk=response.data["id"]).exists())

    def test_patch_put_and_delete(self):
        call = self.rows[0]
        response = self.client.patch(f"/api/v1/crm/activities/{call.pk}/",
                                     {"subject": "Renamed", "duration_minutes": 30}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.data["subject"], response.data["duration_minutes"]), ("Renamed", 30))
        self.assertEqual(response.data["lead"], str(self.lead.pk))
        self.assertEqual(Communication.objects.get(pk=call.pk).duration_seconds, 1800)
        response = self.client.patch(f"/api/v1/crm/activities/{call.pk}/", {"activity_type": "meeting"},
                                     format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Communication.objects.get(pk=call.pk).kind, "meeting")
        response = self.client.patch(f"/api/v1/crm/activities/{call.pk}/", {"activity_type": "task"},
                                     format="json")
        self.assertEqual(response.status_code, 400)
        task = self.rows[4]
        response = self.client.put(f"/api/v1/crm/activities/{task.pk}/", {
            "activity_type": "task", "subject": "Samples again", "activity_date": "2026-10-03T08:00:00Z",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Task.objects.get(pk=task.pk).title, "Samples again")
        self.assertEqual(self.client.delete(f"/api/v1/crm/activities/{task.pk}/").status_code, 204)
        self.assertFalse(Task.objects.filter(pk=task.pk).exists())


class ReconcileStep6Tests(_Step6):
    def _check(self):
        out = io.StringIO()
        try:
            call_command("reconcile_engagements", "--check", stdout=out)
        except CommandError as exc:
            return str(exc), out.getvalue()
        return "", out.getvalue()

    def test_reports_and_fixes_leads_activities_and_emails(self):
        lead = legacy_lead(self.company, status="new")
        support = legacy_lead(self.company, status="new", request_type="support")
        row = legacy_activity(company=self.company)
        from communications.models import EmailLog

        quote = Quote.objects.create(company=self.company, customer=self.customer)
        log = EmailLog.objects.create(
            document_type="quote", document_id=quote.pk, document_number=quote.quote_number,
            recipient_email="cus@example.com", subject="Your quote", template_name="quote",
            status="sent", triggered_by="manual")
        problems, out = self._check()
        self.assertIn("1 lead(s) without an engagement", problems)
        self.assertIn("1 ActivityLog row(s) not mirrored", problems)
        self.assertIn("1 EmailLog(s) without a communication", problems)
        call_command("reconcile_engagements", "--fix", stdout=io.StringIO())
        self.assertEqual(self._check()[0], "")
        lead.refresh_from_db()
        support.refresh_from_db()
        self.assertIsNotNone(lead.engagement_id)
        self.assertIsNone(support.engagement_id)
        self.assertTrue(Communication.objects.filter(pk=row.pk).exists())
        self.assertEqual(Communication.objects.get(email_log=log).engagement, quote.engagement)

    def test_backfill_crm_step_6(self):
        legacy_lead(self.company, status="new")
        legacy_activity(company=self.company)
        out = io.StringIO()
        call_command("backfill_crm", "--step", "6", "--dry-run", stdout=out)
        self.assertIn("[dry run]", out.getvalue())
        self.assertFalse(Engagement.objects.exists())
        call_command("backfill_crm", "--step", "6", stdout=out)
        self.assertEqual(Engagement.objects.count(), 1)
        self.assertEqual(Communication.objects.filter(metadata__from_activitylog=True).count(), 1)
        call_command("backfill_crm", "--step", "6", "--reverse", stdout=out)
        self.assertFalse(Engagement.objects.exists())
        self.assertFalse(Communication.objects.exists())
        self.assertFalse(EngagementStageChange.objects.exists())
        with self.assertRaises(CommandError):
            call_command("backfill_crm", "--step", "6", "--accept-unmapped", stdout=out)


