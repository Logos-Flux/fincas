"""Validate a canonical CRM export directory before anything is staged.

    manage.py check_canonical_export <dir> [--json] [--max-errors N]

Prints the manifest's coverage table and every issue with file, line and
field, then exits non-zero if there is any error. Warnings (references into
an object type whose coverage is not complete) do not fail the check. Reads
files only: no database access. See docs/crm/importing.md.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from crm_import.canonical import OBJECT_FILES, validate_canonical


class Command(BaseCommand):
    help = "Validate a canonical CRM export directory (docs/crm/importing.md)."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("directory", help="the canonical export directory (holds manifest.json)")
        parser.add_argument("--json", action="store_true", help="print the full result as JSON")
        parser.add_argument("--max-errors", type=int, default=1000,
                            help="stop recording errors after this many (the rest are counted)")

    def handle(self, *args, **options):
        directory = Path(options["directory"])
        if not directory.is_dir():
            raise CommandError(f"{directory} is not a directory.")
        result = validate_canonical(directory, max_errors=options["max_errors"])
        if options["json"]:
            self.stdout.write(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            self._print(result)
        if not result.ok:
            raise CommandError(
                f"{len(result.errors) + result.suppressed} error(s) in {directory}. "
                "Fix the adapter or the export, then re-run. See docs/crm/importing.md."
            )

    def _print(self, result):
        self.stdout.write(f"Canonical export {result.directory}")
        self.stdout.write(f"  source {result.source or '?'}, schema_version {result.schema_version or '?'}")
        self.stdout.write(f"  {'object type':<14} {'file':<22} {'rows':>7}  coverage")
        for otype, fname in OBJECT_FILES.items():
            rows = result.counts.get(otype)
            self.stdout.write(
                f"  {otype:<14} {fname:<22} {'-' if rows is None else rows:>7}  "
                f"{result.coverage.get(otype, '?')}"
            )
        for issue in result.errors:
            self.stdout.write(self.style.ERROR(f"ERROR   {issue}"))
        if result.suppressed:
            self.stdout.write(self.style.ERROR(f"ERROR   … and {result.suppressed} more (raise --max-errors)"))
        for issue in result.warnings:
            self.stdout.write(self.style.WARNING(f"WARNING {issue}"))
        if result.ok:
            self.stdout.write(self.style.SUCCESS(
                f"OK: {sum(result.counts.values())} records, {len(result.warnings)} warning(s)."
            ))
