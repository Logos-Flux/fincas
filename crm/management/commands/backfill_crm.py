"""``manage.py backfill_crm --step N [--dry-run] [--accept-unmapped]``.

The CRM Phase 3 data steps (docs/crm/phase3-engagement.md, section 8), the
same functions the data migrations run. Each is idempotent, so running one
after ``migrate`` already did is a no-op that prints the summary.

- ``--step 5``: quotes onto engagements (``crm.backfill.backfill_quote_engagements``).
  ``--dry-run`` writes nothing and prints the distinct raw ``sales_stage``
  values with counts; unmapped values stop a real run unless
  ``--accept-unmapped``. ``--reverse`` deletes what the step created (``--force`` once people
  have added history, tasks or contacts to those threads).
- ``--step 6``: leads, ActivityLog and EmailLog onto the thread, in that
  order (``backfill_lead_engagements``, ``backfill_activity_communications``,
  ``communications.backfill.backfill_email_communications``). ``--dry-run``
  writes nothing; ``--reverse`` undoes the three in the opposite order
  (``--force``: also when people worked on the lead engagements).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from communications.backfill import backfill_email_communications, reverse_email_communications
from crm.backfill import (
    ReverseRefused,
    UnmappedStagesError,
    backfill_activity_communications,
    backfill_lead_engagements,
    backfill_quote_engagements,
    reverse_activity_communications,
    reverse_lead_engagements,
    reverse_quote_engagements,
)

STEPS = (5, 6)


class Command(BaseCommand):
    help = "Run a CRM Phase 3 data step (see docs/crm/phase3-engagement.md section 8)."

    def add_arguments(self, parser):
        parser.add_argument("--step", type=int, required=True, choices=STEPS)
        parser.add_argument("--dry-run", action="store_true", help="Write nothing; print the plan")
        parser.add_argument("--accept-unmapped", action="store_true",
                            help="Proceed when some stage strings map to no stage")
        parser.add_argument("--reverse", action="store_true",
                            help="Undo the step (delete the engagements it created)")
        parser.add_argument("--force", action="store_true",
                            help="With --reverse: also discard work added to those engagements")

    def handle(self, *args, **opts):
        if opts["step"] == 6:
            return self._step6(opts)
        if opts["reverse"]:
            if opts["dry_run"]:
                raise CommandError("--reverse has no --dry-run.")
            try:
                reverse_quote_engagements(log=self.stdout.write, force=opts["force"])
            except ReverseRefused as exc:
                raise CommandError(str(exc)) from exc
            return
        try:
            backfill_quote_engagements(
                dry_run=opts["dry_run"], accept_unmapped=opts["accept_unmapped"],
                log=self.stdout.write,
            )
        except UnmappedStagesError as exc:
            raise CommandError(str(exc)) from exc

    def _step6(self, opts):
        if opts["accept_unmapped"]:
            raise CommandError("--accept-unmapped is step 5's.")
        out = self.stdout.write
        if opts["reverse"]:
            if opts["dry_run"]:
                raise CommandError("--reverse has no --dry-run.")
            reverse_email_communications(log=out)
            reverse_activity_communications(log=out)
            try:
                reverse_lead_engagements(log=out, force=opts["force"])
            except ReverseRefused as exc:
                raise CommandError(str(exc)) from exc
            return
        dry = opts["dry_run"]
        out("leads:")
        backfill_lead_engagements(dry_run=dry, log=lambda line: out(f"  {line}"))
        out("activities:")
        backfill_activity_communications(dry_run=dry, log=lambda line: out(f"  {line}"))
        out("emails:")
        backfill_email_communications(dry_run=dry, log=lambda line: out(f"  {line}"))
