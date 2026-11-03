"""Fill a field inventory's counts from local export files
(docs/crm/phase4-fields.md 1.2).

    manage.py field_inventory --provider hubspot --object contact \\
        --definitions properties.json --records contacts.csv \\
        [--inventory crm-fields/hubspot.csv] [--output crm-fields/hubspot.csv]

Refreshes ``populated``, ``total``, ``source_label``, ``source_type`` and
(for enumerations) ``options`` on the inventory's rows for that provider and
object, and appends a row for each defined property the inventory lacks.
The usage columns, ``pii``, ``decision``, ``target`` and ``notes`` are a
person's work and are never changed; new rows get usage ``unknown`` and no
decision, so ``check_field_inventory`` flags them until someone decides.

It never calls a provider API, and writes no record values except
enumeration options. Without ``--output`` the CSV goes to stdout.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from crm.fields import counts
from crm.fields.inventory import COLUMNS, NAMESPACE_RE, OBJECT_RE


class Command(BaseCommand):
    help = "Fill a field inventory's populated/total/options from local exports."

    def add_arguments(self, parser):
        parser.add_argument("--provider", required=True)
        parser.add_argument("--object", required=True, dest="object_type",
                            help="the provider object (contact, deal, ticket, …)")
        parser.add_argument("--definitions", required=True,
                            help="property definitions: JSON, {results: [...]} or JSONL")
        parser.add_argument("--records", required=True, help="records export CSV")
        parser.add_argument("--inventory", help="an existing inventory CSV to update")
        parser.add_argument("--output", help="where to write the CSV (default: stdout)")

    def handle(self, *args, provider, object_type, definitions, records, inventory, output,
               **options):
        if not NAMESPACE_RE.match(provider) or provider == "custom":
            raise CommandError(f"{provider!r} is not a provider namespace")
        if not OBJECT_RE.match(object_type):
            raise CommandError(f"{object_type!r} is not an object name")
        for label, path in (("--definitions", definitions), ("--records", records),
                            ("--inventory", inventory)):
            if path and not Path(path).is_file():
                raise CommandError(f"{label} {path}: no such file")

        try:
            defs = counts.load_definitions(definitions, object_type=object_type)
        except ValueError as exc:  # includes JSONDecodeError
            raise CommandError(f"--definitions {definitions}: {exc}") from exc
        if not defs:
            raise CommandError(f"--definitions {definitions}: no definitions for {object_type!r}")
        total, stats = counts.count_records(records, defs)

        rows = []
        if inventory:
            with open(inventory, newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                missing = [c for c in COLUMNS if c not in (reader.fieldnames or ())]
                if missing:
                    raise CommandError(f"--inventory {inventory}: missing column(s) "
                                       f"{', '.join(missing)}")
                rows = [{k: (v or "") for k, v in r.items() if k is not None} for r in reader]

        filled = counts.fill(rows, provider=provider, object_type=object_type,
                             definitions=defs, total=total, stats=stats)
        buffer = io.StringIO()
        counts.write(filled, buffer)
        if output:
            Path(output).write_text(buffer.getvalue(), encoding="utf-8")
        else:
            self.stdout.write(buffer.getvalue(), ending="")

        unmatched = sorted(n for n, s in stats.items() if s["populated"] is None)
        added = len(filled) - len(rows)
        self.stderr.write(
            f"{provider}/{object_type}: {total} record(s), {len(defs)} definition(s), "
            f"{added} new row(s)"
            + (f"; not in the export: {', '.join(unmatched)}" if unmatched else ""),
        )
