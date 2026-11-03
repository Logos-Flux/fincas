"""Default pipeline seeding (1.2), check_engagements, and party merges (1.3)."""

from __future__ import annotations

from io import StringIO

from django.apps import apps as global_apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from core.models import Company
from crm import engagements, tasks
from crm.models import Engagement, EngagementContact, Pipeline, Stage, Task, Touchpoint
from crm.pipelines import seed_all_companies, seed_default_pipelines
from parties.merge import merge, unmerge

from .helpers import CrmTestCase, organisation, person

SERVICE = [{"key": "service", "name": "Service", "stages": [
    {"key": "triage", "name": "Triage", "default_probability": 10},
    {"key": "fixed", "name": "Fixed", "outcome_hint": "won"},
]}]


class SeedingTests(TestCase):
    def test_new_company_gets_golden_sales_pipeline(self):
        company = Company.objects.create(name="Fresh")
        (pipeline,) = Pipeline.objects.for_company(company)
        self.assertEqual((pipeline.key, pipeline.is_default, pipeline.stale_after_days),
                         ("sales", True, 14))
        self.assertEqual(list(pipeline.stages.values_list("key", "sort_order", "default_probability")), [
            ("new", 10, 5), ("prospecting", 20, 10), ("qualification", 30, 20),
            ("needs_analysis", 40, 30), ("proposal_sent", 50, 50), ("negotiation", 60, 70),
            ("verbal_commit", 70, 90), ("closed_signed", 80, 100),
        ])
        self.assertTrue(all(s.company_id == company.pk for s in pipeline.stages.all()))

    def test_seeding_is_idempotent_and_leaves_tenant_edits_alone(self):
        company = Company.objects.create(name="Fresh")
        pipeline = Pipeline.objects.get(company=company)
        pipeline.name = "Deals"
        pipeline.save()
        pipeline.stages.filter(key="verbal_commit").update(is_active=False)
        self.assertEqual(seed_default_pipelines(company), 0)
        self.assertEqual(seed_all_companies(), 0)
        self.assertEqual(Pipeline.objects.for_company(company).count(), 1)
        self.assertEqual(Stage.objects.filter(pipeline=pipeline).count(), 8)
        pipeline.refresh_from_db()
        self.assertEqual(pipeline.name, "Deals")
        self.assertFalse(pipeline.stages.get(key="verbal_commit").is_active)

    def test_closed_signed_is_appended_to_existing_sales_pipelines_once(self):
        from crm.pipelines import append_stage
        from crm.registries import CLOSED_SIGNED_STAGE

        company = Company.objects.create(name="Older")
        pipeline = Pipeline.objects.get(company=company)
        pipeline.stages.filter(key="closed_signed").delete()  # as it was before 2026-10-15
        pipeline.stages.filter(key="verbal_commit").update(sort_order=95)
        other = Pipeline.objects.create(company=company, key="service", name="Service", sort_order=1)
        self.assertEqual(append_stage("sales", CLOSED_SIGNED_STAGE), 1)
        self.assertEqual(append_stage("sales", CLOSED_SIGNED_STAGE), 0)
        stage = pipeline.stages.get(key="closed_signed")
        self.assertEqual((stage.name, stage.sort_order, stage.default_probability, stage.outcome_hint,
                          stage.company_id), ("Closed/Signed", 105, 100, "won", company.pk))
        self.assertEqual(list(pipeline.stages.values_list("key", flat=True))[-2:],
                         ["verbal_commit", "closed_signed"])
        self.assertFalse(other.stages.exists())

    def test_entering_closed_signed_proposes_winning(self):
        company = Company.objects.create(name="Signer")
        pipeline = Pipeline.objects.get(company=company)
        eng = engagements.create(company=company, party=organisation(company, "Signer Ltd"),
                                 pipeline=pipeline, title="Signer deal")
        result = engagements.move(eng, pipeline.stages.get(key="closed_signed"))
        self.assertEqual(result.needs_close, "won")
        eng.refresh_from_db()
        self.assertEqual(eng.status, "open")  # proposes, never closes silently

    def test_saving_an_existing_company_seeds_nothing(self):
        company = Company.objects.create(name="Fresh")
        Stage.objects.filter(company=company).delete()
        Pipeline.objects.filter(company=company).delete()
        company.name = "Renamed"
        company.save()
        self.assertFalse(Pipeline.objects.filter(company=company).exists())
        # ... until a seeding run (the data migration, or an explicit call).
        self.assertEqual(seed_all_companies(), 1)
        self.assertTrue(Pipeline.objects.get(company=company).is_default)

    @override_settings(GOLDEN_DEFAULT_PIPELINES=SERVICE)
    def test_deployment_pipelines_replace_the_seed(self):
        company = Company.objects.create(name="Fresh")
        (pipeline,) = Pipeline.objects.for_company(company)
        self.assertEqual(pipeline.key, "service")
        self.assertEqual(pipeline.stages.get(key="fixed").outcome_hint, "won")

    def test_new_seed_added_later_is_not_default_twice(self):
        company = Company.objects.create(name="Fresh")
        with override_settings(GOLDEN_DEFAULT_PIPELINES=[
            {"key": "sales", "name": "Sales", "stages": [{"key": "new", "name": "New"}]},
            *SERVICE,
        ]):
            self.assertEqual(seed_default_pipelines(company), 1)
        self.assertEqual(Pipeline.objects.get(company=company, is_default=True).key, "sales")
        self.assertFalse(Pipeline.objects.get(company=company, key="service").is_default)

    def test_data_migration_seeds_existing_companies_idempotently(self):
        # Tests run without migrations; call 0008's forward function directly.
        import importlib

        migration = importlib.import_module("crm.migrations.0008_seed_default_pipelines")
        Company.objects.create(name="Fresh")
        Stage.objects.all().delete()
        Pipeline.objects.all().delete()
        for _ in range(2):
            migration.seed(global_apps, None)
        self.assertEqual(Pipeline.objects.filter(is_default=True).count(), Company.objects.count())
        self.assertEqual(Stage.objects.count(), 8 * Company.objects.count())


