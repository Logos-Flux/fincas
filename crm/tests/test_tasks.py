"""``crm.tasks``: lifecycle, events, due times and the next action (spec 1.5, 7)."""

from __future__ import annotations

import datetime
from io import StringIO
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from crm import engagements, tasks
from crm.models import Engagement, Task, TaskEvent

from .helpers import CrmTestCase, profile

UTC = datetime.UTC
BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")


def _next(eng):
    eng.refresh_from_db()
    return eng.next_task_id, eng.next_action_due_at


class NextActionTests(CrmTestCase):
    def setUp(self):
        self.eng = self.engagement()

    def test_create_sets_next_action(self):
        self.assertEqual(_next(self.eng), (None, None))
        task = tasks.create(title="Call", engagement=self.eng, due=datetime.date(2030, 1, 10))
        self.assertEqual(_next(self.eng), (task.pk, task.due_at))

    def test_earliest_due_wins_nulls_last_then_oldest(self):
        undated = tasks.create(title="Someday", engagement=self.eng)
        self.assertEqual(_next(self.eng), (undated.pk, None))
        late = tasks.create(title="Late", engagement=self.eng, due=datetime.date(2030, 3, 1))
        self.assertEqual(_next(self.eng)[0], late.pk)
        early = tasks.create(title="Early", engagement=self.eng, due=datetime.date(2030, 2, 1))
        self.assertEqual(_next(self.eng)[0], early.pk)
        twin = tasks.create(title="Same day, newer", engagement=self.eng, due=datetime.date(2030, 2, 1))
        self.assertEqual(_next(self.eng)[0], early.pk)  # same due_at: oldest first
        tasks.complete(early)
        self.assertEqual(_next(self.eng)[0], twin.pk)

    def test_complete_cancel_and_reopen_recompute(self):
        a = tasks.create(title="A", engagement=self.eng, due=datetime.date(2030, 1, 1))
        b = tasks.create(title="B", engagement=self.eng, due=datetime.date(2030, 1, 2))
        tasks.complete(a)
        self.assertEqual(_next(self.eng)[0], b.pk)
        tasks.cancel(b)
        self.assertEqual(_next(self.eng), (None, None))
        tasks.reopen(a)
        self.assertEqual(_next(self.eng)[0], a.pk)

    def test_in_progress_and_waiting_are_open(self):
        a = tasks.create(title="A", engagement=self.eng, due=datetime.date(2030, 1, 1))
        tasks.start(a)
        self.assertEqual(_next(self.eng)[0], a.pk)
        Task.objects.filter(pk=a.pk).update(status="waiting")
        a.refresh_from_db()
        a.save()
        self.assertEqual(_next(self.eng)[0], a.pk)

    def test_reschedule_recomputes(self):
        a = tasks.create(title="A", engagement=self.eng, due=datetime.date(2030, 1, 1))
        b = tasks.create(title="B", engagement=self.eng, due=datetime.date(2030, 1, 5))
        tasks.reschedule(a, datetime.date(2030, 1, 9))
        self.assertEqual(_next(self.eng), (b.pk, b.due_at))

    def test_delete_recomputes_instance_and_queryset(self):
        a = tasks.create(title="A", engagement=self.eng, due=datetime.date(2030, 1, 1))
        b = tasks.create(title="B", engagement=self.eng, due=datetime.date(2030, 1, 5))
        a.delete()
        self.assertEqual(_next(self.eng)[0], b.pk)
        Task.objects.filter(pk=b.pk).delete()
        self.assertEqual(_next(self.eng), (None, None))

    def test_moving_a_task_between_engagements_recomputes_both(self):
        other = self.engagement()
        a = tasks.create(title="A", engagement=self.eng, due=datetime.date(2030, 1, 1))
        a.engagement = other
        a.save()
        self.assertEqual(_next(self.eng), (None, None))
        self.assertEqual(_next(other)[0], a.pk)

    def test_closing_the_engagement_keeps_its_next_action(self):
        a = tasks.create(title="Welcome pack", engagement=self.eng, due=datetime.date(2030, 1, 1))
        engagements.win(self.eng)
        self.assertEqual(_next(self.eng)[0], a.pk)

    def test_complete_bumps_last_activity(self):
        a = tasks.create(title="A", engagement=self.eng)
        Engagement.objects.filter(pk=self.eng.pk).update(last_activity_at=None)
        tasks.complete(a)
        self.eng.refresh_from_db()
        self.assertEqual(self.eng.last_activity_at, a.completed_at)


