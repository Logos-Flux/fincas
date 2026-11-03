"""``manage.py backfill_parties --step <n> [--reverse] [--dry-run]`` (phase2-party.md section 4).

Runs one data step (D2-D10) of the party backfill, the same function the
data migration ``parties.0002_backfill`` calls, on the same historical
models. A large deployment can ``migrate --fake parties 0002`` and run the
steps here in its cutover window. Every step is idempotent, so a re-run
continues where a failed one stopped. Writes use ``bulk_create`` /
``update`` only: no signals are sent.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.migrations.loader import MigrationLoader

from parties.backfill import STEPS, BackfillRefused, format_line, run_step


class Command(BaseCommand):
    help = "Run a party backfill data step (D2-D10); idempotent, emits no signals."

    def add_arguments(self, parser):
        parser.add_argument(
            "--step", action="append", required=True,
            help=f"Step number ({min(STEPS)}-{max(STEPS)}) or 'all'; repeatable",
        )
        parser.add_argument("--reverse", action="store_true", help="Undo the step(s), last first")
        parser.add_argument("--dry-run", action="store_true", help="Run, print the summary, roll back")

    @staticmethod
    def _apps():
        """The migration state's models (what ``parties.0002`` sees); the live
        registry only when migrations are disabled (the test settings)."""
        from django.apps import apps as live

        state = MigrationLoader(connection, ignore_no_migrations=True).project_state().apps
        try:
            for label in ("core.Company", "customers.Customer", "parties.Party", "crm.Lead"):
                state.get_model(label)
        except LookupError:
            return live
        return state

    def handle(self, *args, **opts):
        steps = []
        for value in opts["step"]:
            if value == "all":
                steps += list(STEPS)
                continue
            try:
                n = int(value.upper().lstrip("D"))
            except ValueError:
                raise CommandError(f"--step {value!r}: expected a number or 'all'") from None
            if n not in STEPS:
                raise CommandError(f"--step {n}: steps are {min(STEPS)}-{max(STEPS)} (D1 is the schema)")
            steps.append(n)
        steps = sorted(set(steps), reverse=opts["reverse"])
        apps = self._apps()
        for n in steps:
            try:
                with transaction.atomic():
                    report = run_step(apps, n, reverse=opts["reverse"])
                    if opts["dry_run"]:
                        transaction.set_rollback(True)
            except BackfillRefused as exc:
                raise CommandError(f"D{n} refused: {exc}") from None
            prefix = "[dry run] " if opts["dry_run"] else ""
            self.stdout.write(prefix + format_line(n, report, reverse=opts["reverse"]))
