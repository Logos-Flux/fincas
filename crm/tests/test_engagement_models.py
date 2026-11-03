"""The Phase 3 engagement schema (docs/crm/phase3-engagement.md 1.2-1.5, 1.8).

Every constraint in the ``crm`` app has a ``test_constraint_<name>`` method
that violates it (``test_every_constraint_has_a_violation_test`` enforces
that), plus the save guard, append-only history, immutable keys and the
``company`` copy-on-save.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from crm import engagements, tasks
from crm.models import (
    AppendOnlyError,
    Engagement,
    EngagementContact,
    EngagementStageChange,
    EngagementWriteRefused,
    Pipeline,
    Stage,
    Task,
    TaskEvent,
    Touchpoint,
)

from .helpers import CrmTestCase, person

NOW = timezone.now()


class ConstraintTests(CrmTestCase):
    def test_every_constraint_has_a_violation_test(self):
        names = {
            constraint.name
            for model in apps.get_app_config("crm").get_models()
            for constraint in model._meta.constraints
        }
        self.assertEqual(len(names), 15)
        missing = sorted(n for n in names if not hasattr(self, f"test_constraint_{n}"))
        self.assertEqual(missing, [])

    def _eng(self, **kwargs):
        kwargs.setdefault("company", self.company)
        kwargs.setdefault("pipeline", self.pipeline)
        kwargs.setdefault("stage", self.stage("new"))
        return Engagement.objects.create(**kwargs)

    # --- Pipeline / Stage (1.2) ---

    def test_constraint_uniq_pipeline_co_key(self):
        Pipeline.objects.create(company=self.other_company, key="service", name="Service")
        Pipeline.objects.create(company=self.company, key="service", name="Service")
        self.assertViolates("uniq_pipeline_co_key", lambda: Pipeline.objects.create(
            company=self.company, key="service", name="Service again"))

    def test_constraint_uniq_pipeline_one_default(self):
        Pipeline.objects.create(company=self.company, key="service", name="Service")
        self.assertViolates("uniq_pipeline_one_default", lambda: Pipeline.objects.create(
            company=self.company, key="other", name="Other", is_default=True))

    def test_constraint_uniq_stage_pipeline_key(self):
        Stage.objects.create(pipeline=self.other_pipeline, key="won_ish", name="x", sort_order=99)
        self.assertViolates("uniq_stage_pipeline_key", lambda: Stage.objects.create(
            pipeline=self.pipeline, key="new", name="New again", sort_order=99))

    def test_constraint_stage_probability_range(self):
        Stage.objects.create(pipeline=self.pipeline, key="certain", name="x", sort_order=99,
                             default_probability=100)
        self.assertViolates("stage_probability_range", lambda: Stage.objects.create(
            pipeline=self.pipeline, key="over", name="x", sort_order=99, default_probability=101))

    # --- Engagement (1.3) ---

    def test_constraint_uniq_engagement_number(self):
        self._eng(number="EN-99999")
        self.assertViolates("uniq_engagement_number", lambda: self._eng(number="EN-99999"))

    def test_constraint_engagement_closed_at_iff_closed(self):
        self.assertViolates("engagement_closed_at_iff_closed",
                            lambda: self._eng(status="won"))
        self.assertViolates("engagement_closed_at_iff_closed",
                            lambda: self._eng(closed_at=NOW))

    def test_constraint_engagement_open_has_stage(self):
        # A closed engagement may have no stage (backfill and import, E3).
        self._eng(status="won", closed_at=NOW, stage=None)
        self.assertViolates("engagement_open_has_stage", lambda: self._eng(stage=None))

    def test_constraint_engagement_loss_reason_only_when_lost(self):
        self.assertViolates("engagement_loss_reason_only_when_lost",
                            lambda: self._eng(loss_reason="price"))
        self.assertViolates("engagement_loss_reason_only_when_lost",
                            lambda: self._eng(status="won", closed_at=NOW, loss_reason="price"))

    def test_constraint_engagement_lost_has_reason(self):
        self._eng(status="lost", closed_at=NOW, loss_reason="price")
        self.assertViolates("engagement_lost_has_reason",
                            lambda: self._eng(status="lost", closed_at=NOW))

    def test_constraint_engagement_probability_range(self):
        self._eng(probability=100)
        self.assertViolates("engagement_probability_range", lambda: self._eng(probability=101))

    def test_constraint_engagement_value_non_negative(self):
        self._eng(expected_value=Decimal("0"))
        self.assertViolates("engagement_value_non_negative",
                            lambda: self._eng(expected_value=Decimal("-0.01")))

    # --- EngagementContact (1.3) ---

    def test_constraint_uniq_engcontact_party(self):
        eng = self._eng()
        EngagementContact.objects.create(engagement=eng, party=self.ada)
        self.assertViolates("uniq_engcontact_party", lambda: EngagementContact.objects.create(
            engagement=eng, party=self.ada, role="billing"))

    def test_constraint_uniq_engcontact_one_primary(self):
        eng = self._eng()
        EngagementContact.objects.create(engagement=eng, party=self.ada, is_primary=True)
        grace = person(self.company, "Grace", "Hopper")
        self.assertViolates("uniq_engcontact_one_primary", lambda: EngagementContact.objects.create(
            engagement=eng, party=grace, is_primary=True))

    # --- Task (1.5) ---

    def test_constraint_task_completed_at_iff_done(self):
        self.assertViolates("task_completed_at_iff_done", lambda: Task.objects.create(
            company=self.company, title="t", status="done"))
        self.assertViolates("task_completed_at_iff_done", lambda: Task.objects.create(
            company=self.company, title="t", completed_at=NOW))

    def test_constraint_task_due_has_tz(self):
        Task.objects.create(company=self.company, title="undated")
        self.assertViolates("task_due_has_tz", lambda: Task.objects.create(
            company=self.company, title="t", due_at=NOW))


class EngagementSaveGuardTests(CrmTestCase):
    def test_direct_stage_write_refused(self):
        eng = self.engagement()
        eng.stage = self.stage("negotiation")
        with self.assertRaises(EngagementWriteRefused):
            eng.save()

    def test_direct_status_write_refused(self):
        eng = self.engagement()
        eng.status, eng.closed_at = "won", NOW
        with self.assertRaises(EngagementWriteRefused):
            eng.save()

    def test_direct_pipeline_write_refused(self):
        other = Pipeline.objects.create(company=self.company, key="service", name="Service")
        eng = self.engagement()
        eng.pipeline = other
        with self.assertRaises(EngagementWriteRefused):
            eng.save()

    def test_refused_after_refresh_and_via_fresh_instance(self):
        eng = self.engagement()
        fresh = Engagement.objects.get(pk=eng.pk)
        fresh.status, fresh.closed_at = "won", NOW
        with self.assertRaises(EngagementWriteRefused):
            fresh.save()
        fresh.refresh_from_db()
        fresh.stage = self.stage("negotiation")
        with self.assertRaises(EngagementWriteRefused):
            fresh.save(update_fields=["stage"])

    def test_update_fields_does_not_launder_an_unwritten_change(self):
        eng = self.engagement()
        eng.stage = self.stage("negotiation")
        eng.title = "Renamed"
        eng.save(update_fields=["title"])  # stage not written: allowed
        with self.assertRaises(EngagementWriteRefused):
            eng.save()  # ...but still refused on the next full save
        eng.refresh_from_db()
        self.assertEqual((eng.title, eng.stage.key), ("Renamed", "new"))

    def test_deferred_guarded_field_assignment_is_refused(self):
        eng = self.engagement()
        deferred = Engagement.objects.only("id", "title", "company").get(pk=eng.pk)
        deferred.status, deferred.closed_at = "won", NOW
        with self.assertRaises(EngagementWriteRefused):
            deferred.save()

    def test_other_fields_save_freely(self):
        eng = self.engagement()
        eng.title, eng.probability = "Renamed", 40
        eng.save()
        # update_fields that leave the guarded columns out are fine even when
        # the in-memory stage differs: nothing guarded is written.
        eng.refresh_from_db()
        self.assertEqual(eng.title, "Renamed")

    def test_number_minted_and_title_defaulted(self):
        eng = self.engagement(engagement_source="trade_show")
        self.assertRegex(eng.number, r"^EN-\d{5}$")
        self.assertEqual(eng.title, "Acme Widgets — Trade show")
        self.assertEqual(self.engagement(party=None).title, "No counterparty — New")

    def test_weighted_value_is_computed(self):
        eng = self.engagement(expected_value=Decimal("1000.00"))
        self.assertEqual(eng.weighted_value, Decimal("50.00"))  # stage "new": 5%
        eng.probability = 30
        self.assertEqual(eng.weighted_value, Decimal("300.00"))
        eng.expected_value = None
        self.assertIsNone(eng.weighted_value)

    def test_clean_refuses_stage_from_another_pipeline_and_unknown_keys(self):
        eng = Engagement(company=self.company, pipeline=self.pipeline,
                         stage=self.stage("new", self.other_pipeline), source="carrier_pigeon",
                         loss_reason="bad_vibes")
        with self.assertRaises(ValidationError) as ctx:
            eng.clean()
        self.assertEqual(set(ctx.exception.message_dict), {"stage", "source", "loss_reason"})


class AppendOnlyTests(CrmTestCase):
    def test_stage_change_rows_refuse_update_and_delete(self):
        eng = self.engagement()
        engagements.move(eng, self.stage("qualification"))
        change = eng.stage_changes.last()
        change.note = "rewritten"
        with self.assertRaises(AppendOnlyError):
            change.save()
        with self.assertRaises(AppendOnlyError):
            change.delete()
        self.assertEqual(EngagementStageChange.objects.get(pk=change.pk).note, "")

    def test_task_event_rows_refuse_update_and_delete(self):
        task = tasks.create(company=self.company, title="Call back")
        event = task.events.get()
        event.to_value = "x"
        with self.assertRaises(AppendOnlyError):
            event.save()
        with self.assertRaises(AppendOnlyError):
            event.delete()

    def test_engagement_delete_with_tasks(self):
        eng = self.engagement()
        tasks.create(title="a", engagement=eng)
        tasks.create(title="b", engagement=eng)
        eng.delete()
        self.assertFalse(Engagement.objects.filter(pk=eng.pk).exists())
        self.assertFalse(Task.objects.filter(engagement_id=eng.pk).exists())

    def test_engagement_delete_cascades_history(self):
        # Append-only is about rewriting history, not about keeping orphans.
        eng = self.engagement()
        task = tasks.create(title="t", engagement=eng)
        eng.delete()
        self.assertFalse(EngagementStageChange.objects.filter(engagement_id=eng.pk).exists())
        self.assertFalse(TaskEvent.objects.filter(task_id=task.pk).exists())


class KeyAndProtectTests(CrmTestCase):
    def test_pipeline_and_stage_keys_are_immutable(self):
        self.pipeline.key = "renamed"
        with self.assertRaises(ValidationError):
            self.pipeline.save()
        stage = self.stage("new")
        stage.key = "brand_new"
        with self.assertRaises(ValidationError):
            stage.save()
        stage.refresh_from_db()
        stage.name = "Fresh"  # names are tenant data
        stage.save()

    def test_stage_with_engagements_cannot_be_deleted(self):
        self.engagement()
        with self.assertRaises(ProtectedError):
            self.stage("new").delete()


class CompanyCopyOnSaveTests(CrmTestCase):
    def test_children_copy_company_from_parent(self):
        stage = Stage.objects.create(pipeline=self.other_pipeline, company=self.company,
                                     key="extra", name="Extra", sort_order=99)
        self.assertEqual(stage.company, self.other_company)
        eng = self.engagement()
        contact = EngagementContact.objects.create(engagement=eng, party=self.ada,
                                                   company=self.other_company)
        self.assertEqual(contact.company, self.company)
        self.assertEqual(eng.stage_changes.get().company, self.company)
        task = Task.objects.create(engagement=eng, company=self.other_company, title="t")
        self.assertEqual(task.company, self.company)
        self.assertEqual(task.party, self.org)  # defaults to the engagement's party
        event = TaskEvent.objects.create(task=task, company=self.other_company, event="created")
        self.assertEqual(event.company, self.company)

    def test_check_tenancy_covers_crm(self):
        from io import StringIO

        from django.core.management import call_command

        self.engagement()
        out = StringIO()
        call_command("check_tenancy", "--app", "crm", stdout=out)
        self.assertIn("0 mismatched", out.getvalue())


class ContactCleanTests(CrmTestCase):
    def test_contact_must_be_a_person_and_not_the_engagement_party(self):
        eng = self.engagement(party=self.ada)
        with self.assertRaises(ValidationError):
            EngagementContact(engagement=eng, party=self.org).clean()
        with self.assertRaises(ValidationError):
            EngagementContact(engagement=eng, party=self.ada).clean()
        EngagementContact(engagement=eng, party=person(self.company, "Grace", "Hopper")).clean()


class TouchpointTests(CrmTestCase):
    def test_channel_must_be_a_source_key(self):
        tp = Touchpoint(company=self.company, occurred_at=NOW, channel="carrier_pigeon",
                        source="lead_form")
        with self.assertRaises(ValidationError):
            tp.clean()
        tp.channel = "paid_search"
        tp.clean()

    def test_no_ip_address_column(self):
        self.assertNotIn("ip_address", {f.name for f in Touchpoint._meta.fields})


class TaskModelTests(CrmTestCase):
    def test_overdue_is_utc_instant_comparison(self):
        task = tasks.create(company=self.company, title="t",
                            due=timezone.now() - datetime.timedelta(minutes=1))
        self.assertTrue(task.is_overdue)
        tasks.complete(task)
        self.assertFalse(task.is_overdue)

    def test_clean_refuses_cross_tenant_assignee(self):
        from .helpers import profile

        outsider = profile(self.other_company, "outsider")
        with self.assertRaises(ValidationError) as ctx:
            tasks.create(company=self.company, title="t", assignee=outsider)
        self.assertIn("assignee", ctx.exception.message_dict)

    def test_clean_refuses_unknown_kind_and_bad_zone(self):
        task = Task(company=self.company, title="t", kind="telepathy", due_tz="Mars/Olympus",
                    due_at=NOW)
        with self.assertRaises(ValidationError) as ctx:
            task.full_clean()
        self.assertTrue({"kind", "due_tz"} <= set(ctx.exception.message_dict))