class EventTests(CrmTestCase):
    def test_lifecycle_writes_events(self):
        other = profile(self.company, "other")
        task = tasks.create(company=self.company, title="Call", by=self.rep,
                            due=datetime.date(2030, 1, 1))
        tasks.assign(task, other, by=self.rep)
        tasks.reschedule(task, datetime.date(2030, 1, 3), by=self.rep)
        tasks.start(task, by=other)
        tasks.complete(task, by=other)
        tasks.reopen(task, by=self.rep)
        tasks.cancel(task, by=self.rep)
        events = list(task.events.order_by("at", "created_at").values_list("event", "by"))
        self.assertEqual([e for e, _ in events], [
            "created", "assigned", "rescheduled", "started", "completed", "reopened", "cancelled",
        ])
        self.assertEqual(task.events.get(event="assigned").to_value, str(other.pk))
        self.assertEqual(task.events.get(event="completed").from_value, "in_progress")
        self.assertEqual(task.events.get(event="reopened").to_value, "open")

    def test_completion_sets_and_reopen_clears_completed(self):
        task = tasks.create(company=self.company, title="Call")
        tasks.complete(task, by=self.rep)
        self.assertEqual((task.status, task.completed_by), ("done", self.rep))
        self.assertIsNotNone(task.completed_at)
        tasks.reopen(task)
        task.refresh_from_db()
        self.assertEqual((task.status, task.completed_at, task.completed_by), ("open", None, None))
        self.assertEqual(TaskEvent.objects.filter(task=task, event="reopened").count(), 1)

    def test_noops_write_no_events(self):
        task = tasks.create(company=self.company, title="Call", due=datetime.date(2030, 1, 1))
        tasks.reopen(task)  # already open
        tasks.reschedule(task, datetime.date(2030, 1, 1))
        tasks.assign(task, None)
        self.assertEqual(task.events.count(), 1)

    def test_reopen_needs_an_open_status_and_unknown_kind_refused(self):
        task = tasks.create(company=self.company, title="Call")
        tasks.complete(task)
        with self.assertRaises(ValidationError):
            tasks.reopen(task, status="done")
        with self.assertRaises(ValidationError):
            tasks.create(company=self.company, title="x", kind="telepathy")

    def test_leaving_done_or_cancelled_needs_reopen(self):
        task = tasks.create(company=self.company, title="Call")
        tasks.complete(task)
        for change in (tasks.start, tasks.cancel):
            with self.subTest(change=change.__name__), self.assertRaises(ValidationError):
                change(task)
        task.refresh_from_db()
        self.assertEqual(task.status, "done")
        self.assertIsNotNone(task.completed_at)
        tasks.reopen(task)
        tasks.cancel(task)
        with self.assertRaises(ValidationError):
            tasks.complete(task)

    def test_reschedule_clears_due_notified_at(self):
        task = tasks.create(company=self.company, title="Call", due=datetime.date(2030, 1, 1))
        Task.objects.filter(pk=task.pk).update(due_notified_at=timezone.now())
        task.refresh_from_db()
        tasks.reschedule(task, datetime.date(2030, 1, 2))
        task.refresh_from_db()
        self.assertIsNone(task.due_notified_at)


@override_settings(TIME_ZONE="America/New_York")
class DueTimeTests(CrmTestCase):
    def test_all_day_is_end_of_local_day_in_assignees_zone(self):
        berliner = profile(self.company, "berliner", timezone="Europe/Berlin")
        task = tasks.create(company=self.company, title="t", assignee=berliner,
                            due=datetime.date(2026, 6, 15))
        self.assertEqual((task.due_tz, task.all_day), ("Europe/Berlin", True))
        # 23:59:59.999999 CEST (UTC+2) is 21:59:59.999999 UTC.
        self.assertEqual(task.due_at, datetime.datetime(2026, 6, 15, 21, 59, 59, 999999, tzinfo=UTC))
        self.assertEqual(tasks.local_due_date(task), datetime.date(2026, 6, 15))

    def test_resolution_falls_back_to_company_then_settings(self):
        self.company.timezone = "Asia/Tokyo"
        self.company.save()
        task = tasks.create(company=self.company, title="t", assignee=self.rep,
                            due=datetime.date(2026, 6, 15))
        self.assertEqual(task.due_tz, "Asia/Tokyo")
        unzoned = tasks.create(company=self.other_company, title="t", due=datetime.date(2026, 6, 15))
        self.assertEqual(unzoned.due_tz, "America/New_York")

    def test_explicit_zone_and_naive_datetime(self):
        task = tasks.create(company=self.company, title="t", due_tz="Europe/Berlin",
                            due=datetime.datetime(2026, 1, 20, 9, 30))
        self.assertFalse(task.all_day)
        self.assertEqual(task.due_at, datetime.datetime(2026, 1, 20, 8, 30, tzinfo=UTC))
        with self.assertRaises(ValidationError):
            tasks.create(company=self.company, title="t", due_tz="Mars/Olympus",
                         due=datetime.date(2026, 1, 1))

    def test_aware_datetime_kept_as_instant(self):
        instant = datetime.datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
        task = tasks.create(company=self.company, title="t", due=instant)
        self.assertEqual(task.due_at, instant)
        self.assertEqual(task.due_tz, "America/New_York")

    def test_dst_change_days_end_at_local_midnight(self):
        # Spring forward (23h day) and fall back (25h day) in New York and Berlin.
        cases = [
            (NEW_YORK, datetime.date(2026, 3, 8), datetime.datetime(2026, 3, 9, 3, 59, 59, 999999, tzinfo=UTC)),
            (NEW_YORK, datetime.date(2026, 11, 1), datetime.datetime(2026, 11, 2, 4, 59, 59, 999999, tzinfo=UTC)),
            (BERLIN, datetime.date(2026, 3, 29), datetime.datetime(2026, 3, 29, 21, 59, 59, 999999, tzinfo=UTC)),
            (BERLIN, datetime.date(2026, 10, 25), datetime.datetime(2026, 10, 25, 22, 59, 59, 999999, tzinfo=UTC)),
        ]
        for zone, day, expected in cases:
            with self.subTest(zone=zone.key, day=day):
                task = tasks.create(company=self.company, title="t", due_tz=zone.key, due=day)
                self.assertEqual(task.due_at, expected)
                self.assertEqual(tasks.local_due_date(task), day)
                # One microsecond later is the next local day.
                after = (task.due_at + datetime.timedelta(microseconds=1)).astimezone(zone)
                self.assertEqual((after.date(), after.time()), (day + datetime.timedelta(days=1),
                                                                datetime.time.min))

    def test_reassigning_does_not_move_the_deadline(self):
        berliner = profile(self.company, "berliner", timezone="Europe/Berlin")
        task = tasks.create(company=self.company, title="t", assignee=self.rep,
                            due=datetime.date(2026, 6, 15))
        due_at = task.due_at
        tasks.assign(task, berliner)
        task.refresh_from_db()
        self.assertEqual((task.due_at, task.due_tz), (due_at, "America/New_York"))

    def test_reschedule_keeps_the_set_in_zone(self):
        task = tasks.create(company=self.company, title="t", due_tz="Europe/Berlin",
                            due=datetime.date(2026, 6, 15))
        tasks.reschedule(task, datetime.date(2026, 6, 16))
        self.assertEqual(task.due_tz, "Europe/Berlin")
        self.assertEqual(tasks.local_due_date(task), datetime.date(2026, 6, 16))
        tasks.reschedule(task, None)
        task.refresh_from_db()
        self.assertEqual((task.due_at, task.due_tz), (None, ""))


