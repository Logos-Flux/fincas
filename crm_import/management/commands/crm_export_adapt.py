"""Convert a provider's CRM export into the canonical import format.

    manage.py crm_export_adapt --provider hubspot --input <export dir> --output <canonical dir>
                               [--mapping mapping.toml] [--time-zone <IANA>]

Runs the export adapter that a plugin registered for ``--provider``
(``BasePlugin.get_crm_export_adapters``), then validates the output exactly
as ``check_canonical_export`` does and prints the coverage table with every
reason. Exits non-zero if the adapter refuses the input or the output does
not validate. Reads and writes files only: no database, no network. See
docs/crm/importing.md section 8.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.plugins import plugin_registry
from core.plugins.payroll import AdapterError
from crm_import.canonical import OBJECT_FILES, validate_canonical

#: What to do about each coverage reason (spec 2.13), printed under the table.
FIXES = {
    "forbidden": "grant the missing scope to the recorder's private app and re-crawl that endpoint",
    "search_cap": "re-crawl with the list endpoint (cursor paging has no cap) or use a file export",
    "page_missing": "re-run the recorder; it stopped before the last page",
    "count_mismatch": "re-crawl; records changed or were deleted during the crawl, or a referenced record is gone",
    "archived_not_enumerated": "record the archived listing (archived=true), or accept that archived records are absent",
    "history_not_exported": "record property history (propertiesWithHistory=…)",
    "pair_not_exported": "record the association pair, or accept that those links are absent",
    "file_absent": "export the missing object, or accept that it is absent",
    "truncated_export": "export again (split by a filter if the export hit its row limit)",
}


class Command(BaseCommand):
    help = "Convert a CRM provider export into the canonical import format (docs/crm/importing.md)."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--provider", required=True, help="the adapter's provider id, e.g. hubspot")
        parser.add_argument("--input", required=True, help="the provider export / recorded crawl directory")
        parser.add_argument("--output", required=True, help="the canonical directory to write (new or empty)")
        parser.add_argument("--mapping", help="the mapping file (TOML); default <input>/mapping.toml if present")
        parser.add_argument("--time-zone", dest="time_zone",
                            help="IANA zone of offset-less CSV date-times (the exporting user's zone)")

    def handle(self, *args, **options):
        provider = options["provider"]
        adapters = plugin_registry.get_crm_export_adapters()
        adapter_cls = adapters.get(provider)
        if adapter_cls is None:
            installed = ", ".join(sorted(adapters)) or "none"
            raise CommandError(
                f"No CRM export adapter {provider!r} is installed (installed: {installed}). Enable its plugin, "
                "e.g. GOLDEN_PLUGINS = [\"golden_erp.addons.hubspot_export.plugin.HubSpotExportPlugin\"]. "
                "See docs/crm/importing.md section 8."
            )
        source, out = Path(options["input"]), Path(options["output"])
        if not source.is_dir():
            raise CommandError(f"{source} is not a directory.")
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            raise CommandError(f"{out} exists and is not an empty directory; the adapter writes a fresh one.")
        mapping = None
        if options.get("mapping"):
            import tomllib

            try:
                with Path(options["mapping"]).open("rb") as fh:
                    mapping = tomllib.load(fh)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise CommandError(f"Cannot read the mapping file {options['mapping']}: {exc}") from exc
        try:
            manifest = adapter_cls().export(source, out, mapping=mapping, time_zone=options.get("time_zone"))
        except AdapterError as exc:
            raise CommandError(str(exc)) from exc

        result = validate_canonical(out)
        self.stdout.write(f"Canonical export {out} from {manifest['source']} ({manifest['input']['kind']})")
        self.stdout.write(f"  {'object type':<14} {'file':<22} {'rows':>7}  coverage")
        codes: set[str] = set()
        for otype, fname in OBJECT_FILES.items():
            entry = manifest["objects"].get(otype, {})
            self.stdout.write(f"  {otype:<14} {fname:<22} {entry.get('rows', 0):>7}  {entry.get('coverage', '?')}")
            for reason in entry.get("reasons", []):
                codes.add(reason["code"])
                self.stdout.write(f"      {reason['code']}: {reason['detail']}")
        for code in sorted(codes):
            self.stdout.write(self.style.WARNING(f"  fix for {code}: {FIXES.get(code, 'see the spec, 2.13')}"))
        for issue in result.errors:
            self.stdout.write(self.style.ERROR(f"ERROR   {issue}"))
        for issue in result.warnings:
            self.stdout.write(self.style.WARNING(f"WARNING {issue}"))
        if not result.ok:
            raise CommandError(f"The adapter's output does not validate ({len(result.errors)} error(s)); "
                               "this is an adapter bug. See docs/crm/importing.md.")
        self.stdout.write(self.style.SUCCESS(
            f"OK: {sum(result.counts.values())} records; every partial or unknown line is a decision "
            "(fix the export, or accept it and write down why)."
        ))
