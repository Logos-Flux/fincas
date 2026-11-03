"""CRM Phase 3 step 7: the engagement, task and communication lifecycle
signals and ``crm_emit_due`` (docs/crm/phase3-engagement.md 5.1).

Each signal fires once, after the emitting transaction commits, never on a
rollback; every payload the webhook layer builds from it is JSON; the step-5
and step-6 backfills and ``reconcile_engagements --fix`` send nothing.
"""

from __future__ import annotations

import datetime
import io
import json
from unittest import mock

from django.core.management import call_command
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from communications import ingest
from communications.models import Communication, EmailLog
from core.plugins import signals
from core.plugins.dispatch import emit, suppressed
from crm import due, engagements, tasks
from crm.backfill import (
    backfill_activity_communications,
    backfill_lead_engagements,
    backfill_quote_engagements,
)
from crm.models import ActivityLog, Engagement, Lead, Task
from customers.models import Customer
from orders.models import Quote
from webhooks import events

from .helpers import CrmTestCase, person, profile

CRM_SIGNALS = (
    "engagement_created", "engagement_stage_changed", "engagement_won", "engagement_lost",
    "engagement_reopened", "task_created", "task_assigned", "task_completed", "task_reopened",
    "task_due", "communication_logged",
)


class Counter:
    """Records every CRM lifecycle signal delivered while connected."""

    def __init__(self, test, *names):
        self.calls: list[tuple[str, dict]] = []
        for name in names or CRM_SIGNALS:
            uid = f"crm-counter.{name}.{id(self)}"
            getattr(signals, name).connect(self._receiver(name), weak=False, dispatch_uid=uid)
            test.addCleanup(getattr(signals, name).disconnect, dispatch_uid=uid)

    def _receiver(self, name):
        def receive(sender, signal=None, **kwargs):
            self.calls.append((name, kwargs))
        return receive

    def names(self):
        return [name for name, _ in self.calls]

    def only(self, name):
        rows = [kw for n, kw in self.calls if n == name]
        assert len(rows) == 1, f"{name} delivered {len(rows)} times: {self.names()}"
        return rows[0]


def webhook_payload(name, kwargs):
    """The webhook ``data`` each signal turns into (spec: ids, keys, timestamps)."""
    if name == "engagement_created":
        return events.engagement_payload(kwargs["engagement"])
    if name == "engagement_stage_changed":
        return events.engagement_stage_changed_payload(
            kwargs["engagement"], kwargs["from_stage"], kwargs["to_stage"], kwargs["change"])
    if name == "engagement_won":
        return events.engagement_closed_payload(kwargs["engagement"], kwargs["change"])
    if name == "engagement_lost":
        return events.engagement_closed_payload(kwargs["engagement"], kwargs["change"],
                                                loss_reason=kwargs["loss_reason"])
    if name == "engagement_reopened":
        return events.engagement_reopened_payload(kwargs["engagement"], kwargs["change"],
                                                  kwargs["previous_status"])
    if name.startswith("task_"):
        return events.task_payload(kwargs["task"])
    return events.communication_logged_payload(kwargs["communication"])


class _Base(CrmTestCase):
    def assertJsonPayloads(self, counter):
        for name, kwargs in counter.calls:
            with self.subTest(signal=name):
                json.dumps(webhook_payload(name, kwargs))


