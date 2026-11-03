"""Job models, permissions and the source lock (docs/crm/phase5-importer.md step 2)."""

from __future__ import annotations

import threading
import time
import unittest
from io import StringIO

from django.apps import apps
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, models, transaction
from django.db.transaction import TransactionManagementError
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from core.models import Company, UserProfile
from crm_import.canonical.schema import OBJECT_TYPES
from crm_import.locks import SourceBusy, begin_staging, lock_source
from crm_import.models import (
    STEPS,
    ImportJob,
    ImportJobBatch,
    ImportSourceLock,
    ImportWriteLog,
    entity_kind_for,
)
from unify.models import ApprovalRow, ApprovalRun, ImportBatch, StagedRow

SOURCE = "hubspot:1234567"
DIGEST = "0" * 64


def job(company, source=SOURCE, **kwargs):
    return ImportJob.objects.create(company=company, source=source, provider="hubspot", **kwargs)


def crm_batch(company, object_type="contact", source=SOURCE):
    return ImportBatch.objects.create(
        company=company, source_kind=ImportBatch.SourceKind.CRM, source_name=source,
        entity_kind=entity_kind_for(object_type),
    )


def approval_row(batch):
    staged = StagedRow.objects.create(batch=batch, row_number=1, raw={}, content_hash="h")
    run = ApprovalRun.objects.create(batch=batch, kind=ApprovalRun.Kind.APPLY, actor="t")
    return ApprovalRow.objects.create(run=run, staged_row=staged)


class JobModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other = Company.objects.create(name="Tenant B")

    def assertViolates(self, name, create):
        with self.assertRaises(IntegrityError) as ctx, transaction.atomic():
            create()
        constraint = next(
            c for model in apps.get_app_config("crm_import").get_models()
            for c in model._meta.constraints if c.name == name
        )
        if connection.vendor == "postgresql" or isinstance(constraint, models.CheckConstraint):
            self.assertIn(name, str(ctx.exception))

    # --- ImportJob ---------------------------------------------------------

    def test_defaults_never_claim_complete_coverage(self):
        j = job(self.company)
        self.assertEqual(j.status, ImportJob.Status.VALIDATING)
        self.assertEqual(j.coverage, ImportJob.Coverage.PARTIAL)
        self.assertEqual((j.options, j.report, j.mapping), ({}, {}, {}))

    def test_every_status_fits_the_column(self):
        width = ImportJob._meta.get_field("status").max_length
        self.assertTrue(all(len(s) <= width for s in ImportJob.Status.values))
        for status in ImportJob.Status.values:
            job(self.company, status=status)

    def test_job_constraints(self):
        self.assertViolates("crmimportjob_status_valid", lambda: job(self.company, status="done"))
        self.assertViolates(
            "crmimportjob_coverage_valid", lambda: job(self.company, coverage="unknown")
        )
        self.assertViolates("crmimportjob_source_not_blank", lambda: job(self.company, source=""))
        self.assertViolates(
            "crmimportjob_provider_not_blank",
            lambda: ImportJob.objects.create(company=self.company, source=SOURCE, provider=""),
        )
        j = job(self.company)
        self.assertViolates(
            "crmimportjob_previous_not_self",
            lambda: ImportJob.objects.filter(pk=j.pk).update(previous_job=j),
        )

    def test_many_jobs_per_source(self):
        first = job(self.company, status=ImportJob.Status.APPLIED)
        second = job(self.company, previous_job=first)
        self.assertEqual(list(first.reimports.all()), [second])

    def test_previous_job_must_share_company_and_source(self):
        foreign = job(self.other)
        with self.assertRaisesMessage(ValidationError, "another company"):
            job(self.company, previous_job=foreign)
        elsewhere = job(self.company, source="hubspot:999")
        with self.assertRaisesMessage(ValidationError, "another source"):
            job(self.company, previous_job=elsewhere)

    def test_company_is_protected(self):
        job(self.company)
        with self.assertRaises(models.ProtectedError):
            self.company.delete()

    def test_for_company_and_for_request_scope_jobs(self):
        mine, theirs = job(self.company), job(self.other)
        self.assertEqual(list(ImportJob.objects.for_company(self.company)), [mine])
        user = User.objects.create_user("rep")
        UserProfile.objects.create(user=user, company=self.company, display_name="Rep")
        request = RequestFactory().get("/")
        request.user = user
        self.assertEqual(list(ImportJob.objects.for_request(request)), [mine])
        self.assertNotIn(theirs, ImportJob.objects.for_request(request))

    # --- ImportJobBatch ----------------------------------------------------

    def test_steps_follow_the_canonical_apply_order(self):
        applied = [t for t in OBJECT_TYPES if t != "property_def"]
        self.assertEqual(list(STEPS), applied)
        self.assertEqual(list(STEPS.values()), list(range(1, len(applied) + 1)))
        for object_type in STEPS:
            self.assertLessEqual(
                len(entity_kind_for(object_type)),
                ImportBatch._meta.get_field("entity_kind").max_length,
            )

    def test_job_batch_derives_step_and_company(self):
        j = job(self.company)
        link = ImportJobBatch.objects.create(
            job=j, batch=crm_batch(self.company, "deal"), object_type="deal", company=self.other
        )
        self.assertEqual((link.step, link.company_id), (5, self.company.id))
        self.assertEqual(link.batch.crm_import_job_batch, link)

    def test_job_batch_constraints(self):
        j = job(self.company)
        ImportJobBatch.objects.create(job=j, batch=crm_batch(self.company), object_type="contact")
        self.assertViolates(
            "uniq_crmimportjobbatch_job_type",
            lambda: ImportJobBatch.objects.create(
                job=j, batch=crm_batch(self.company), object_type="contact"
            ),
        )
        link = ImportJobBatch.objects.get(job=j)
        self.assertViolates(
            "crmimportjobbatch_step_matches_type",
            lambda: ImportJobBatch.objects.filter(pk=link.pk).update(step=1),
        )

    def test_job_batch_refuses_a_foreign_or_non_crm_batch(self):
        j = job(self.company)
        with self.assertRaisesMessage(ValidationError, "another company"):
            ImportJobBatch.objects.create(
                job=j, batch=crm_batch(self.other), object_type="contact"
            )
        csv = ImportBatch.objects.create(
            company=self.company, source_kind=ImportBatch.SourceKind.CSV, source_name=SOURCE
        )
        with self.assertRaisesMessage(ValidationError, "source_kind=crm"):
            ImportJobBatch.objects.create(job=j, batch=csv, object_type="contact")
        with self.assertRaisesMessage(ValidationError, "another source"):
            ImportJobBatch.objects.create(
                job=j, batch=crm_batch(self.company, source="hubspot:999"), object_type="contact"
            )
        with self.assertRaisesMessage(ValidationError, "entity_kind=crm_deal"):
            ImportJobBatch.objects.create(job=j, batch=crm_batch(self.company), object_type="deal")

    # --- ImportWriteLog ----------------------------------------------------

    def _log(self, j, row, seq=1, action="create", before=None, **kwargs):
        target = kwargs.pop("target", self.company)
        return ImportWriteLog.objects.create(
            job=j, approval_row=row, seq=seq, action=action, before=before,
            content_type=ContentType.objects.get_for_model(target), object_id=str(target.pk),
            after_digest=DIGEST, **kwargs,
        )

    def _job_with_row(self, company=None):
        company = company or self.company
        j = job(company)
        batch = crm_batch(company)
        ImportJobBatch.objects.create(job=j, batch=batch, object_type="contact")
        return j, approval_row(batch)

    def test_write_log_names_its_target_and_copies_company(self):
        j, row = self._job_with_row()
        entry = self._log(j, row, company=self.other)
        self.assertEqual(entry.company_id, self.company.id)
        self.assertEqual(entry.target, self.company)
        self.assertIsNone(entry.before)

    def test_write_log_constraints(self):
        j, row = self._job_with_row()
        self._log(j, row, seq=1)
        self.assertViolates("uniq_crmimportwritelog_job_seq", lambda: self._log(j, row, seq=1))
        self.assertViolates("crmimportwritelog_seq_positive", lambda: self._log(j, row, seq=0))
        self.assertViolates(
            "crmimportwritelog_action_valid", lambda: self._log(j, row, seq=2, action="delete")
        )
        self.assertViolates(
            "crmimportwritelog_before_iff_update", lambda: self._log(j, row, seq=3, action="update")
        )
        self.assertViolates(
            "crmimportwritelog_before_iff_update",
            lambda: self._log(j, row, seq=4, action="create", before={"name": "x"}),
        )
        self.assertViolates(
            "crmimportwritelog_before_iff_update",
            lambda: self._log(j, row, seq=5, action="link", before={}),
        )
        self.assertViolates(
            "crmimportwritelog_digest_not_blank",
            lambda: ImportWriteLog.objects.filter(seq=1).update(after_digest=""),
        )
        self.assertViolates(
            "crmimportwritelog_target_not_blank",
            lambda: ImportWriteLog.objects.filter(seq=1).update(object_id=""),
        )

    def test_update_keeps_the_full_before_row(self):
        j, row = self._job_with_row()
        before = {f.attname: str(f.value_from_object(self.company))
                  for f in Company._meta.concrete_fields}
        entry = self._log(j, row, action="update", before=before)
        entry.refresh_from_db()
        self.assertEqual(entry.before, before)

    def test_write_log_is_append_only_except_undone_at(self):
        j, row = self._job_with_row()
        entry = self._log(j, row)
        entry.after_digest = "1" * 64
        with self.assertRaisesMessage(ValueError, "append-only"):
            entry.save()
        with self.assertRaisesMessage(ValueError, "append-only"):
            entry.save(update_fields=["after_digest"])
        entry.refresh_from_db()
        entry.undone_at, entry.undone_by = timezone.now(), "reviewer"
        entry.save(update_fields=["undone_at", "undone_by"])
        entry.refresh_from_db()
        self.assertIsNotNone(entry.undone_at)
        self.assertEqual(entry.undone_by, "reviewer")
        self.assertEqual(entry.after_digest, DIGEST)

    def test_write_log_refuses_an_approval_row_outside_the_job(self):
        j, _ = self._job_with_row()
        _, foreign_row = self._job_with_row(self.other)
        with self.assertRaisesMessage(ValidationError, "outside this job"):
            self._log(j, foreign_row)
        _, own_company_row = self._job_with_row()
        with self.assertRaisesMessage(ValidationError, "outside this job"):
            self._log(j, own_company_row)

    def test_write_log_refuses_a_target_in_another_tenant(self):
        j, row = self._job_with_row()
        theirs = job(self.other)
        with self.assertRaisesMessage(ValidationError, "another company"):
            self._log(j, row, target=theirs)
        self._log(j, row, target=job(self.company))

    def test_write_log_stores_the_canonical_pk_text(self):
        j, row = self._job_with_row()
        target = job(self.company)
        entry = ImportWriteLog.objects.create(
            job=j, approval_row=row, seq=1, action="create",
            content_type=ContentType.objects.get_for_model(ImportJob),
            object_id=target.pk.hex, after_digest=DIGEST,
        )
        self.assertEqual(entry.object_id, str(target.pk))
        self.assertEqual(entry.target, target)

    def test_deleting_a_job_deletes_its_log_and_links(self):
        j, row = self._job_with_row()
        self._log(j, row)
        j.delete()
        self.assertFalse(ImportWriteLog.objects.exists())
        self.assertFalse(ImportJobBatch.objects.exists())

    def test_check_tenancy_is_clean(self):
        j, row = self._job_with_row()
        self._log(j, row)
        job(self.company, previous_job=j)
        call_command("check_tenancy", "--app", "crm_import", stdout=StringIO())
        # A copy written around save() is caught, against the job and the run's batch.
        ImportWriteLog.objects.update(company=self.other)
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command("check_tenancy", "--app", "crm_import", stdout=out)
        self.assertIn("crm_import.ImportWriteLog: 1 row(s) whose company differs from job.company",
                      out.getvalue())
        self.assertIn("approval_row__run__batch.company", out.getvalue())

    # --- permissions -------------------------------------------------------

    def test_permissions_exist_and_gate(self):
        ct = ContentType.objects.get_for_model(ImportJob)
        perms = dict(Permission.objects.filter(content_type=ct).values_list("codename", "name"))
        self.assertEqual(perms["apply_import"], "Can apply and undo CRM imports")
        self.assertIn("view_import_pii", perms)
        user = User.objects.create_user("reviewer")
        self.assertFalse(user.has_perm("crm_import.apply_import"))
        user.user_permissions.add(Permission.objects.get(content_type=ct, codename="apply_import"))
        user = User.objects.get(pk=user.pk)
        self.assertTrue(user.has_perm("crm_import.apply_import"))
        self.assertFalse(user.has_perm("crm_import.view_import_pii"))


class SourceLockTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other = Company.objects.create(name="Tenant B")

    def test_begin_staging_moves_a_validating_job(self):
        j = begin_staging(job(self.company))
        j.refresh_from_db()
        self.assertEqual(j.status, ImportJob.Status.STAGING)
        self.assertEqual(ImportSourceLock.objects.get().source, SOURCE)

    def test_a_second_staging_of_the_same_source_refuses(self):
        for busy_state in (ImportJob.Status.STAGING, ImportJob.Status.APPLYING):
            with self.subTest(busy_state):
                busy = job(self.company, status=busy_state)
                second = job(self.company)
                with self.assertRaises(SourceBusy) as ctx:
                    begin_staging(second)
                self.assertEqual(ctx.exception.busy, busy)
                second.refresh_from_db()
                self.assertEqual(second.status, ImportJob.Status.VALIDATING)
                busy.delete()

    def test_other_sources_and_tenants_and_finished_jobs_do_not_block(self):
        job(self.company, status=ImportJob.Status.STAGING)
        job(self.company, source="hubspot:999", status=ImportJob.Status.REVIEWING)
        begin_staging(job(self.company, source="hubspot:999"))
        begin_staging(job(self.other))
        self.assertEqual(ImportSourceLock.objects.count(), 2)

    def test_a_crashed_staging_resumes(self):
        j = job(self.company, status=ImportJob.Status.STAGING)
        self.assertEqual(begin_staging(j).status, ImportJob.Status.STAGING)

    def test_only_validating_or_staging_jobs_start(self):
        j = job(self.company, status=ImportJob.Status.APPLIED)
        with self.assertRaisesMessage(ValueError, "applied"):
            begin_staging(j)

