"""The staging run lock (phase5-importer.md 7, step 2's "As built": the
stager keeps a second process off the job). A Postgres session advisory lock;
SQLite has no second process to keep off, so these run on Postgres only (the
``tests-postgres`` CI job)."""

from __future__ import annotations

import threading
import time
import unittest

from django.db import connection
from django.test import TransactionTestCase

from core.models import Company
from crm_import import stage as staging
from crm_import.models import ImportJob

from .staging_helpers import ExportDir, contact


@unittest.skipUnless(connection.vendor == "postgresql", "session advisory locks are Postgres-only")
class JobRunLockTests(TransactionTestCase):
    def setUp(self):
        self.co = Company.objects.create(name="Tenant A")
        self.export = ExportDir(self, {"contact": [contact(i) for i in range(1, 4)]})
        self.job = staging.stage(self.co, self.export.path)
        ImportJob.objects.filter(pk=self.job.pk).update(status=ImportJob.Status.STAGING)
        self.job.refresh_from_db()

    def _hold(self, held, release, holder_pid):
        """Another process mid-staging: holds the job's advisory lock on its own connection."""
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s, %s)",
                               [staging.ADVISORY_NAMESPACE, staging._advisory_key(self.job)])
                cursor.execute("SELECT pg_backend_pid()")
                holder_pid.append(cursor.fetchone()[0])
            held.set()
            release.wait(60)
        finally:
            connection.close()  # ends the session: Postgres releases the lock

    def _wait_for_session_end(self, pid, timeout=60.0):
        """Closing a connection only *sends* the terminate message: the old
        backend releases its locks some time later, on its own schedule. On a
        loaded box the resume below could otherwise still find the lock held
        (CI, 2026-10-14; the runner twin in ``test_runner_pg`` had the same
        race, #115). Wait until Postgres shows no advisory lock for that backend."""
        deadline = time.monotonic() + timeout
        while True:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = %s", [pid])
                if cursor.fetchone()[0] == 0:
                    return
            if time.monotonic() > deadline:
                self.fail(f"backend {pid} still holds its advisory lock after {timeout}s")
            time.sleep(0.05)

    def test_a_second_process_cannot_resume_a_job_being_staged(self):
        held, release, holder_pid = threading.Event(), threading.Event(), []
        holder = threading.Thread(target=self._hold, args=(held, release, holder_pid))
        holder.start()
        try:
            self.assertTrue(held.wait(60), "the holder thread never took the lock")
            with self.assertRaises(staging.StagingError):
                staging.stage_job(self.job, self.export.path, resume=True)
        finally:
            release.set()
            holder.join(60)
        self.assertFalse(holder.is_alive())
        self._wait_for_session_end(holder_pid[0])
        # The holder's session ended (a crash looks the same): resume proceeds.
        self.assertEqual(staging.stage_job(self.job, self.export.path, resume=True).status, "reviewing")

    def test_inside_a_transaction_the_lock_goes_with_it(self):
        """The step 8 dry run stages inside a transaction it rolls back."""
        from django.db import transaction

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                staging.stage_job(self.job, self.export.path, resume=True)
                raise RuntimeError("roll back")
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()")
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_the_lock_is_released_after_a_run(self):
        staging.stage_job(self.job, self.export.path, resume=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()")
            self.assertEqual(cursor.fetchone()[0], 0)