class EngagementSignalTests(_Base):
    def test_created_fires_once_after_commit_not_before(self):
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=False) as pending:
            eng = self.engagement(owner=self.rep)
            self.assertEqual(counter.calls, [])
        for callback in pending:
            callback()
        self.assertEqual(counter.names(), ["engagement_created"])
        self.assertIs(counter.only("engagement_created")["engagement"], eng)
        self.assertJsonPayloads(counter)

    def test_a_rolled_back_create_sends_nothing(self):
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.engagement()
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass
        self.assertEqual(counter.calls, [])

    def test_move_sends_stage_changed_once_and_a_no_op_sends_nothing(self):
        eng = self.engagement()
        from_stage = eng.stage
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True):
            engagements.move(eng, self.stage("proposal_sent"), by=self.rep, source="api")
        with self.captureOnCommitCallbacks(execute=True):
            engagements.move(eng, self.stage("proposal_sent"), by=self.rep)
        self.assertEqual(counter.names(), ["engagement_stage_changed"])
        kw = counter.only("engagement_stage_changed")
        self.assertEqual((kw["from_stage"], kw["to_stage"].key), (from_stage, "proposal_sent"))
        self.assertEqual(kw["change"].source, "api")
        self.assertJsonPayloads(counter)

    def test_change_pipeline_is_a_stage_change(self):
        from crm.models import Pipeline, Stage

        other = Pipeline.objects.create(company=self.company, key="renewals", name="Renewals")
        target = Stage.objects.create(pipeline=other, key="open", name="Open", sort_order=1)
        eng = self.engagement()
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True):
            engagements.change_pipeline(eng, other, target, by=self.rep)
        self.assertEqual(counter.names(), ["engagement_stage_changed"])
        payload = webhook_payload("engagement_stage_changed", counter.calls[0][1])
        self.assertEqual((payload["from_pipeline_key"], payload["pipeline_key"]), ("sales", "renewals"))

    def test_won_lost_reopened_each_fire_once(self):
        reopener = profile(self.company, "boss", perms=["crm.reopen_engagement"])
        eng = self.engagement()
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True):
            engagements.win(eng, by=self.rep)
        with self.captureOnCommitCallbacks(execute=True):
            engagements.win(eng, by=self.rep)  # no-op
        self.assertEqual(counter.names(), ["engagement_won"])
        with self.captureOnCommitCallbacks(execute=True):
            engagements.reopen(eng, by=reopener)
        kw = counter.only("engagement_reopened")
        self.assertEqual(kw["previous_status"], "won")
        with self.captureOnCommitCallbacks(execute=True):
            engagements.lose(eng, "price", by=self.rep, notes="too dear")
        self.assertEqual(counter.only("engagement_lost")["loss_reason"], "price")
        self.assertEqual(counter.names(), ["engagement_won", "engagement_reopened", "engagement_lost"])
        self.assertJsonPayloads(counter)

    def test_reopen_at_another_stage_also_reports_the_stage(self):
        reopener = profile(self.company, "boss", perms=["crm.reopen_engagement"])
        eng = self.engagement()
        engagements.lose(eng, "price", by=self.rep)
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True):
            engagements.reopen(eng, by=reopener, stage=self.stage("negotiation"))
        self.assertEqual(counter.names(), ["engagement_reopened", "engagement_stage_changed"])


