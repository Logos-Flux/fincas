"""``crm.engagements``: the one writer of stage and status (spec 1.4, E3, E12)."""

from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError

from crm import engagements, tasks
from crm.models import Engagement, EngagementStageChange, Pipeline, Stage

from .helpers import CrmTestCase, profile


class CreateTests(CrmTestCase):
    def test_lands_in_default_pipeline_first_stage_with_history(self):
        eng = self.engagement(owner=self.rep, by=self.rep, engagement_source="phone")
        self.assertEqual((eng.pipeline, eng.stage.key, eng.status), (self.pipeline, "new", "open"))
        self.assertIsNotNone(eng.stage_entered_at)
        self.assertIsNotNone(eng.last_activity_at)
        change = eng.stage_changes.get()
        self.assertEqual((change.from_status, change.to_status), ("", "open"))
        self.assertEqual((change.from_stage, change.to_stage), (None, eng.stage))
        self.assertEqual((change.to_pipeline, change.changed_by, change.source),
                         (self.pipeline, self.rep, "erp"))

    def test_refuses_inactive_pipeline_and_unknown_source(self):
        Pipeline.objects.filter(pk=self.pipeline.pk).update(is_active=False)
        self.pipeline.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.engagement(pipeline=self.pipeline)
        Pipeline.objects.filter(pk=self.pipeline.pk).update(is_active=True)
        self.pipeline.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.engagement(engagement_source="carrier_pigeon")

    def test_refuses_outcome_fields(self):
        from django.utils import timezone

        with self.assertRaises(ValidationError) as ctx:
            self.engagement(status="lost", closed_at=timezone.now(), loss_reason="unknown")
        self.assertEqual(set(ctx.exception.message_dict), {"status", "closed_at", "loss_reason"})

    def test_refuses_party_of_another_company(self):
        from .helpers import organisation

        with self.assertRaises(ValidationError):
            self.engagement(party=organisation(self.other_company, "Elsewhere"))


class MoveTests(CrmTestCase):
    def test_move_writes_history_and_resets_time_in_stage(self):
        eng = self.engagement()
        entered = eng.stage_entered_at
        result = engagements.move(eng, self.stage("qualification"), by=self.rep, note="call went well")
        self.assertEqual(result.needs_close, "")
        eng.refresh_from_db()
        self.assertEqual(eng.stage.key, "qualification")
        self.assertGreaterEqual(eng.stage_entered_at, entered)
        change = result.change
        self.assertEqual((change.from_stage.key, change.to_stage.key), ("new", "qualification"))
        self.assertEqual((change.from_status, change.to_status), ("open", "open"))
        self.assertEqual(change.note, "call went well")
        self.assertEqual(eng.stage_changes.count(), 2)

    def test_noop_move_writes_nothing(self):
        eng = self.engagement()
        result = engagements.move(eng, eng.stage)
        self.assertIsNone(result.change)
        self.assertEqual(eng.stage_changes.count(), 1)

    def test_refuses_stage_of_another_pipeline(self):
        eng = self.engagement()
        with self.assertRaises(ValidationError):
            engagements.move(eng, self.stage("negotiation", self.other_pipeline))

    def test_refuses_on_closed_engagement(self):
        eng = self.engagement()
        engagements.win(eng)
        with self.assertRaises(ValidationError):
            engagements.move(eng, self.stage("negotiation"))

    def test_outcome_hint_proposes_but_never_closes(self):
        closed = Stage.objects.create(pipeline=self.pipeline, key="closed_won", name="Closed won",
                                      sort_order=200, outcome_hint="won")
        eng = self.engagement()
        result = engagements.move(eng, closed)
        self.assertEqual(result.needs_close, "won")
        eng.refresh_from_db()
        self.assertEqual(eng.status, "open")

    def test_refuses_archived_stage(self):
        eng = self.engagement()
        Stage.objects.filter(pipeline=self.pipeline, key="negotiation").update(is_active=False)
        with self.assertRaises(ValidationError):
            engagements.move(eng, self.stage("negotiation"))

    def test_unknown_history_source_refused(self):
        eng = self.engagement()
        with self.assertRaises(ValidationError):
            engagements.move(eng, self.stage("negotiation"), source="carrier_pigeon")

    def test_stale_instance_is_refreshed_under_lock(self):
        eng = self.engagement()
        stale = Engagement.objects.get(pk=eng.pk)
        engagements.move(eng, self.stage("qualification"))
        engagements.move(stale, self.stage("negotiation"))
        last = eng.stage_changes.order_by("-changed_at", "-created_at").first()
        self.assertEqual(last.from_stage.key, "qualification")  # not the stale "new"