class QueueTests(CrmTestCase):
    def test_due_queue_filters_and_orders(self):
        now = timezone.now()
        other = profile(self.company, "other")
        overdue = tasks.create(company=self.company, title="overdue", assignee=self.rep,
                               due=now - datetime.timedelta(hours=2))
        older = tasks.create(company=self.company, title="older", assignee=self.rep,
                             due=now - datetime.timedelta(days=2))
        tasks.create(company=self.company, title="future", assignee=self.rep,
                     due=now + datetime.timedelta(days=2))
        tasks.create(company=self.company, title="undated", assignee=self.rep)
        unassigned = tasks.create(company=self.company, title="nobody's",
                                  due=now - datetime.timedelta(hours=1))
        tasks.create(company=self.company, title="theirs", assignee=other,
                     due=now - datetime.timedelta(hours=1))
        done = tasks.create(company=self.company, title="done", assignee=self.rep,
                            due=now - datetime.timedelta(hours=3))
        tasks.complete(done)
        tasks.create(company=self.other_company, title="elsewhere",
                     due=now - datetime.timedelta(hours=1))
        self.assertEqual(list(tasks.due_queue(self.company, assignee=self.rep)), [older, overdue])
        self.assertEqual(list(tasks.due_queue(self.company, unassigned=True)), [unassigned])
        self.assertEqual(tasks.due_queue(self.company).count(), 4)

    def test_due_on_local_day_uses_viewers_zone(self):
        # Due 23:30 New York on the 15th = 04:30 UTC on the 16th = 06:30 Berlin on the 16th.
        task = tasks.create(company=self.company, title="t", due_tz="America/New_York",
                            due=datetime.datetime(2026, 6, 15, 23, 30))
        self.assertEqual(list(tasks.due_on_local_day(self.company, datetime.date(2026, 6, 15),
                                                     NEW_YORK)), [task])
        self.assertEqual(list(tasks.due_on_local_day(self.company, datetime.date(2026, 6, 16),
                                                     BERLIN)), [task])
        self.assertFalse(tasks.due_on_local_day(self.company, datetime.date(2026, 6, 15), BERLIN))


class ReconcileEngagementsTests(CrmTestCase):
    def test_reports_and_fixes_drift_from_update(self):
        eng = self.engagement()
        task = tasks.create(title="A", engagement=eng, due=datetime.date(2030, 1, 1))
        out = StringIO()
        call_command("reconcile_engagements", "--check", stdout=out)
        self.assertIn("drift: 0", out.getvalue())
        Task.objects.filter(pk=task.pk).update(status="done", completed_at=timezone.now())
        with self.assertRaises(CommandError):
            call_command("reconcile_engagements", stdout=StringIO())
        out = StringIO()
        call_command("reconcile_engagements", "--fix", stdout=out)
        self.assertIn("Fixed: 1", out.getvalue())
        self.assertEqual(_next(eng), (None, None))