class TaskSignalTests(_Base):
    def test_created_fires_once_after_commit(self):
        counter = Counter(self, "task_created", "task_assigned")
        with self.captureOnCommitCallbacks(execute=False) as pending:
            task = tasks.create(company=self.company, title="Call back", assignee=self.rep)
            self.assertEqual(counter.calls, [])
        for callback in pending:
            callback()
        # An assignee given at creation is part of task_created, not an assignment.
        self.assertEqual(counter.names(), ["task_created"])
        self.assertIs(counter.only("task_created")["task"], task)

    def test_assigned_completed_reopened(self):
        other = profile(self.company, "other")
        task = tasks.create(company=self.company, title="Call back", assignee=self.rep)
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True):
            tasks.assign(task, other, by=self.rep)
        self.assertEqual(counter.only("task_assigned")["previous_assignee"], self.rep.pk)
        with self.captureOnCommitCallbacks(execute=True):
            tasks.start(task, by=self.rep)  # open -> in_progress: no signal
            tasks.complete(task, by=self.rep)
        with self.captureOnCommitCallbacks(execute=True):
            tasks.complete(task, by=self.rep)  # no-op
        with self.captureOnCommitCallbacks(execute=True):
            tasks.reopen(task, by=self.rep)
        with self.captureOnCommitCallbacks(execute=True):
            tasks.cancel(task, by=self.rep)
            tasks.reopen(task, by=self.rep, status="waiting")
        self.assertEqual(counter.names(), ["task_assigned", "task_completed", "task_reopened",
                                           "task_reopened"])
        self.assertJsonPayloads(counter)

    def test_update_fields_by_attname_still_sends_assigned(self):
        task = tasks.create(company=self.company, title="Call back")
        counter = Counter(self, "task_assigned")
        with self.captureOnCommitCallbacks(execute=True):
            task.assignee_id = self.rep.pk
            task.save(update_fields=["assignee_id", "updated_at"])
        self.assertIsNone(counter.only("task_assigned")["previous_assignee"])

    def test_a_save_writing_other_columns_sends_no_transition(self):
        task = tasks.create(company=self.company, title="Call back")
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True):
            task.title = "Call back today"
            task.save(update_fields=["title", "updated_at"])
        self.assertEqual(counter.calls, [])

    def test_a_rolled_back_completion_sends_nothing(self):
        task = tasks.create(company=self.company, title="Call back")
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    tasks.complete(task, by=self.rep)
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass
        self.assertEqual(counter.calls, [])


class CommunicationSignalTests(_Base):
    def test_logged_fires_once_and_a_reimport_does_not_repeat_it(self):
        counter = Counter(self)
        envelope = {"schema_version": 1, "provider": "dialer", "payload": {"duration": 30}}
        kwargs = dict(direction="outbound", occurred_at=timezone.now(), provider="dialer",
                      external_id="call-1", envelope=envelope, known_max_version=1,
                      party=self.ada)
        with self.captureOnCommitCallbacks(execute=True):
            first = ingest.record(self.company, "call", **kwargs)
        with self.captureOnCommitCallbacks(execute=True):
            ingest.record(self.company, "call", **{**kwargs, "envelope": {
                **envelope, "payload": {"duration": 45}}})
        self.assertEqual(counter.names(), ["communication_logged"])
        self.assertEqual(counter.only("communication_logged")["communication"].pk,
                         first.communication.pk)
        payload = webhook_payload("communication_logged", counter.calls[0][1])
        self.assertEqual(set(payload) & {"subject", "body_text", "summary"}, set())
        json.dumps(payload)


class SuppressedTests(TestCase):
    def test_emit_inside_suppressed_sends_nothing_and_the_block_ends_cleanly(self):
        from core.models import Company

        company = Company.objects.create(name="X")
        counter = Counter(self, "engagement_created")
        with self.captureOnCommitCallbacks(execute=True):
            with suppressed():
                emit(signals.engagement_created, Engagement, company, engagement=company)
            emit(signals.engagement_created, Engagement, company, engagement=company)
        self.assertEqual(len(counter.calls), 1)


