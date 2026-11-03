"""Step 8: the runner, the dry run, the report and the CLI (docs/crm/phase5-importer.md 4.1-4.5, 4.10).

The end-to-end test runs the HubSpot adapter's own ``crawl-small`` fixture
the way an operator does: ``crm_export_adapt`` → ``check_canonical_export`` →
``crm_import stage`` → ``crm_import run --dry-run`` (nothing changes) →
scripted review decisions → ``crm_import run`` → the reviewer settles the
outcome conflicts → ``crm_import retry`` → the report equals
``expected/import-report.expected.json`` byte for byte (job id and time
normalised) → ``crm_import undo`` restores every table the appliers write.

Set ``CRM_REPORT_REGEN=1`` to rewrite the expected file after a deliberate
change, then review its diff.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import tomllib
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import Permission, User
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from core.models import Company, UserProfile
from crm_import import report, runner
from crm_import import stage as staging
from crm_import.models import ImportJob
from parties.models import Party
from unify.apply import start_apply_run
from unify.models import ApprovalRow, ApprovalRun, StagedRow

from .test_appliers import ACTOR, rows_of
from .test_appliers_activity import dump

FIXTURE = Path(settings.BASE_DIR) / "golden_erp" / "addons" / "hubspot_export" / "fixtures" / "crawl-small"
EXPECTED = FIXTURE / "expected" / "import-report.expected.json"
#: The reviewer's won/lost picks for the fixture's closed-but-unhinted stages (2.4).
STAGE_PICKS = {"hs_12345680": "lost", "active": "won", "ended": "lost"}
MS = StagedRow.MappingState
PLUGIN = "golden_erp.addons.hubspot_export.plugin.HubSpotExportPlugin"


def count_every_table() -> dict:
    """Row counts of every installed model (the dry run's done-when)."""
    return {m._meta.label: m._base_manager.count() for m in apps.get_models() if m._meta.managed}


def normalise(built: dict) -> dict:
    out = json.loads(json.dumps(built))
    out["job"] = "<job>"
    out["generated_at"] = "<time>"
    return out


def fixture_text() -> str:
    """Every personal value of the fixture: no report may contain one."""
    return "\n".join(p.read_text(encoding="utf-8") for p in (FIXTURE / "expected").glob("*.jsonl"))


class Operator:
    """Drives the CLI on a fresh copy of crawl-small (adapt → check → stage)."""

    def __init__(self, testcase, company):
        self.t = testcase
        self.company = company
        self.root = Path(tempfile.mkdtemp(prefix="crm-runner-"))
        testcase.addCleanup(shutil.rmtree, self.root, True)
        self.canonical = self.root / "canonical"
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.mapping = FIXTURE / "mapping.toml"

    def call(self, *args) -> str:
        out = io.StringIO()
        call_command(*args, stdout=out, stderr=io.StringIO())
        return out.getvalue()

    def adapt_check_stage(self, *extra) -> ImportJob:
        with override_settings(GOLDEN_PLUGINS=[PLUGIN]):
            self.call("crm_export_adapt", "--provider", "hubspot", "--input", str(FIXTURE),
                      "--output", str(self.canonical), "--mapping", str(self.mapping))
        self.t.assertIn("OK: ", self.call("check_canonical_export", str(self.canonical)))
        self.call("crm_import", "stage", "--company", str(self.company.pk), "--dir", str(self.canonical),
                  "--mapping", str(self.mapping), *extra)
        job = ImportJob.objects.get(company=self.company)
        self.t.assertEqual(job.status, ImportJob.Status.REVIEWING, job.report)
        return job

    def run(self, job, *extra) -> str:
        return self.call("crm_import", "run", "--job", str(job.pk), "--report-dir", str(self.reports),
                         "--actor", ACTOR, *extra)

    def report(self, name="import-report.json") -> dict:
        return json.loads((self.reports / name).read_text(encoding="utf-8"))


def review(job):
    """The scripted reviewer: approve every proposal (ambiguous rows become new
    records) and pick won/lost for the closed stages the pipelines left unhinted."""
    for otype in runner.STEP_ORDER:
        for row in rows_of(job, otype):
            if row.mapping_state != MS.PROPOSED:
                continue
            staging.approve_row(row, ACTOR, create=(row.proposed_mapping or {}).get("decision") is None)
            if otype == "pipeline":
                row.refresh_from_db()
                StagedRow.objects.filter(pk=row.pk).update(
                    mapped_values={**row.mapped_values, "stage_outcomes": STAGE_PICKS})


def settle_outcomes(job) -> int:
    """The reviewer settles each deal that failed ``outcome_conflict`` as lost."""
    n = 0
    for row in rows_of(job, "deal"):
        if row.mapping_state == MS.FAILED and "outcome_conflict" in row.error:
            staging.approve_row(row, ACTOR, outcome="lost")
            n += 1
    return n


class CrawlSmallEndToEndTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(name="Tenant A")
        Company.objects.create(name="Tenant B")
        Party.objects.create(company=self.co, kind="organisation", display_name="Tenant A", is_self=True,
                             origin="system")
        self.op = Operator(self, self.co)

    def test_adapt_validate_stage_run_report_undo(self):
        before = dump()
        job = self.op.adapt_check_stage()

        # Dry run: every table keeps its row count, the job its state.
        counts = count_every_table()
        staged = list(StagedRow.objects.order_by("pk").values_list("pk", "mapping_state", "mapped_values", "error"))
        self.op.run(job, "--dry-run")
        self.assertEqual(count_every_table(), counts)
        self.assertEqual(list(StagedRow.objects.order_by("pk").values_list(
            "pk", "mapping_state", "mapped_values", "error")), staged)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.REVIEWING)
        dry = self.op.report("import-report.dry-run.json")
        self.assertTrue(dry["dry_run"])
        self.assertGreater(dry["totals"]["applied"], 0)

        review(job)
        self.op.run(job)
        settle_outcomes(job)  # deals the reviewer must settle (2.5), if any
        self.op.call("crm_import", "retry", "--job", str(job.pk), "--code", "invalid_value",
                     "--report-dir", str(self.op.reports), "--actor", ACTOR)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.APPLIED, self.op.report()["totals"])

        got = report.dumps(normalise(self.op.report()))
        if os.environ.get("CRM_REPORT_REGEN"):
            EXPECTED.write_text(got, encoding="utf-8")
        self.assertEqual(got, EXPECTED.read_text(encoding="utf-8"))
        for value in ("@example.com", "555-01"):
            self.assertIn(value, fixture_text())  # the probe is real ...
            self.assertNotIn(value, got)  # ... and the report holds none of it

        self.op.call("crm_import", "undo", "--job", str(job.pk), "--actor", ACTOR)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.UNDONE)
        self.assertEqual(dump(), before)

        # Undone is not forgotten (4.5): re-applying is one run.
        self.op.run(job)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.APPLIED)

    def test_status_prints_every_step_and_the_consent_line(self):
        job = self.op.adapt_check_stage()
        out = self.op.call("crm_import", "status", "--job", str(job.pk))
        for otype in runner.STEP_ORDER:
            self.assertIn(otype, out)
        self.assertIn("will have no marketing consent", out)
        built = json.loads(self.op.call("crm_import", "status", "--job", str(job.pk), "--json"))
        self.assertEqual(sorted(built["types"]), sorted(runner.STEP_ORDER))

    def test_a_crash_mid_run_leaves_a_resumable_job(self):
        job = self.op.adapt_check_stage()
        review(job)
        real = runner.apply_chunk
        calls = {"n": 0}

        def crash(run_id, row_ids, chunk_size=200):
            calls["n"] += 1
            if calls["n"] == 6:
                raise ConnectionError("the database went away")
            return real(run_id, row_ids, chunk_size=chunk_size)

        with mock.patch.object(runner, "apply_chunk", crash), self.assertRaises(ConnectionError):
            runner.run_job(job, actor=ACTOR, chunk_size=1)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.APPLYING)
        self.assertTrue(ApprovalRow.objects.filter(state=ApprovalRow.State.PENDING).exists())
        result = runner.run_job(job, actor=ACTOR)
        self.assertFalse(ApprovalRow.objects.filter(state=ApprovalRow.State.PENDING).exists())
        self.assertFalse(ApprovalRun.objects.exclude(status=ApprovalRun.Status.DONE).exists())
        # Exactly what an uninterrupted run gives: nothing applied twice.
        self.assertEqual(result.report["totals"]["applied"],
                         StagedRow.objects.filter(mapping_state=MS.APPLIED).count())
        self.assertEqual(result.job.status, ImportJob.Status.APPLIED)