class CheckEngagementsTests(CrmTestCase):
    def run_check(self):
        out = StringIO()
        try:
            call_command("check_engagements", stdout=out)
        except CommandError:
            return False, out.getvalue()
        return True, out.getvalue()

    def test_clean_database_passes(self):
        eng = self.engagement()
        tasks.create(title="t", engagement=eng)
        ok, out = self.run_check()
        self.assertTrue(ok, out)
        self.assertIn("0 violation(s)", out)

    def test_reports_each_invariant(self):
        eng = self.engagement()
        foreign_stage = self.stage("new", self.other_pipeline)
        Engagement.objects.filter(pk=eng.pk).update(stage=foreign_stage, source="carrier_pigeon")
        task = tasks.create(title="t", engagement=eng)
        Task.objects.filter(pk=task.pk).update(kind="telepathy", due_tz="Mars/Olympus")
        other = self.engagement()
        Engagement.objects.filter(pk=other.pk).update(next_task=task)
        EngagementContact.objects.bulk_create([
            EngagementContact(engagement=other, company=self.company, party=self.org),
        ])
        Pipeline.objects.filter(company=self.other_company).update(is_default=False)
        ok, out = self.run_check()
        self.assertFalse(ok)
        for line in (
            "engagement stage outside its pipeline: 1",
            "engagement stage in another company: 1",
            "engagement source not in GOLDEN_ENGAGEMENT_SOURCES: 1",
            "engagement next_task not an open task of the engagement: 1",
            "engagement contact is not a person: 1",
            "engagement contact duplicates the engagement's party: 1",
            "task kind not in GOLDEN_TASK_KINDS: 1",
            "task due_tz not a known IANA zone: 1",
            "company without a default pipeline: 1",
        ):
            self.assertIn(line, out)