class BackfillsSendNothingTests(_Base):
    """Section 7, "signal noise at cutover": every data step is silent."""

    def _customer(self):
        return Customer.objects.create(first_name="Cus", last_name="Tomer", email="cus@example.com",
                                       company=self.company)

    def test_step5_quote_backfill_sends_nothing(self):
        customer = self._customer()
        quote = Quote(company=self.company, customer=customer, quote_number="QT-L00001",
                      status="sent", sales_rep=self.rep, legacy_follow_up_date=datetime.date(2026, 11, 2))
        Quote.objects.bulk_create([quote])
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True):
            backfill_quote_engagements()
        self.assertIsNotNone(Quote.objects.get(pk=quote.pk).engagement_id)
        self.assertEqual(counter.calls, [])

    def test_step6_backfills_and_the_command_send_nothing(self):
        lead = Lead(company=self.company, lead_number="LD-L00001", first_name="Ada",
                    last_name="Lovelace", email="ada.l@example.com", status="contacted")
        Lead.objects.bulk_create([lead])
        ActivityLog.objects.bulk_create([ActivityLog(
            company=self.company, activity_type="call", subject="Rang", activity_date=timezone.now())])
        ActivityLog.objects.bulk_create([ActivityLog(
            company=self.company, activity_type="task", subject="Sent pack", activity_date=timezone.now())])
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True):
            backfill_lead_engagements()
            backfill_activity_communications()
        self.assertIsNotNone(Lead.objects.get(pk=lead.pk).engagement_id)
        self.assertEqual(Communication.objects.filter(metadata__from_activitylog=True).count(), 1)
        self.assertEqual(counter.calls, [])

    def test_email_backfill_through_live_save_sends_nothing(self):
        from communications.backfill import backfill_email_communications

        customer = self._customer()
        quote = Quote.objects.create(customer=customer, company=self.company)
        log = EmailLog.objects.create(
            document_type="quote", document_id=quote.pk, document_number=quote.quote_number,
            recipient_email="cus@example.com", subject="Your quote", template_name="quote",
            status="sent", triggered_by="manual",
        )
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True):
            backfill_email_communications()
        self.assertTrue(Communication.objects.filter(email_log=log).exists())
        self.assertEqual(counter.calls, [])

    def test_reconcile_fix_sends_nothing(self):
        customer = self._customer()
        quote = Quote(company=self.company, customer=customer, quote_number="QT-L00002")
        Quote.objects.bulk_create([quote])  # an untouched draft: --fix ensures it lazily
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True):
            call_command("reconcile_engagements", "--fix", stdout=io.StringIO())
        self.assertIsNotNone(Quote.objects.get(pk=quote.pk).engagement_id)
        self.assertEqual(counter.calls, [])

    def test_a_legacy_quote_threaded_on_its_next_save_sends_nothing(self):
        customer = self._customer()
        legacy = Quote(company=self.company, customer=customer, quote_number="QT-L00009",
                       status="accepted")
        Quote.objects.bulk_create([legacy])
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True):
            legacy = Quote.objects.get(pk=legacy.pk)
            legacy.notes = "touched"
            legacy.save()
        legacy.refresh_from_db()
        self.assertEqual(legacy.engagement.status, "won")
        self.assertEqual(counter.calls, [])
        with self.captureOnCommitCallbacks(execute=True):
            Quote.objects.create(company=self.company, customer=customer)  # a new deal
        self.assertEqual(counter.names(), ["engagement_created"])

    def test_backfill_crm_command_sends_nothing(self):
        lead = Lead(company=self.company, lead_number="LD-L00003", first_name="Bo",
                    last_name="Peep", email="bo@example.com", status="new")
        Lead.objects.bulk_create([lead])
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True):
            call_command("backfill_crm", "--step", "6", stdout=io.StringIO())
        self.assertIsNotNone(Lead.objects.get(pk=lead.pk).engagement_id)
        self.assertEqual(counter.calls, [])

    def test_counter_does_see_live_intake(self):
        # Guards the tests above against a counter that never fires.
        counter = Counter(self)
        with self.captureOnCommitCallbacks(execute=True):
            Lead.objects.create(company=self.company, first_name="Live", last_name="Lead",
                                email="live@example.com")
        self.assertEqual(sorted(set(counter.names())),
                         ["communication_logged", "engagement_created", "task_created"])


