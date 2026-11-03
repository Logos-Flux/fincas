"""``manage.py party_dedupe_propose`` (docs/crm/phase2-party.md D12, step 6).

Runs the dedupe rules over each company's live parties and queues every new
candidate cluster as a ``StagedRow`` of a ``dedupe`` ``ImportBatch``, for
review at ``/erp/parties/merge-review/``. Nothing is merged here.

Clusters already dismissed ("not a duplicate"), already merged, or still in
the queue are skipped. Run ``party_dedupe_report`` first and read it (D11).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from core.models import Company
from parties.merge_queue import scan


class Command(BaseCommand):
    help = "Queue new party duplicate candidates for merge review (no merges)."

    def add_arguments(self, parser):
        parser.add_argument("--company", action="append", default=[],
                            help="Company id (repeatable). Default: every company.")
        parser.add_argument("--dry-run", action="store_true", help="Count, write nothing.")

    def handle(self, *args, **opts):
        companies = Company.objects.order_by("name")
        if opts["company"]:
            try:
                companies = companies.filter(pk__in=opts["company"])
                found = companies.count()
            except (ValueError, TypeError) as exc:
                raise CommandError(f"Bad company id: {exc}") from exc
            if found != len(set(opts["company"])):
                raise CommandError("One or more --company ids do not exist.")
        for company in companies:
            out = scan(company, dry_run=opts["dry_run"])
            batch = out["batch"]
            self.stdout.write(
                f"{company.name}: {out['clusters']} clusters, {out['new']} queued, "
                f"{out['suppressed']} already known, {out['stale']} stale"
                + (f" (batch {batch.pk})" if batch else " (dry run)" if opts["dry_run"] else "")
            )
