"""
Set structured_specs on products from a JSON file — generic, schema-validated.

Originally built for OP WP #312 (solar panel specs not reaching the website),
which reduced to (a) a frontend parse bug — fixed in apps/web — and (b) a data
gap: products whose `structured_specs` were never populated. This command is the
backfill mechanism for (b). It works for ANY spec category the instance's
spec validator knows (solar_panels, batteries, inverters, ...). The validator is
the `products.validate_structured_specs` hook (core/hook_registry.py; a
downstream forge add-on provides it). Without one, specs are written unvalidated, with a
warning.

INPUT FORMAT (JSON object):
  {
    "<match-value>": {
      "<spec_key>": {"value": <number|string|bool>, "unit": "<optional unit>"},
      ...
    },
    ...
  }

`<match-value>` is matched against the product's slug by default; use
`--match-by sku` or `--match-by part_number` to change. `--category` selects the
schema used for validation (default: solar_panels).

Each product.save() fires the storefront_views signals, which refresh the
`storefront.products` materialized view on commit — so the new specs land on the
site without a manual refresh. (The 5-min `refresh_storefront_views` cron is the
backstop.)

Examples:
  # Dry run against the bundled CW Energy template:
  python manage.py set_structured_specs \\
      --file products/data/cw_energy_solar_panel_specs.json --dry-run

  # Apply, matching on part_number, validating against the solar_panels schema:
  python manage.py set_structured_specs --file specs.json --match-by part_number

  # Merge new keys into existing structured_specs instead of replacing:
  python manage.py set_structured_specs --file specs.json --merge
"""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core import hook_registry
from products.models import Product


class Command(BaseCommand):
    help = (
        "Load structured_specs onto products from a JSON file, validated "
        "against a spec schema category."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            required=True,
            help="Path to a JSON file mapping match-value -> {spec_key: {value, unit}}.",
        )
        parser.add_argument(
            "--match-by",
            choices=["slug", "sku", "part_number"],
            default="slug",
            help="Product field to match the JSON keys against (default: slug).",
        )
        parser.add_argument(
            "--category",
            default="solar_panels",
            help="Spec schema category key for validation (default: solar_panels).",
        )
        parser.add_argument(
            "--merge",
            action="store_true",
            help="Merge spec keys into existing structured_specs instead of replacing.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and print what would change without writing.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        file_path = options["file"]
        match_by = options["match_by"]
        category = options["category"]
        merge = options["merge"]
        dry_run = options["dry_run"]

        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except OSError as exc:
            raise CommandError(f"Could not read --file {file_path}: {exc}")
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON in {file_path}: {exc}")

        if not isinstance(payload, dict) or not payload:
            raise CommandError(
                "Expected a non-empty JSON object {match-value: {spec_key: {...}}}."
            )

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN — nothing will be saved.")
            )

        validate_structured_specs = hook_registry.get("products.validate_structured_specs")
        if validate_structured_specs is None:
            self.stdout.write(
                self.style.WARNING(
                    "No structured-spec validator is installed "
                    "(hook products.validate_structured_specs); specs are not validated."
                )
            )

        lookup = {
            "slug": "slug",
            "sku": "variants__sku",  # match a parent via its variant SKU
            "part_number": "part_number",
        }[match_by]
        distinct = "variants__sku" == lookup

        updated = 0
        skipped = 0
        for match_value, specs in payload.items():
            # Skip documentation/metadata keys (e.g. "__doc__") so a template
            # file can carry instructions and still be fed directly to the command.
            if isinstance(match_value, str) and match_value.startswith("__"):
                continue

            qs = Product.objects.filter(**{lookup: match_value})
            if distinct:
                qs = qs.distinct()
            product = qs.first()

            if product is None:
                self.stdout.write(
                    self.style.ERROR(
                        f"  [MISSING] {match_by}={match_value!r} — no product; skipping"
                    )
                )
                skipped += 1
                continue

            if not isinstance(specs, dict):
                self.stdout.write(
                    self.style.ERROR(
                        f"  [INVALID] {match_value!r}: specs must be an object; skipping"
                    )
                )
                skipped += 1
                continue

            # Drop inline documentation keys (e.g. "__product__") so they never
            # reach structured_specs or the validator.
            clean_specs = {
                k: v for k, v in specs.items()
                if not (isinstance(k, str) and k.startswith("__"))
            }

            # Validate against the category schema; surface warnings but proceed.
            warnings = (
                validate_structured_specs(category, clean_specs)
                if validate_structured_specs is not None
                else []
            )
            for w in warnings:
                self.stdout.write(self.style.WARNING(f"  [{match_value}] {w}"))

            new_specs = dict(product.structured_specs) if merge else {}
            new_specs.update(clean_specs)

            changed = product.structured_specs != new_specs
            self.stdout.write(
                f"  [product] {product.title} ({match_by}={match_value})"
            )
            self.stdout.write(
                f"    [structured_specs] {len(new_specs)} keys"
                f"{' (merge)' if merge else ''}"
                f"{' — would set' if dry_run else ' — setting'}"
                if changed
                else "    [structured_specs] already current"
            )

            if changed and not dry_run:
                product.structured_specs = new_specs
                product.save(update_fields=["structured_specs", "updated_at"])
                # save() fires storefront_views signals -> MV refresh on commit.
                self.stdout.write(self.style.SUCCESS("    -> saved"))
                updated += 1
            elif changed:
                updated += 1
            else:
                skipped += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. affected={updated} skipped={skipped} dry_run={dry_run}"
            )
        )