class SourceLockTransactionTests(TransactionTestCase):
    def test_lock_needs_a_transaction(self):
        company = Company.objects.create(name="Tenant A")
        with self.assertRaises(TransactionManagementError):
            lock_source(company, SOURCE)


@unittest.skipUnless(connection.vendor == "postgresql", "SELECT ... FOR UPDATE is Postgres-only")
class SourceLockConcurrencyTests(TransactionTestCase):
    """Two connections racing for the same source (spec 7)."""

    def setUp(self):
        self.company = Company.objects.create(name="Tenant A")

    def _hold(self, holder_job, held, release):
        try:
            with transaction.atomic():
                lock_source(holder_job.company, holder_job.source)
                ImportJob.objects.filter(pk=holder_job.pk).update(status=ImportJob.Status.STAGING)
                held.set()
                release.wait(10)
        finally:
            connection.close()

    def _run(self, fn, result):
        try:
            result["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - reported to the test thread
            result["error"] = exc
        finally:
            result["finished_at"] = time.monotonic()
            connection.close()

    def _wait_for_lock_wait(self, timeout=5.0):
        """True once some other backend is blocked on a row lock."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
                )
                if cursor.fetchone()[0]:
                    return True
            time.sleep(0.05)
        return False

    def test_the_second_staging_waits_for_the_lock_then_refuses(self):
        holder_job = job(self.company)
        second = job(self.company)
        held, release = threading.Event(), threading.Event()
        holder = threading.Thread(target=self._hold, args=(holder_job, held, release))
        holder.start()
        result = {}
        try:
            self.assertTrue(held.wait(10))
            waiter = threading.Thread(target=self._run, args=(lambda: begin_staging(second), result))
            waiter.start()
            self.assertTrue(self._wait_for_lock_wait(), "begin_staging did not wait for the lock")
            self.assertTrue(waiter.is_alive())
            released_at = time.monotonic()
        finally:
            release.set()
            holder.join(10)
        waiter.join(10)
        self.assertGreaterEqual(result["finished_at"], released_at)
        self.assertIsInstance(result.get("error"), SourceBusy)
        self.assertEqual(result["error"].busy.pk, holder_job.pk)
        second.refresh_from_db()
        self.assertEqual(second.status, ImportJob.Status.VALIDATING)

    def test_another_source_is_not_blocked(self):
        holder_job = job(self.company)
        other = job(self.company, source="hubspot:999")
        held, release = threading.Event(), threading.Event()
        holder = threading.Thread(target=self._hold, args=(holder_job, held, release))
        holder.start()
        try:
            self.assertTrue(held.wait(10))
            result = {}
            waiter = threading.Thread(target=self._run, args=(lambda: begin_staging(other), result))
            waiter.start()
            waiter.join(5)
            self.assertFalse(waiter.is_alive(), "another source waited for this source's lock")
            self.assertNotIn("error", result)
        finally:
            release.set()
            holder.join(10)
