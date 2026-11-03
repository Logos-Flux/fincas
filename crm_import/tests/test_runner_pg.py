"""The runner on Postgres (step 8): two runners cannot run one job, and the dry
run is a real transaction rollback (``TransactionTestCase``: nothing wraps the
test in a transaction of its own, so a commit would be a commit). The advisory
lock is Postgres-only, so these run in the ``tests-postgres`` CI job."""

from __future__ import annotations

import threading
import time
import unittest

from django.db import connection
from django.test import TransactionTestCase

from core.models import Company
from crm_import import runner
from crm_import import stage as staging
from crm_import.models import ImportJob
from parties.models import Party
from unify.models import StagedRow

from .test_appliers import ACTOR
from .test_runner import Operator, count_every_table, review


def advisory_locks_held() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()")
        return cursor.fetchone()[0]


@unittest.skipUnless(connection.vendor == "postgresql", "session advisory locks are Postgres-only")
class RunnerPostgresTests(TransactionTestCase):
    def setUp(self):
        self.co = Company.objects.create(name="Tenant A")
        Party.objects.create(company=self.co, kind="organisation", display_name="Tenant A", is_self=True,
                             origin="system")
        self.op = Operator(self, self.co)
        self.job = self.op.adapt_check_stage()
        review(self.job)

    def _hold(self, held, release, holder_pid):
        """Another runner mid-run: holds the job's advisory lock on its own connection."""
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
        loaded box the next ``run_job`` could otherwise still find the lock
        held. Wait until Postgres shows no advisory lock for that backend."""
        deadline = time.monotonic() + timeout
        while True:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = %s", [pid])
                if cursor.fetchone()[0] == 0:
                    return
            if time.monotonic() > deadline:
                self.fail(f"backend {pid} still holds its advisory lock after {timeout}s")
            time.sleep(0.05)

    def test_two_runners_cannot_run_one_job(self):
        held, release, holder_pid = threading.Event(), threading.Event(), []
        holder = threading.Thread(target=self._hold, args=(held, release, holder_pid))
        holder.start()
        try:
            # The holder owns the lock before any attempt; it lets go only after
            # every refusal is asserted (``release`` in ``finally``).
            self.assertTrue(held.wait(60), "the holder thread never took the lock")
            for attempt in (lambda: runner.run_job(self.job, actor=ACTOR),
                            lambda: runner.run_job(self.job, actor=ACTOR, dry_run=True),
                            lambda: runner.undo_job(self.job, actor=ACTOR),
                            lambda: runner.cancel_job(self.job)):
                with self.assertRaisesMessage(runner.RunnerError, "by another process"):
                    attempt()
            self.job.refresh_from_db()
            self.assertEqual(self.job.status, ImportJob.Status.REVIEWING)
            self.assertFalse(StagedRow.objects.filter(mapping_state=StagedRow.MappingState.APPLIED).exists())
        finally:
            release.set()
            holder.join(60)
        self.assertFalse(holder.is_alive())
        self._wait_for_session_end(holder_pid[0])
        # The other runner's session ended (a crash looks the same): the run proceeds.
        self.assertEqual(runner.run_job(self.job, actor=ACTOR).job.status, ImportJob.Status.APPLIED)
        self.assertEqual(advisory_locks_held(), 0)

    def test_dry_run_rolls_back_for_real(self):
        counts = count_every_table()
        result = runner.run_job(self.job, actor=ACTOR, dry_run=True)
        self.assertGreater(result.report["totals"]["applied"], 0)
        self.assertEqual(count_every_table(), counts)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ImportJob.Status.REVIEWING)
        self.assertEqual(advisory_locks_held(), 0)  # the dry run's lock went with its transaction
        # And the real run afterwards applies what the dry run said it would.
        real = runner.run_job(self.job, actor=ACTOR)
        self.assertEqual(real.report["types"], result.report["types"])