class PartyMergeTests(CrmTestCase):
    """Phase 3 1.3: a merge retargets engagement links; unmerge restores them."""

    def test_merge_retargets_and_collision_keeps_survivor_contact(self):
        survivor = person(self.company, "Ada", "Lovelace")
        loser = person(self.company, "Ada", "L.")
        eng = self.engagement(party=self.org)
        shared = self.engagement(party=self.org)
        owned = self.engagement(party=loser)
        EngagementContact.objects.create(engagement=eng, party=loser, role="billing")
        EngagementContact.objects.create(engagement=shared, party=survivor, role="champion")
        EngagementContact.objects.create(engagement=shared, party=loser, role="technical")
        task = tasks.create(title="t", engagement=eng, party=loser)
        touch = Touchpoint.objects.create(company=self.company, party=loser, occurred_at=eng.created_at,
                                          source="lead_form")
        (log,) = merge(survivor, [loser], actor="tester")
        owned.refresh_from_db()
        self.assertEqual(owned.party, survivor)
        self.assertEqual(EngagementContact.objects.get(engagement=eng).party, survivor)
        (kept,) = EngagementContact.objects.filter(engagement=shared)
        self.assertEqual((kept.party, kept.role), (survivor, "champion"))
        task.refresh_from_db()
        touch.refresh_from_db()
        self.assertEqual((task.party, touch.party), (survivor, survivor))
        self.assertIn("crm.EngagementContact", log.removed)

        unmerge(log)
        owned.refresh_from_db()
        self.assertEqual(owned.party, loser)
        self.assertEqual(
            set(EngagementContact.objects.filter(engagement=shared).values_list("party", "role")),
            {(survivor.pk, "champion"), (loser.pk, "technical")},
        )

    def test_unmerge_after_deal_activity_restores_links_not_deal_state(self):
        survivor = person(self.company, "Ada", "Lovelace")
        loser = person(self.company, "Ada", "L.")
        eng = self.engagement(party=loser)
        (log,) = merge(survivor, [loser], actor="tester")
        task = tasks.create(title="after the merge", engagement=eng)
        engagements.move(eng, self.stage("proposal_sent"))
        unmerge(log)  # not refused: only the party link is in the ledger
        eng.refresh_from_db()
        self.assertEqual((eng.party, eng.stage.key, eng.next_task_id), (loser, "proposal_sent", task.pk))
        out = StringIO()
        call_command("reconcile_engagements", stdout=out)
        self.assertIn("drift: 0", out.getvalue())

    def test_survivor_as_engagement_party_drops_implicit_contact_rows(self):
        survivor = person(self.company, "Ada", "Lovelace")
        loser = person(self.company, "Ada", "L.")
        mine = self.engagement(party=survivor)
        theirs = self.engagement(party=loser)
        EngagementContact.objects.create(engagement=mine, party=loser, role="billing")
        EngagementContact.objects.create(engagement=theirs, party=survivor, role="champion")
        (log,) = merge(survivor, [loser], actor="tester")
        self.assertFalse(EngagementContact.objects.filter(engagement__in=[mine, theirs]).exists())
        ok, out = CheckEngagementsTests.run_check(self)
        self.assertTrue(ok, out)
        unmerge(log)
        self.assertEqual(EngagementContact.objects.filter(engagement__in=[mine, theirs]).count(), 2)

    def test_merge_refuses_nothing_new_for_organisations(self):
        survivor = organisation(self.company, "Initech")
        loser = organisation(self.company, "Initech Inc")
        eng = self.engagement(party=loser)
        merge(survivor, [loser], actor="tester")
        eng.refresh_from_db()
        self.assertEqual(eng.party, survivor)


class ClosedSignedMigrationTests(TestCase):
    """``crm.0019``'s function on live ``apps`` (CLAUDE.md: test data migrations directly)."""

    def setUp(self):
        import importlib

        self.add_stage = importlib.import_module("crm.migrations.0019_closed_signed_stage").add_stage
        self.company = Company.objects.create(name="Pre-0019")
        self.pipeline = Pipeline.objects.get(company=self.company, key="sales")
        self.pipeline.stages.filter(key="closed_signed").delete()

    def closed(self):
        return Stage.objects.filter(company=self.company, key="closed_signed")

    def test_appends_once(self):
        self.add_stage(global_apps, None)
        self.add_stage(global_apps, None)
        self.assertEqual(self.closed().count(), 1)
        self.assertEqual(list(self.pipeline.stages.order_by("sort_order").values_list("key", flat=True))[-1],
                         "closed_signed")

    def test_skips_when_the_deployment_owns_its_pipelines(self):
        with override_settings(GOLDEN_DEFAULT_PIPELINES=SERVICE):
            self.add_stage(global_apps, None)
        self.assertFalse(self.closed().exists())

    def test_leaves_a_pipeline_that_already_has_its_own_closing_stage(self):
        Stage.objects.create(pipeline=self.pipeline, company=self.company, key="signed", name="Contract signed",
                             sort_order=200, default_probability=100, outcome_hint="won")
        self.add_stage(global_apps, None)
        self.assertFalse(self.closed().exists())
        self.assertEqual(self.pipeline.stages.filter(outcome_hint="won").count(), 1)
