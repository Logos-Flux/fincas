"""Step 5 end to end on the HubSpot adapter's own output (``crawl-small``).

The adapter's expected canonical files are staged as they are, with the
fixture's mapping file, and every row up to tickets is applied: a real
export's shapes (``stage_metadata`` on pipelines, label-less associations,
closed ticket stages) must import, and undo must restore every table.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import tomllib
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase

from core.models import Company
from crm.models import Engagement, EngagementContact
from crm_import import stage as staging
from crm_import import writelog
from crm_import.models import ImportJob
from parties.models import Party, PartyRelationship
from unify.models import StagedRow

from .test_appliers import ACTOR, apply_type, rows_of
from .test_appliers_deals import dump

FIXTURE = Path(settings.BASE_DIR) / "golden_erp" / "addons" / "hubspot_export" / "fixtures" / "crawl-small"
STEPS = ("owner", "pipeline", "company", "contact", "deal", "association", "relationship")
#: The reviewer's won/lost picks for the fixture's closed-but-unhinted stages (2.4).
STAGE_PICKS = {"hs_12345680": "lost", "active": "won", "ended": "lost"}


class CrawlSmallTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(name="Tenant A")
        Company.objects.create(name="Tenant B")
        Party.objects.create(company=self.co, kind="organisation", display_name="Tenant A", is_self=True,
                             origin="system")
        self.dir = Path(tempfile.mkdtemp(prefix="crm-crawl-small-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        for path in (FIXTURE / "expected").iterdir():
            shutil.copy(path, self.dir / path.name.replace(".expected", ""))
        self.mapping = tomllib.loads((FIXTURE / "mapping.toml").read_text(encoding="utf-8"))
        self.before = dump()

    def run_import(self, options=None):
        job = staging.stage(self.co, self.dir, mapping=self.mapping, options=options)
        self.assertEqual(job.status, ImportJob.Status.REVIEWING, job.report)
        for otype in STEPS:
            for row in rows_of(job, otype):
                if row.mapping_state != StagedRow.MappingState.PROPOSED:
                    continue
                staging.approve_row(row, ACTOR, create=(row.proposed_mapping or {}).get("decision") is None)
                if otype == "pipeline":
                    row.refresh_from_db()
                    StagedRow.objects.filter(pk=row.pk).update(
                        mapped_values={**row.mapped_values, "stage_outcomes": STAGE_PICKS})
            apply_type(job, otype)
            for row in rows_of(job, otype):
                if row.mapping_state == StagedRow.MappingState.FAILED and "outcome_conflict" in row.error:
                    staging.approve_row(row, ACTOR, outcome="lost")  # the reviewer's pick
            apply_type(job, otype)
        failed = {otype: [(r.raw.get("source_id"), r.error) for r in rows_of(job, otype)
                          if r.mapping_state == StagedRow.MappingState.FAILED] for otype in STEPS}
        self.assertEqual({k: v for k, v in failed.items() if v}, {})
        return job

    def test_the_fixture_imports_cleanly_and_undoes_exactly(self):
        from orders.models import Order, Quote

        job = self.run_import()
        deals = [r for r in rows_of(job, "deal") if r.mapping_state == StagedRow.MappingState.APPLIED]
        self.assertTrue(deals)
        self.assertEqual(Engagement.objects.filter(company=self.co).count(), len(deals))
        self.assertTrue(EngagementContact.objects.filter(company=self.co).exclude(role="other").exists())
        self.assertTrue(PartyRelationship.objects.filter(company=self.co, kind="partnership").exists())
        self.assertFalse(Quote._base_manager.exists())
        self.assertFalse(Order._base_manager.exists())
        out = io.StringIO()
        call_command("check_engagements", stdout=out)
        call_command("reconcile_engagements", "--check", stdout=out)
        self.assertIn("0 violation(s)", out.getvalue())
        self.assertIn("drift: 0 engagement(s)", out.getvalue())
        self.assertEqual(writelog.undo_job(job, actor=ACTOR).refused, [])
        self.assertEqual(dump(), self.before)

    def test_tickets_as_engagements(self):
        job = self.run_import({"tickets_as_engagements": True})
        tickets = [r for r in rows_of(job, "relationship") if r.mapping_state == StagedRow.MappingState.APPLIED]
        self.assertTrue(tickets)
        self.assertTrue(Engagement.objects.filter(company=self.co, pipeline__key="partners").exists())
        self.assertEqual(writelog.undo_job(job, actor=ACTOR).refused, [])
        self.assertEqual(dump(), self.before)
