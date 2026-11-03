"""``manage.py check_money``: read-only comparator for stored totals
(docs/release-hardening-plan.md 7.2).

Recomputes every quote, order, sales invoice and purchase order with the
model's own ``recalculate_totals()`` inside one transaction that is always
rolled back, buckets each difference by a named cause, asserts the trial
balance is zero per company and that every invoice's balance agrees with its
payments. Run it on a clone of the database you are about to cut over.

Exit codes (``core/management/release_exit_codes.py``): 20 when a posted
invoice's recomputed amount differs by more than one minor unit and no
historical cause explains it, or the ledger is unbalanced, or a posted
invoice's balance does not add up; 10 when every difference has a named
cause (listed for the release notes); 0 when the stored totals are exactly
what the code computes today.
"""

from __future__ import annotations

import json
import sys

from django.core.management.base import BaseCommand

from core import integrity
from core.management import release_exit_codes as codes


class Command(BaseCommand):
    help = (
        "Recompute every document's totals without saving and bucket the differences; "
        "exit 0 ok, 10 explained differences, 20 roll back."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=None, metavar="N",
            help="Examine at most N documents per type (newest first).",
        )
        parser.add_argument(
            "--samples", type=int, default=5, metavar="N",
            help="Document numbers kept per bucket (default 5).",
        )
        parser.add_argument("--json", action="store_true", help="Print the result as JSON.")

    def handle(self, *args, **opts):
        result = integrity.compare_totals(limit=opts["limit"], samples=opts["samples"])
        if opts["json"]:
            self.stdout.write(json.dumps(result.as_dict(), indent=2))
        else:
            self.render(result)
        self.stdout.flush()
        if result.exit_code:
            sys.exit(result.exit_code)

    def render(self, result: integrity.MoneyResult) -> None:
        out = self.stdout.write
        examined = ", ".join(f"{k}={v}" for k, v in result.examined.items())
        out(f"Documents recomputed (rolled back): {examined}")
        out("")
        out("Recomputed totals vs stored")
        rows = [
            [b.doc_type, b.cause, "yes" if b.explained else "NO", "yes" if b.historical else "no",
             b.count, b.posted_over_unit, b.max_abs_diff, b.status,
             " ".join(f"{f}:{n}" for f, n in sorted(b.fields.items())), ", ".join(b.samples)]
            for b in sorted(result.buckets.values(), key=lambda b: (b.doc_type, b.cause))
        ]
        if rows:
            out(integrity.render_table(
                ["doc_type", "cause", "explained", "historical", "count", "posted>1 unit",
                 "max_diff", "status", "fields", "samples"], rows,
            ))
            out("(a document is counted under its header cause and, when balance_due moved "
                "on its own, under its balance cause)")
        else:
            out("  every stored total equals the recomputed one")
        out("")
        out("Trial balance (posted journal entries)")
        out(integrity.render_table(
            ["company", "entries", "debits", "credits", "difference", "unbalanced", "status",
             "samples"],
            [[r["company"], r["posted_entries"], r["debits"], r["credits"], r["difference"],
              r["unbalanced_entries"], r["status"], ", ".join(r["unbalanced_samples"])]
             for r in result.trial_balance],
        ) if result.trial_balance else "  no companies")
        out("")
        out("Invoice balances vs payments")
        if result.invoice_balances:
            out(integrity.render_table(
                ["check", "count", "posted>1 unit", "max_diff", "status", "samples"],
                [[b.cause, b.count, b.posted_over_unit, b.max_abs_diff, b.status,
                  ", ".join(b.samples)]
                 for _, b in sorted(result.invoice_balances.items())],
            ))
        else:
            out("  every invoice's balance_due is grand_total - amount_paid, and amount_paid "
                "is its settled payments")
        out("")
        for cause in sorted({b.cause for b in result.buckets.values()}
                            | set(result.invoice_balances)):
            out(f"{cause}: {integrity.CAUSES[cause].description}")
        out("")
        out(f"STATUS: {result.status} (exit {result.exit_code}): {self.guidance(result)}")

    @staticmethod
    def guidance(result: integrity.MoneyResult) -> str:
        if result.status == codes.STATUS_OK:
            return "stored totals, the ledger and invoice balances all agree with the code."
        if result.status == codes.STATUS_ROLL_BACK:
            return (
                "a posted invoice or the ledger is wrong by more than one minor unit with no "
                "historical cause (S1): do not cut over on this data; see the buckets marked "
                "ROLL BACK."
            )
        return (
            "every difference has a named cause (S2): list the buckets in the release notes "
            "and decide whether a backfill repairs them."
        )
