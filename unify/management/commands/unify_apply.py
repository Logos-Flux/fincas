"""``manage.py unify_apply --run <id>``: drive an approval run server-side.

Applies the run's pending rows in the same chunks the screen posts
(``CHUNK_SIZE``), so a 10k-row run does not depend on an open browser tab
(phase2-party.md 3.4). Safe to re-run: applied rows are skipped, and a
cancelled run stops before its next chunk.

``--batch <id> --actor <name>`` opens a new run over the batch's approved
rows first, then drives it.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from unify.appliers import get_applier
from unify.apply import CHUNK_SIZE, apply_chunk, start_apply_run
from unify.models import ApprovalRow, ApprovalRun, ImportBatch


class Command(BaseCommand):
    help = "Apply an approval run's pending rows in chunks, server-side."

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--run", help="ApprovalRun id to drive.")
        target.add_argument("--batch", help="ImportBatch id: open a new run over its approved rows.")
        parser.add_argument("--actor", help="Decider recorded on a new run (required with --batch).")
        parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE)
        parser.add_argument("--max-chunks", type=int, default=0, help="Stop after N chunks (0 = all).")

    def handle(self, *args, **opts):
        if opts["batch"]:
            if not opts["actor"]:
                raise CommandError("--batch needs --actor: every run names its decider.")
            try:
                batch = ImportBatch.objects.get(pk=opts["batch"])
            except (ImportBatch.DoesNotExist, ValueError, TypeError) as exc:
                raise CommandError(f"No batch {opts['batch']}.") from exc
            refusal = get_applier(batch.entity_kind).refusal(batch, "apply")
            if refusal:
                raise CommandError(refusal)
            run = start_apply_run(batch, actor=opts["actor"])
            self.stdout.write(f"opened run {run.pk} ({run.rows.count()} rows), undo token {run.undo_token}")
        else:
            try:
                run = ApprovalRun.objects.get(pk=opts["run"])
            except (ApprovalRun.DoesNotExist, ValueError, TypeError) as exc:
                raise CommandError(f"No run {opts['run']}.") from exc
            refusal = get_applier(run.batch.entity_kind).refusal(run.batch, "apply")
            if refusal:
                raise CommandError(refusal)
        if opts["chunk_size"] < 1:
            raise CommandError("--chunk-size must be at least 1.")

        totals = {"applied": 0, "failed": 0, "already-applied": 0}
        chunks = 0
        while True:
            out = apply_chunk(str(run.pk), [], chunk_size=opts["chunk_size"])
            if out["status"] == ApprovalRun.Status.CANCELLED:
                self.stdout.write("run canceled; stopping")
                break
            if not out["results"]:
                break
            chunks += 1
            for result in out["results"]:
                totals[result["state"]] = totals.get(result["state"], 0) + 1
            self.stdout.write(
                f"chunk {chunks}: " + ", ".join(f"{k} {v}" for k, v in sorted(totals.items()))
            )
            if opts["max_chunks"] and chunks >= opts["max_chunks"]:
                break
        pending = run.rows.filter(state=ApprovalRow.State.PENDING).count()
        if not pending and run.status != ApprovalRun.Status.CANCELLED:
            ApprovalRun.objects.filter(pk=run.pk).update(status=ApprovalRun.Status.DONE)
        self.stdout.write(
            f"run {run.pk}: {totals['applied']} applied, {totals['failed']} failed, "
            f"{totals['already-applied']} already applied, {pending} pending"
        )
