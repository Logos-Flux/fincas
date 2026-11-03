"""Compare a Postgres schema against a committed baseline snapshot (Pin M1).

Modes:
    --snapshot PATH                 write the live schema as sorted JSON ("-" = stdout)
    --compare BASELINE              diff the live database against BASELINE
    --compare BASELINE --against B  diff snapshot B against BASELINE, no database needed

"added" always means present in the live database (or in --against) and absent
from the baseline; "removed" the reverse.

Exit codes:
    0  no differences, or only differences listed in --expect-diff
    1  an unexpected difference, or a stale --expect-diff entry (unless --allow-stale)
    2  could not run: not Postgres, unreadable snapshot or manifest, bad arguments

The comparison logic lives in core/schema_parity.py; see docs/pin/README.md.
"""

import json
from pathlib import Path

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections
from django.utils.connection import ConnectionDoesNotExist

from core import schema_parity


def table_app_labels() -> dict:
    """Map every table Django knows about (including auto-created M2M tables) to its app label."""
    mapping = {}
    for model in django_apps.get_models(include_auto_created=True):
        if model._meta.proxy:
            continue
        mapping[model._meta.db_table] = model._meta.app_label
    return mapping


class Command(BaseCommand):
    help = "Snapshot the Postgres schema, or diff it (or another snapshot) against a baseline snapshot."
    # A read-only diagnostic that must also run offline (--against); system
    # checks would only add stderr noise and unrelated failure modes.
    requires_system_checks = []

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--snapshot", metavar="PATH", help="Write the live schema snapshot to PATH ('-' for stdout).")
        mode.add_argument("--compare", metavar="BASELINE", help="Baseline snapshot to compare against.")
        parser.add_argument(
            "--against",
            metavar="SNAPSHOT",
            help="With --compare: diff this snapshot file instead of the live database (no DB needed).",
        )
        parser.add_argument(
            "--expect-diff",
            metavar="PATH",
            help="Manifest of expected diff keys; only entries outside it fail the check.",
        )
        parser.add_argument(
            "--allow-stale",
            action="store_true",
            help="Do not fail when a manifest entry did not occur.",
        )
        parser.add_argument("--json", action="store_true", help="Print the diff as JSON instead of a report.")
        parser.add_argument(
            "--apps",
            nargs="+",
            metavar="LABEL",
            help="Restrict to tables owned by these Django app labels.",
        )
        parser.add_argument("--schema", default="public", help="Postgres schema to read (default: public).")
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS, help="Database alias (default: default).")

    def handle(self, *args, **options):
        if options["snapshot"]:
            for flag in ("against", "expect_diff", "json"):
                if options[flag]:
                    raise CommandError(f"--{flag.replace('_', '-')} only applies to --compare.", returncode=2)
            snapshot = self._capture(options, apps=options["apps"])
            text = schema_parity.dump_snapshot(snapshot)
            if options["snapshot"] == "-":
                self.stdout.write(text, ending="")
            else:
                Path(options["snapshot"]).write_text(text)
                self.stderr.write(f"Wrote {len(snapshot['tables'])} tables to {options['snapshot']}")
            return

        baseline = self._load(options["compare"])
        if options["against"]:
            actual = self._load(options["against"])
            actual_label = options["against"]
        else:
            # Capture every table and let diff_snapshots apply --apps, so a table
            # is in scope when EITHER side's label matches (an app-label move
            # keeps its db_table and must not read as remove + add).
            actual = self._capture(options, apps=None)
            actual_label = f"database {connections[options['database']].settings_dict.get('NAME')!s}"

        manifest = {}
        if options["expect_diff"]:
            try:
                manifest = schema_parity.parse_expected(Path(options["expect_diff"]).read_text())
            except OSError as exc:
                raise CommandError(f"Cannot read --expect-diff manifest: {exc}", returncode=2) from exc

        warnings = [w for w in [schema_parity.major_version_warning(baseline, actual)] if w]
        for warning in warnings:
            self.stderr.write(f"WARNING: {warning}")

        entries = schema_parity.diff_snapshots(baseline, actual, apps=options["apps"])
        entries, stale = schema_parity.classify(entries, manifest)
        summary = schema_parity.summary(entries, stale)
        stale_fails = bool(stale) and not options["allow_stale"]
        ok = summary["unexpected"] == 0 and not stale_fails

        if options["json"]:
            payload = {
                "baseline": options["compare"],
                "actual": actual_label,
                "apps": options["apps"],
                "ok": ok,
                "summary": summary,
                "entries": entries,
                "stale_expected": stale,
                "warnings": warnings,
            }
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
        else:
            self.stdout.write(
                schema_parity.format_report(entries, stale, options["compare"], actual_label, warnings)
            )

        if ok:
            return
        problems = []
        if summary["unexpected"]:
            problems.append(f"{summary['unexpected']} unexpected difference(s)")
        if stale_fails:
            problems.append(
                f"{len(stale)} expected-diff entr{'y' if len(stale) == 1 else 'ies'} did not occur "
                f"({', '.join(stale)}); remove them from the manifest or pass --allow-stale"
            )
        raise CommandError("Schema parity failed: " + "; ".join(problems) + ".", returncode=1)

    def _capture(self, options, apps) -> dict:
        try:
            connection = connections[options["database"]]
        except ConnectionDoesNotExist as exc:
            raise CommandError(f"Unknown --database alias {options['database']!r}.", returncode=2) from exc
        if connection.vendor != "postgresql":
            raise CommandError(
                f"check_schema_parity reads pg_catalog and needs PostgreSQL; database "
                f"'{options['database']}' is {connection.vendor}. Point DATABASE_URL at Postgres "
                "(e.g. --settings=config.settings.dev), or diff two snapshot files with --compare/--against.",
                returncode=2,
            )
        with connection.cursor() as cursor:
            return schema_parity.capture_snapshot(
                cursor, schema=options["schema"], table_apps=table_app_labels(), apps=apps
            )

    def _load(self, path) -> dict:
        try:
            return schema_parity.load_snapshot(path)
        except (OSError, ValueError) as exc:
            raise CommandError(f"Cannot read snapshot: {exc}", returncode=2) from exc

