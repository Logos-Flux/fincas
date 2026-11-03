"""Import a QuickBooks Online export into the Django general ledger.

    python manage.py import_qbo_export --company "Example Supply, Inc." \
        --chart-of-accounts exports/accounts.xlsx \
        --transactions exports/journal.xlsx \
        --dry-run

Company is required and never guessed: ``Account``/``JournalEntry`` are
company-scoped, and picking the wrong one posts a whole ledger to the wrong
books. ``--dry-run`` runs the real import inside a transaction and rolls it
back, so the report is exactly what a live run would produce.
"""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Company

from ...qbo.importer import (
    ChartOfAccountsImporter,
    ImportReport,
    import_chart_of_accounts,
    import_transactions,
)
from ...qbo.readers import QBOFormatError


class _Rollback(Exception):
    """Internal signal used to unwind a --dry-run transaction."""


class Command(BaseCommand):
    help = "Import a QuickBooks Online export (chart of accounts and/or transactions)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company",
            required=True,
            help="Company name or UUID to import into. Required — accounts and "
            "entries are company-scoped and the wrong one corrupts the books.",
        )
        parser.add_argument(
            "--chart-of-accounts",
            dest="coa_path",
            help="Path to the QBO account list export (.csv or .xlsx).",
        )
        parser.add_argument(
            "--transactions",
            dest="txn_path",
            help="Path to a Journal or Transaction Detail export (.csv or .xlsx).",
        )
        parser.add_argument(
            "--dayfirst",
            action="store_true",
            help="Read ambiguous slash dates as DD/MM/YYYY. QBO exports in US "
            "format by default; nothing in the file says which it is.",
        )
        parser.add_argument(
            "--require-status",
            dest="require_status",
            action="append",
            metavar="VALUE",
            help="Only import entries whose every leg carries this value in the "
            "export's Status column (case- and punctuation-insensitive, exact "
            "match — 'POST' does NOT match 'PROPOSED - NOT POSTED'). Repeat the "
            "flag to allow several values. An entry whose legs disagree is "
            "reported as an error and not imported; approval is per entry, not "
            "per line. If the file has no status column the run is refused "
            "outright rather than importing everything.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Run the import and roll it back, printing what would change.",
        )

    def handle(self, *args, **options):
        coa_path = options.get("coa_path")
        txn_path = options.get("txn_path")
        if not coa_path and not txn_path:
            raise CommandError(
                "Nothing to do — pass --chart-of-accounts and/or --transactions."
            )

        company = self._resolve_company(options["company"])
        dry_run = options["dry_run"]
        report = ImportReport()

        self.stdout.write(
            f"Importing into company: {company.name} ({company.pk})"
            + ("  [DRY RUN]" if dry_run else "")
        )
        # Echo the gate. A ledger import's log has to record what was allowed
        # in, not just what landed — "9 entries" means something different
        # under a status filter than without one.
        if options.get("require_status"):
            self.stdout.write(
                "Status filter:     only entries whose every leg is "
                + " / ".join(repr(s) for s in options["require_status"])
            )

        try:
            with transaction.atomic():
                accounts = None
                if coa_path:
                    self.stdout.write(f"Chart of accounts: {coa_path}")
                    accounts, report = import_chart_of_accounts(
                        company, coa_path, report
                    )
                if txn_path:
                    self.stdout.write(f"Transactions:      {txn_path}")
                    if accounts is None:
                        accounts = ChartOfAccountsImporter(company, report)
                        accounts.load_existing()
                    import_transactions(
                        company,
                        txn_path,
                        accounts=accounts,
                        report=report,
                        dayfirst=options["dayfirst"],
                        require_status=options.get("require_status"),
                    )
                if dry_run:
                    raise _Rollback
        except _Rollback:
            pass
        except QBOFormatError as exc:
            raise CommandError(
                f"{exc} Nothing was written — the run was rolled back."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            # Per-row problems are captured into the report; anything reaching
            # here is fatal (a DB or environment failure) and the atomic block
            # has already rolled the whole run back. Say so plainly — an
            # automated caller seeing a bare traceback cannot tell whether it
            # is safe to re-run.
            raise CommandError(
                f"Import aborted: {type(exc).__name__}: {exc}. "
                f"Nothing was written — the run was rolled back and re-running "
                f"is safe."
            ) from exc

        self.stdout.write("")
        for line in report.as_lines():
            self.stdout.write(line)

        if dry_run:
            self.stdout.write(self.style.WARNING("\nDRY RUN — rolled back, nothing written."))

        # Unbalanced groups and hard errors are skipped rather than written, so
        # the ledger stays consistent — but everything that DID parse has
        # already been committed by this point. A caller reading only the exit
        # code would otherwise conclude nothing landed and re-run, so the
        # message has to say plainly what is in the database.
        if report.errors or report.skipped_unbalanced:
            if dry_run:
                state = "Nothing was written (dry run)."
            else:
                state = (
                    f"PARTIAL IMPORT COMMITTED: "
                    f"{report.accounts_created + report.accounts_updated} account(s) "
                    f"and {report.entries_created + report.entries_updated} entry(ies) "
                    f"are in the database. Fix the source rows listed above and "
                    f"re-run — the import is idempotent, so re-running is safe."
                )
            raise CommandError(
                f"Import finished with {len(report.errors)} error(s) and "
                f"{len(report.skipped_unbalanced)} unbalanced group(s) skipped. "
                f"{state}"
            )

        self.stdout.write(self.style.SUCCESS("\nImport complete."))

    def _resolve_company(self, reference):
        """Look up by UUID first, then by name (case-insensitively)."""
        # A non-UUID pk lookup raises ValidationError on Postgres and
        # ValueError on SQLite, so --company stays usable with a plain name
        # on either backend.
        try:
            company = Company.objects.filter(pk=reference).first()
        except (ValidationError, ValueError, TypeError):
            company = None
        if company:
            return company

        matches = list(Company.objects.filter(name__iexact=reference)[:5])
        if len(matches) == 1:
            return matches[0]
        if not matches:
            known = ", ".join(Company.objects.values_list("name", flat=True)[:20]) or "(none)"
            raise CommandError(
                f"No company matches {reference!r}. Known companies: {known}"
            )
        raise CommandError(
            f"{reference!r} matches {len(matches)} companies — pass the UUID instead."
        )
