"""``manage.py check_numbering``: document numbers per company and type
(docs/release-hardening-plan.md 7.3).

For quotes, orders, invoices, purchase orders, engagements (EN), leads (LD)
and payments: every number is unique within its company (duplicates exit
20), numbers that carry a sequence (``PREFIX-<digits>``) rise with
``created_at`` (a later row with a lower number is an order violation, exit
10), and gaps are reported as a statistic, never a failure. Quotes, orders,
invoices, payments and engagements share one ``DocumentSequence`` per
company, so gaps inside one type are expected.

Read-only. Exit codes: ``core/management/release_exit_codes.py``.
"""

from __future__ import annotations

import json
import sys

from django.core.management.base import BaseCommand

from core import integrity
from core.management import release_exit_codes as codes


class Command(BaseCommand):
    help = (
        "Check document numbers per company: unique (else exit 20), monotonic by created_at "
        "(else exit 10); gaps are a statistic."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--samples", type=int, default=5, metavar="N",
            help="Numbers kept per finding (default 5).",
        )
        parser.add_argument("--json", action="store_true", help="Print the result as JSON.")

    def handle(self, *args, **opts):
        result = integrity.numbering_report(samples=opts["samples"])
        if opts["json"]:
            self.stdout.write(json.dumps(result.as_dict(), indent=2, default=str))
        else:
            self.render(result)
        self.stdout.flush()
        if result.exit_code:
            sys.exit(result.exit_code)

    def render(self, result: integrity.NumberingResult) -> None:
        out = self.stdout.write
        if not result.rows:
            out("no numbered documents")
        else:
            out(integrity.render_table(
                ["company", "doc_type", "rows", "blank", "duplicates", "non_sequence",
                 "order_violations", "lowest", "highest", "gaps_missing", "gaps_runs",
                 "gaps_largest", "status"],
                [[r.company, r.doc_type, r.rows, r.blank, r.duplicates, r.non_sequence,
                  r.order_violations, r.lowest if r.lowest is not None else "-",
                  r.highest if r.highest is not None else "-", r.gaps_missing, r.gaps_runs,
                  r.gaps_largest, r.status]
                 for r in result.rows],
            ))
            for r in result.rows:
                for label, samples in (
                    ("duplicate", r.duplicate_samples),
                    ("non-sequence", r.non_sequence_samples),
                    ("out of order", r.order_violation_samples),
                ):
                    if samples:
                        out(f"{r.doc_type} ({r.company}) {label}: {', '.join(samples)}")
        out("")
        out(f"STATUS: {result.status} (exit {result.exit_code}): {self.guidance(result)}")

    @staticmethod
    def guidance(result: integrity.NumberingResult) -> str:
        if result.status == codes.STATUS_OK:
            return "every number is unique in its company and sequences rise with created_at."
        if result.status == codes.STATUS_ROLL_BACK:
            return "a document number is held by more than one row in a company (S1)."
        return (
            "numbers are unique, but some rows are blank or carry a sequence lower than an "
            "earlier row's; gaps are informational."
        )
