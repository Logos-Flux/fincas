"""CRM Phase 6 step 2: ``task_updated`` and ``at=`` on the task services
(docs/crm/phase6-adapters.md 3.1 and section 8, step 2).

``task_updated`` is sent once per save of an existing task with the columns
the save wrote (the ``party_updated`` pattern), so a reschedule, a cancel and
a title edit, which have no signal of their own, are heard. ``at=`` lets an
inbound sync date the change with the provider's timestamp; a completion
dated in the past never moves the engagement's activity clock backwards.
"""

from __future__ import annotations

import datetime

from django.core.exceptions import ValidationError
from django.utils import timezone

from core.plugins import signals
from crm import tasks
from crm.models import Engagement, Task, TaskEvent

from .helpers import CrmTestCase

DAY = datetime.timedelta(days=1)


class Updates:
    def __init__(self, test):
        self.calls: list[tuple] = []
        uid = f"task-updated-test.{id(self)}"
        signals.task_updated.connect(self._receive, weak=False, dispatch_uid=uid)
        test.addCleanup(signals.task_updated.disconnect, dispatch_uid=uid)

    def _receive(self, sender, signal=None, **kwargs):
        self.calls.append((kwargs["task"].pk, kwargs["update_fields"]))


class TaskUpdatedSignalTests(CrmTestCase):
    def setUp(self):
        self.task = tasks.create(company=self.company, title="Call back", due=datetime.date(2026, 3, 5))
        self.updates = Updates(self)

    def run_once(self, fn):
        with self.captureOnCommitCallbacks(execute=True):
            fn()
        return self.updates.calls

    def test_create_sends_no_task_updated(self):
        calls = self.run_once(lambda: tasks.create(company=self.company, title="New"))
        self.assertEqual(calls, [])

    def test_reschedule_sends_one_with_the_written_columns(self):
        calls = self.run_once(lambda: tasks.reschedule(self.task, datetime.date(2026, 3, 9)))
        self.assertEqual(calls, [(self.task.pk, ("all_day", "due_at", "due_notified_at", "due_tz", "updated_at"))])

    def test_cancel_sends_one(self):
        calls = self.run_once(lambda: tasks.cancel(self.task))
        self.assertEqual(calls, [(self.task.pk, ("completed_at", "completed_by", "status", "updated_at"))])

    def test_title_edit_sends_one(self):
        calls = self.run_once(lambda: tasks.edit(self.task, title="Call back today"))
        self.assertEqual(calls, [(self.task.pk, ("title", "updated_at"))])

    def test_an_edit_that_changes_nothing_saves_nothing(self):
        calls = self.run_once(lambda: tasks.edit(self.task, title="Call back"))
        self.assertEqual(calls, [])

    def test_assign_and_complete_also_send_it(self):
        calls = self.run_once(lambda: (tasks.assign(self.task, self.rep), tasks.complete(self.task)))
        self.assertEqual([c[1] for c in calls], [
            ("assignee", "updated_at"), ("completed_at", "completed_by", "status", "updated_at")])

    def test_a_full_save_names_every_column(self):
        def save():
            self.task.description = "x"
            self.task.save()

        (pk, columns), = self.run_once(save)
        self.assertIn("description", columns)
        self.assertIn("assignee", columns)  # field names, not attnames
        self.assertNotIn("id", columns)
        self.assertEqual(list(columns), sorted(columns))

    def test_changed_fields_drops_untouched_snapshotted_columns(self):
        seen = []
        uid = f"task-updated-changed.{id(self)}"
        signals.task_updated.connect(lambda sender, **kw: seen.append(kw["changed_fields"]), weak=False,
                                     dispatch_uid=uid)
        self.addCleanup(signals.task_updated.disconnect, dispatch_uid=uid)
        task = Task.objects.get(pk=self.task.pk)
        with self.captureOnCommitCallbacks(execute=True):
            task.save()
        self.assertNotIn("title", seen[-1])
        self.assertNotIn("due_at", seen[-1])
        self.assertIn("updated_at", seen[-1])  # not snapshotted: always kept
        with self.captureOnCommitCallbacks(execute=True):
            tasks.edit(task, title="Changed")
        self.assertEqual(seen[-1], ("title", "updated_at"))

    def test_a_rollback_sends_nothing(self):
        from django.db import transaction

        class Boom(Exception):
            pass

        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    tasks.edit(self.task, title="Lost")
                    raise Boom
            except Boom:
                pass
        self.assertEqual(self.updates.calls, [])

    def test_edit_refuses_columns_it_does_not_own(self):
        with self.assertRaises(ValueError):
            tasks.edit(self.task, status="done")


class AtTests(CrmTestCase):
    def setUp(self):
        self.engagement = self.engagement()
        self.task = tasks.create(engagement=self.engagement, title="Measure")

    def test_complete_at_a_past_time_dates_the_completion_and_the_event(self):
        past = timezone.now() - 3 * DAY
        tasks.complete(self.task, at=past)
        self.task.refresh_from_db()
        self.assertEqual(self.task.completed_at, past)
        event = TaskEvent.objects.get(task=self.task, event=TaskEvent.Event.COMPLETED)
        self.assertEqual(event.at, past)

    def test_complete_at_a_past_time_never_moves_last_activity_backwards(self):
        recent = timezone.now() - DAY
        Engagement._base_manager.filter(pk=self.engagement.pk).update(last_activity_at=recent)
        tasks.complete(self.task, at=timezone.now() - 3 * DAY)
        self.engagement.refresh_from_db()
        self.assertEqual(self.engagement.last_activity_at, recent)

    def test_complete_at_moves_last_activity_forward(self):
        old = timezone.now() - 10 * DAY
        Engagement._base_manager.filter(pk=self.engagement.pk).update(last_activity_at=old)
        when = timezone.now() - 2 * DAY
        tasks.complete(self.task, at=when)
        self.engagement.refresh_from_db()
        self.assertEqual(self.engagement.last_activity_at, when)

    def test_a_future_at_is_clamped_to_now(self):
        before = timezone.now()
        tasks.complete(self.task, at=before + 30 * DAY)
        self.assertLessEqual(self.task.completed_at, timezone.now())
        self.assertGreaterEqual(self.task.completed_at, before)

    def test_every_service_takes_at(self):
        when = timezone.now() - 5 * DAY
        task = tasks.create(company=self.company, title="Dated", at=when)
        tasks.assign(task, self.rep, at=when)
        tasks.reschedule(task, datetime.date(2026, 4, 1), at=when)
        tasks.start(task, at=when)
        tasks.cancel(task, at=when)
        tasks.reopen(task, at=when)
        self.assertEqual(set(TaskEvent.objects.filter(task=task).values_list("at", flat=True)), {when})

    def test_set_status_routes_through_the_owning_service(self):
        task = tasks.create(company=self.company, title="Route")
        tasks.set_status(task, "waiting")
        self.assertEqual(task.status, "waiting")
        tasks.set_status(task, "done")
        self.assertIsNotNone(task.completed_at)
        with self.assertRaises(ValidationError):
            tasks.set_status(task, "cancelled")  # a completion is history
        tasks.set_status(task, "in_progress")  # a reopen
        self.assertEqual(Task.objects.get(pk=task.pk).status, "in_progress")
        self.assertEqual(
            list(TaskEvent.objects.filter(task=task).order_by("at", "created_at").values_list("event", flat=True)),
            ["created", "started", "completed", "reopened"])
