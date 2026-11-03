"""Report erasures that have not reached a provider yet (docs/crm/phase6-adapters.md 4, step 8).

    python manage.py check_erasure [--company <uuid>] [--max-age-hours 24] [--json]

Lists every ``<provider>.erase`` outbox row not finished: the provider, the
erased party's id, how many provider records are left, attempts and the last
error code (never a provider id or a payload). Exits 1 when a row is dead, has
no registered consumer (its adapter was removed), or has waited longer than
``--max-age-hours``; 0 otherwise, including while rows are still retrying
within the window. Run it from the scheduler beside ``check_outbox``.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError

from core.integrations.erasure import DEFAULT_MAX_AGE, pending


class Command(BaseCommand):
    help = "Report erasures still pending at a provider; exit 1 on a dead, orphaned or late one."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="Only this company (UUID).")
        parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE.total_seconds() / 3600,
                            help="A pending row older than this is late (default 24).")
        parser.add_argument("--json", action="store_true", help="Print the report as JSON.")

    def handle(self, *args, **options):
        if options["company"]:
            try:
                uuid.UUID(options["company"])
            except ValueError:
                raise CommandError(f"--company must be a company id (UUID), not {options['company']!r}.") from None
        report = pending(company_id=options["company"], max_age=timedelta(hours=options["max_age_hours"]))
        failing = report["dead"] or report["orphaned"] or report["late"]
        if options["json"]:
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
        else:
            for row in report["rows"]:
                flags = [name for name in ("late", "orphaned", "collector_failed") if row[name]]
                self.stdout.write(
                    f"{row['consumer']} {row['state']} party={row['party_id']} refs_left={row['refs_left']} "
                    f"attempts={row['attempts']} error={row['last_error'] or '-'} since={row['created_at']}"
                    + (f" [{', '.join(flags)}]" if flags else "")
                )
            summary = (f"{len(report['rows'])} provider erasure(s) pending: {report['dead']} dead, "
                       f"{report['orphaned']} orphaned, {report['late']} late.")
            self.stdout.write(self.style.ERROR(summary) if failing else self.style.SUCCESS(summary))
        if failing:
            raise SystemExit(1)