class TaskDueTests(_Base):
    def setUp(self):
        self.now = timezone.now()

    def due_task(self, minutes_ago=5, **kwargs):
        due_at = self.now - datetime.timedelta(minutes=minutes_ago)
        return tasks.create(company=self.company, title="Call", due=due_at, assignee=self.rep, **kwargs)

    def run_due(self, now=None):
        with self.captureOnCommitCallbacks(execute=True):
            return due.emit_due(now or self.now)

    def test_fires_once_per_task_crossing_its_due_time(self):
        crossed = [self.due_task(), self.due_task(minutes_ago=600)]
        tasks.create(company=self.company, title="Later", due=self.now + datetime.timedelta(hours=1))
        tasks.create(company=self.company, title="Undated")
        done = self.due_task()
        tasks.complete(done)
        counter = Counter(self, "task_due")
        first = self.run_due()
        second = self.run_due()
        self.assertEqual((first.sent, second.sent), (2, 0))
        self.assertEqual(sorted(kw["task"].pk for _n, kw in counter.calls),
                         sorted(t.pk for t in crossed))
        for task in crossed:
            task.refresh_from_db()
            self.assertEqual(task.due_notified_at, self.now)
        self.assertJsonPayloads(counter)

    def test_two_concurrent_runs_send_once(self):
        """Both runs select the task before either claims it; one UPDATE wins."""
        task = self.due_task()
        counter = Counter(self, "task_due")
        stale = list(due.due_candidates(self.now).values_list("pk", flat=True))
        self.assertEqual(self.run_due().sent, 1)
        with mock.patch.object(due, "due_candidates") as candidates:
            candidates.return_value.values_list.return_value = stale
            late = self.run_due()
        self.assertEqual((late.candidates, late.sent, late.skipped), (1, 0, 1))
        self.assertEqual([kw["task"].pk for _n, kw in counter.calls], [task.pk])

    def test_rescheduling_clears_the_marker_and_it_fires_again_at_the_new_time(self):
        task = self.due_task()
        counter = Counter(self, "task_due")
        self.run_due()
        later = self.now + datetime.timedelta(days=1)
        tasks.reschedule(task, later)
        task.refresh_from_db()
        self.assertIsNone(task.due_notified_at)
        self.assertEqual(self.run_due().sent, 0)  # not due yet
        self.assertEqual(self.run_due(later + datetime.timedelta(minutes=1)).sent, 1)
        self.assertEqual(len(counter.calls), 2)

    def test_a_missed_run_sends_late_never_twice(self):
        self.due_task(minutes_ago=60 * 24 * 3)
        counter = Counter(self, "task_due")
        self.run_due()
        self.run_due(self.now + datetime.timedelta(hours=1))
        self.assertEqual(len(counter.calls), 1)

    def test_a_task_rescheduled_between_select_and_claim_is_not_claimed(self):
        task = self.due_task()
        stale = list(due.due_candidates(self.now).values_list("pk", flat=True))
        tasks.reschedule(task, self.now + datetime.timedelta(days=1))
        counter = Counter(self, "task_due")
        with mock.patch.object(due, "due_candidates") as candidates:
            candidates.return_value.values_list.return_value = stale
            self.assertEqual(self.run_due().sent, 0)
        task.refresh_from_db()
        self.assertIsNone(task.due_notified_at)  # still fires at the new time
        self.assertEqual(self.run_due(self.now + datetime.timedelta(days=1, minutes=1)).sent, 1)
        self.assertEqual(len(counter.calls), 1)

    def test_a_completed_task_is_never_claimed(self):
        task = self.due_task()
        tasks.complete(task)
        self.assertFalse(due.claim(task.pk, self.now))

    def test_the_command(self):
        self.due_task()
        out = io.StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("crm_emit_due", stdout=out)
        self.assertIn("task_due sent: 1", out.getvalue())
        self.assertEqual(Task.objects.filter(due_notified_at__isnull=False).count(), 1)


class TwoTenantTests(_Base):
    def test_signals_carry_their_own_tenant(self):
        other_rep = profile(self.other_company, "rival")
        counter = Counter(self, "engagement_created")
        with self.captureOnCommitCallbacks(execute=True):
            mine = self.engagement()
            theirs = engagements.create(company=self.other_company, party=person(self.other_company),
                                        owner=other_rep)
        payloads = [webhook_payload("engagement_created", kw) for _n, kw in counter.calls]
        self.assertEqual({p["company_id"] for p in payloads},
                         {str(mine.company_id), str(theirs.company_id)})