class CommittedButUnmarkedTests(TestCase):
    """4.10: a row's writes committed, its ApprovalRow update lost; the run is
    then cancelled and a new run applies the row again. It must not write twice."""

    def setUp(self):
        self.co = Company.objects.create(name="Tenant A")
        Party.objects.create(company=self.co, kind="organisation", display_name="Tenant A", is_self=True,
                             origin="system")
        self.job = Operator(self, self.co).adapt_check_stage()
        review(self.job)

    def test_a_new_run_finds_the_earlier_write(self):
        import unify.apply as unify_apply
        from unify.apply import cancel_run

        real, seen = unify_apply._mark, {"n": 0}

        def lost(approval_row, state):
            if state == ApprovalRow.State.APPLIED and approval_row.staged_row.raw.get("object_type") == "contact":
                seen["n"] += 1
                if seen["n"] == 2:
                    raise ConnectionError("the process died after the commit")
            return real(approval_row, state)

        with mock.patch.object(unify_apply, "_mark", lost), self.assertRaises(ConnectionError):
            runner.run_job(self.job, actor=ACTOR)
        for run in ApprovalRun.objects.filter(status__in=("open", "applying")):
            cancel_run(str(run.pk))
        result = runner.run_job(self.job, actor=ACTOR)
        self.assertEqual(result.report["totals"]["failed"], 0)
        applied = rows_of(self.job, "contact")
        people = Party.objects.filter(company=self.co, kind="person").count()
        self.assertEqual(people, sum(1 for r in applied if r.mapping_state == MS.APPLIED
                                     and r.mapped_values.get("decision") == "create"))


class GateTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(name="Tenant A")
        Party.objects.create(company=self.co, kind="organisation", display_name="Tenant A", is_self=True,
                             origin="system")
        self.op = Operator(self, self.co)
        self.job = self.op.adapt_check_stage()
        review(self.job)

    def test_through_stops_after_the_step_and_a_rerun_resumes(self):
        result = runner.run_job(self.job, actor=ACTOR, through="contact")
        states = {s.object_type: s.state for s in result.steps}
        self.assertEqual(states["contact"], "applied")
        self.assertEqual(states["deal"], "not_reached")
        self.assertFalse(StagedRow.objects.filter(staging.job_rows_filter([self.job.pk], "deal"),
                                                  mapping_state=MS.APPLIED).exists())
        result = runner.run_job(self.job, actor=ACTOR)
        states = {s.object_type: s.state for s in result.steps}
        self.assertEqual([states[t] for t in ("owner", "pipeline", "company", "contact")], ["complete"] * 4)
        self.assertEqual(states["deal"], "applied")

    def test_a_step_waits_for_approved_rows_upstream(self):
        runner.run_job(self.job, actor=ACTOR, through="contact")
        contact = next(r for r in rows_of(self.job, "contact") if r.mapping_state == MS.APPLIED)
        StagedRow.objects.filter(pk=contact.pk).update(mapping_state=MS.APPROVED)  # e.g. re-decided since
        self.assertEqual(runner.gate(self.job, "deal"), {"contact": 1})
        # A retry of failed rows only does not apply the contact, so deals stay shut.
        StagedRow.objects.filter(staging.job_rows_filter([self.job.pk], "deal")).update(
            mapping_state=MS.FAILED, error="dependency_missing: test")
        with self.assertRaisesMessage(CommandError, "deal cannot be applied yet"):
            self.op.call("crm_import", "retry", "--job", str(self.job.pk), "--no-report-file", "--actor", ACTOR)
        self.assertFalse(StagedRow.objects.filter(staging.job_rows_filter([self.job.pk], "deal"),
                                                  mapping_state=MS.APPLIED).exists())

    def test_generic_batch_screen_and_unify_apply_refuse_crm_batches(self):
        batch = self.job.job_batches.get(object_type="owner").batch
        with self.assertRaisesMessage(CommandError, "crm_import run --job"):
            call_command("unify_apply", "--batch", str(batch.pk), "--actor", ACTOR, stdout=io.StringIO())
        staff = User.objects.create_user("ops", password="x", is_staff=True)
        UserProfile.objects.update_or_create(user=staff, defaults={"company": self.co})
        staff.user_permissions.add(Permission.objects.get(codename="apply_import"))
        self.client.force_login(staff)
        self.client.post(f"/erp/imports/{batch.pk}/", {"action": "start-apply"})
        self.assertFalse(ApprovalRun.objects.filter(batch=batch).exists())
        # Nor may they drive or cancel a run the runner opened.
        run = start_apply_run(batch, ACTOR)
        self.client.post(f"/erp/imports/{batch.pk}/", {"action": "apply-chunk", "run": run.pk})
        self.client.post(f"/erp/imports/{batch.pk}/", {"action": "cancel-run", "run": run.pk})
        run.refresh_from_db()
        self.assertEqual(run.status, ApprovalRun.Status.OPEN)
        self.assertFalse(run.rows.exclude(state=ApprovalRow.State.PENDING).exists())
        with self.assertRaisesMessage(CommandError, "crm_import run --job"):
            call_command("unify_apply", "--run", str(run.pk), stdout=io.StringIO())

    def test_undo_one_step_waits_for_its_dependents(self):
        runner.run_job(self.job, actor=ACTOR, through="contact")
        with self.assertRaisesMessage(runner.RunnerError, "company cannot be undone yet"):
            runner.undo_batch(self.job, "company", actor=ACTOR)
        out = runner.undo_batch(self.job, "contact", actor=ACTOR)
        self.assertEqual(out.refused, [])
        self.assertFalse(StagedRow.objects.filter(staging.job_rows_filter([self.job.pk], "contact"),
                                                  mapping_state=MS.APPLIED).exists())
        runner.undo_batch(self.job, "company", actor=ACTOR)

    def test_a_real_run_refuses_to_start_inside_a_transaction(self):
        from django.db import transaction

        with transaction.atomic(), self.assertRaisesMessage(runner.RunnerError, "inside a transaction"):
            runner.run_job(self.job, actor=ACTOR)
        with transaction.atomic():  # a dry run is one by design
            self.assertTrue(runner.run_job(self.job, actor=ACTOR, dry_run=True).dry_run)

    def test_states_refused(self):
        ImportJob.objects.filter(pk=self.job.pk).update(status=ImportJob.Status.CANCELLED)
        with self.assertRaisesMessage(CommandError, "is cancelled"):
            self.op.run(self.job)

    def test_cancel_frees_the_source_and_refuses_once_applied(self):
        out = self.op.call("crm_import", "cancel", "--job", str(self.job.pk))
        self.assertIn("canceled", out)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ImportJob.Status.CANCELLED)
        job2 = staging.stage(self.co, self.op.canonical, mapping=tomllib.loads(
            self.op.mapping.read_text(encoding="utf-8")))
        review(job2)
        runner.run_job(job2, actor=ACTOR, through="owner")
        with self.assertRaisesMessage(runner.RunnerError, "has applied rows"):
            runner.cancel_job(job2)

    def test_another_job_of_the_source_applying_blocks_the_run(self):
        busy = ImportJob.objects.create(company=self.co, source=self.job.source, provider="hubspot",
                                        status=ImportJob.Status.APPLYING)
        with self.assertRaisesMessage(runner.RunnerError, f"already being imported by job {busy.pk}"):
            runner.run_job(self.job, actor=ACTOR)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ImportJob.Status.REVIEWING)


class ReportPageTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(name="Tenant A")
        self.other = Company.objects.create(name="Tenant B")
        Party.objects.create(company=self.co, kind="organisation", display_name="Tenant A", is_self=True,
                             origin="system")
        self.job = Operator(self, self.co).adapt_check_stage()

    def login(self, company, **extra):
        user = User.objects.create_user(f"u{User.objects.count()}", password="x", is_staff=True, **extra)
        UserProfile.objects.update_or_create(user=user, defaults={"company": company})
        self.client.force_login(user)
        return user

    def url(self, job=None):
        return f"/erp/imports/crm/{(job or self.job).pk}/report/"

    def test_staff_of_the_tenant_see_the_report(self):
        self.login(self.co)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Import report", body)
        self.assertIn("Engagements", body)  # labels, not object types
        self.assertIn("no marketing consent", body)
        self.assertNotIn("@example.com", body)

    def test_labels_follow_the_deployment(self):
        self.login(self.co)
        with self.settings(GOLDEN_LABEL_OVERRIDES={"crm.engagement": {"singular": "Idea", "plural": "Ideas",
                                                                       "article": "an"}}):
            body = self.client.get(self.url()).content.decode()
        self.assertIn("Ideas", body)

    def test_another_tenant_gets_404(self):
        self.login(self.other)
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_non_staff_are_refused(self):
        user = User.objects.create_user("nostaff", password="x")
        self.client.force_login(user)
        self.assertNotEqual(self.client.get(self.url()).status_code, 200)
