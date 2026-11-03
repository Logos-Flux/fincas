"""``seq`` numbering under parallel appliers (step 2's note for step 4): two
processes applying rows of one job at once never draw the same number, and
each row's entries stay contiguous. Real row locks need Postgres (SQLite
serialises every writer), so these run in the "Test Suite (Postgres 17)" CI job."""

from __future__ import annotations

import threading
import unittest
from itertools import groupby

from django.db import connection
from django.test import TransactionTestCase

from core.models import Company
from crm_import import stage as staging
from crm_import import writelog
from crm_import.models import ImportWriteLog
from unify.apply import apply_chunk, start_apply_run
from unify.models import StagedRow

from .staging_helpers import ExportDir, company, contact


@unittest.skipUnless(connection.vendor == "postgresql", "row locks are Postgres-only")
class ParallelSeqTests(TransactionTestCase):
    def setUp(self):
        self.co = Company.objects.create(name="Tenant A")
        export = ExportDir(self, {
            "contact": [contact(i, given=f"P{i}", emails=[f"p{i}@example.com"], phones=["+1 202 555 0143"])
                        for i in range(1, 13)],
            "company": [company(100 + i, name=f"Org {i}") for i in range(1, 13)],
        })
        self.job = staging.stage(self.co, export.path)
        self.assertEqual(self.job.status, "reviewing", self.job.report)

    def _drive(self, run_id, row_ids, errors):
        try:
            apply_chunk(run_id, row_ids)
        except Exception as exc:  # noqa: BLE001 — reported to the main thread
            errors.append(exc)
        finally:
            connection.close()

    def _runs(self, *object_types):
        return {t: start_apply_run(self.job.job_batches.get(object_type=t).batch, actor="test") for t in object_types}

    def _assert_numbering(self):
        logs = list(ImportWriteLog.objects.filter(job=self.job).order_by("seq").values_list("seq", "approval_row_id"))
        self.assertEqual([s for s, _ in logs], list(range(1, len(logs) + 1)))
        rows = [row for row, _ in groupby(r for _, r in logs)]
        self.assertEqual(len(rows), len(set(rows)), "one row's entries are interleaved with another's")

    def test_two_workers_on_one_run(self):
        run = self._runs("contact")["contact"]
        ids = [str(pk) for pk in run.rows.values_list("staged_row_id", flat=True)]
        errors = []
        threads = [threading.Thread(target=self._drive, args=(str(run.pk), ids[i::2], errors)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        states = set(StagedRow.objects.filter(pk__in=ids).values_list("mapping_state", flat=True))
        self.assertEqual(states, {StagedRow.MappingState.APPLIED})
        self._assert_numbering()

    def test_two_batches_in_parallel(self):
        runs = self._runs("company", "contact")
        errors = []
        threads = [threading.Thread(target=self._drive, args=(str(r.pk), [], errors)) for r in runs.values()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        for run in runs.values():
            states = set(run.batch.rows.values_list("mapping_state", flat=True))
            self.assertEqual(states, {StagedRow.MappingState.APPLIED})
        self._assert_numbering()
        report = writelog.undo_job(self.job, actor="test")
        self.assertEqual(report.refused, [])