class CloseTests(CrmTestCase):
    def test_win_keeps_stage_and_open_tasks(self):
        eng = self.engagement()
        engagements.move(eng, self.stage("negotiation"))
        task = tasks.create(title="Send welcome pack", engagement=eng)
        change = engagements.win(eng, by=self.rep, source="system")
        eng.refresh_from_db()
        self.assertEqual((eng.status, eng.stage.key), ("won", "negotiation"))
        self.assertIsNotNone(eng.closed_at)
        self.assertEqual((change.from_status, change.to_status), ("open", "won"))
        self.assertEqual(change.from_stage, change.to_stage)
        task.refresh_from_db()
        self.assertEqual(task.status, "open")
        self.assertEqual(eng.next_task_id, task.pk)

    def test_win_twice_is_a_noop(self):
        eng = self.engagement()
        engagements.win(eng)
        self.assertIsNone(engagements.win(eng))
        self.assertEqual(eng.stage_changes.count(), 2)

    def test_lose_keeps_stage_and_records_reason(self):
        eng = self.engagement()
        engagements.move(eng, self.stage("negotiation"))
        engagements.lose(eng, "competitor", competitor="Globex", notes="cheaper")
        eng.refresh_from_db()
        self.assertEqual((eng.status, eng.stage.key, eng.loss_reason, eng.loss_competitor),
                         ("lost", "negotiation", "competitor", "Globex"))
        self.assertEqual(eng.stage_changes.last().to_status, "lost")

    def test_lose_refuses_unknown_reasons(self):
        eng = self.engagement()
        with self.assertRaises(ValidationError):
            engagements.lose(eng, "bad_vibes")
        with self.assertRaises(ValidationError):
            engagements.lose(eng, "unknown")
        engagements.lose(eng, "unknown", source="import")
        eng.refresh_from_db()
        self.assertEqual(eng.loss_reason, "unknown")

    def test_cannot_lose_a_won_engagement_without_reopening(self):
        eng = self.engagement()
        engagements.win(eng)
        with self.assertRaises(ValidationError):
            engagements.lose(eng, "price")


class ReopenTests(CrmTestCase):
    def setUp(self):
        self.manager = profile(self.company, "manager", perms=["crm.reopen_engagement"])

    def test_needs_permission(self):
        eng = self.engagement()
        engagements.lose(eng, "price")
        with self.assertRaises(PermissionDenied):
            engagements.reopen(eng, by=self.rep)
        with self.assertRaises(PermissionDenied):
            engagements.reopen(eng)  # no user, not a system source
        engagements.reopen(eng, source="system")
        self.assertEqual(eng.status, "open")

    def test_back_at_kept_stage_and_clears_loss(self):
        eng = self.engagement()
        engagements.move(eng, self.stage("negotiation"))
        engagements.lose(eng, "price", competitor="Globex")
        change = engagements.reopen(eng, by=self.manager)
        eng.refresh_from_db()
        self.assertEqual((eng.status, eng.stage.key, eng.loss_reason, eng.closed_at),
                         ("open", "negotiation", "", None))
        self.assertEqual((change.from_status, change.to_status), ("lost", "open"))

    def test_stageless_close_reopens_at_last_open_stage_from_history(self):
        eng = self.engagement()
        engagements.move(eng, self.stage("proposal_sent"))
        engagements.win(eng)
        # An import closed it with the stage unknown (E3).
        Engagement.objects.filter(pk=eng.pk).update(stage=None)
        eng.refresh_from_db()
        engagements.reopen(eng, by=self.manager)
        self.assertEqual(eng.stage.key, "proposal_sent")

    def test_no_history_falls_back_to_first_stage(self):
        eng = Engagement.objects.create(company=self.company, pipeline=self.pipeline, stage=None,
                                        status="won", closed_at=self.pipeline.created_at)
        engagements.reopen(eng, by=self.manager)
        self.assertEqual(eng.stage.key, "new")

    def test_explicit_stage(self):
        eng = self.engagement()
        engagements.win(eng)
        engagements.reopen(eng, by=self.manager, stage=self.stage("verbal_commit"))
        self.assertEqual(eng.stage.key, "verbal_commit")
        engagements.win(eng)
        with self.assertRaises(ValidationError):
            engagements.reopen(eng, by=self.manager, stage=self.stage("new", self.other_pipeline))


class ChangePipelineTests(CrmTestCase):
    def test_one_history_row_with_both_pipelines(self):
        service = Pipeline.objects.create(company=self.company, key="service", name="Service")
        triage = Stage.objects.create(pipeline=service, key="triage", name="Triage", sort_order=10)
        eng = self.engagement()
        change = engagements.change_pipeline(eng, service, triage, by=self.rep)
        self.assertEqual((change.from_pipeline, change.to_pipeline), (self.pipeline, service))
        self.assertEqual((change.from_stage.key, change.to_stage), ("new", triage))
        eng.refresh_from_db()
        self.assertEqual((eng.pipeline, eng.stage), (service, triage))

    def test_refuses_mismatched_stage_and_foreign_pipeline(self):
        eng = self.engagement()
        with self.assertRaises(ValidationError):
            engagements.change_pipeline(eng, self.pipeline, self.stage("new", self.other_pipeline))
        with self.assertRaises(ValidationError):
            engagements.change_pipeline(eng, self.other_pipeline,
                                        self.stage("new", self.other_pipeline))


class HistoryTests(CrmTestCase):
    def test_full_thread_history_in_order(self):
        manager = profile(self.company, "manager", perms=["crm.reopen_engagement"])
        eng = self.engagement()
        engagements.move(eng, self.stage("qualification"))
        engagements.move(eng, self.stage("negotiation"))
        engagements.lose(eng, "budget")
        engagements.reopen(eng, by=manager)
        engagements.win(eng)
        rows = list(EngagementStageChange.objects.filter(engagement=eng)
                    .order_by("changed_at", "created_at")
                    .values_list("to_stage__key", "to_status"))
        self.assertEqual(rows, [("new", "open"), ("qualification", "open"),
                                ("negotiation", "open"), ("negotiation", "lost"),
                                ("negotiation", "open"), ("negotiation", "won")])
