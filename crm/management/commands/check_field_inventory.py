"""Validate field inventory CSVs (docs/crm/phase4-fields.md 1.2).

    manage.py check_field_inventory [PATH ...]

PATH is a CSV file or a directory of them (``*.csv``). With none, golden's
own standard files in ``docs/crm/fields/`` are checked. Every refusal is
printed as ``<file>:<line>: [<code>] <message>``; the command exits 1 when
there is any.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from crm.fields.inventory import DECISIONS, validate_file


def default_dir() -> Path:
    return Path(settings.BASE_DIR) / "docs" / "crm" / "fields"


class Command(BaseCommand):
    help = "Validate provider field inventory CSVs (phase4-fields.md 1.2)."

    def add_arguments(self, parser):
        parser.add_argument("paths", nargs="*", help="CSV files or directories of them")
        parser.add_argument("--no-models", action="store_true",
                            help="skip checking map/promote targets against golden's models")

    def handle(self, *args, paths, no_models, **options):
        files = []
        for raw in paths or [str(default_dir())]:
            path = Path(raw)
            if path.is_dir():
                files.extend(sorted(path.glob("*.csv")))
            elif path.is_file():
                files.append(path)
            else:
                raise CommandError(f"{raw}: no such file or directory")
        if not files:
            raise CommandError("no inventory CSVs found")

        failed = 0
        for path in files:
            result = validate_file(path, check_models=not no_models)
            for error in result.errors:
                self.stderr.write(f"{path}:{error.line}: [{error.code}] {error.message}"
                                  + (f" ({error.column})" if error.column else ""))
            decisions = Counter(r["decision"] for r in result.rows)
            summary = ", ".join(f"{d} {decisions[d]}" for d in DECISIONS if decisions[d])
            status = "ok" if result.ok else f"{len(result.errors)} error(s)"
            self.stdout.write(f"{path}: {len(result.rows)} row(s){': ' + summary if summary else ''}"
                              f" — {status}")
            failed += not result.ok
        if failed:
            raise CommandError(f"{failed} of {len(files)} inventory file(s) failed validation")
