"""
Management command: Populate SureSine variant option dimensions.

Parses the SKU pattern SI-{WATTAGE}-{BATTV}-{ACV}-{FREQ}-{STYLE} and creates:
  - 4 ProductOption records (Wattage, Battery Voltage, AC Output, Output Style)
  - ProductOptionValue records for each dimension
  - VariantOptionValue junction records linking each variant to its 4 values

Usage:
  python manage.py populate_suresine_options --dry-run   # preview only
  python manage.py populate_suresine_options              # execute
"""

import re

from django.core.management.base import BaseCommand
from django.db import transaction

from products.models import (
    Product,
    ProductOption,
    ProductOptionValue,
    ProductVariant,
    VariantOptionValue,
)

# SKU pattern: SI-{wattage}-{battery_v}-{ac_v}-{freq}-{style}
SKU_RE = re.compile(
    r"^SI-(\d+)-(\d+)-(\d+)-(\d+)-(B|HW|UNI)$"
)

# Legacy SKUs to skip (different pattern)
SKIP_SKUS = {"SI-15-24-12-HA"}

# Display mappings
OUTPUT_STYLE_MAP = {
    "B": "Type B Receptacle",
    "HW": "Hard-Wired",
    "UNI": "Universal Receptacle",
}

# Option definitions with sort order and known values
OPTION_DEFS = [
    {
        "name": "wattage",
        "display": "Wattage",
        "sort_order": 0,
        "values": ["150W", "300W", "700W", "1000W", "1250W", "2500W"],
    },
    {
        "name": "battery_voltage",
        "display": "Battery Voltage",
        "sort_order": 1,
        "values": ["12V", "24V", "48V"],
    },
    {
        "name": "ac_output",
        "display": "AC Output",
        "sort_order": 2,
        "values": ["120V 60Hz", "127V 60Hz", "220V 60Hz", "230V 50Hz"],
    },
    {
        "name": "output_style",
        "display": "Output Style",
        "sort_order": 3,
        "values": ["Type B Receptacle", "Hard-Wired", "Universal Receptacle"],
    },
]


def parse_sku(sku):
    """Parse a SureSine SKU into option values. Returns dict or None."""
    m = SKU_RE.match(sku)
    if not m:
        return None
    wattage, batt_v, ac_v, freq, style_code = m.groups()
    return {
        "wattage": f"{wattage}W",
        "battery_voltage": f"{batt_v}V",
        "ac_output": f"{ac_v}V {freq}Hz",
        "output_style": OUTPUT_STYLE_MAP[style_code],
    }


class Command(BaseCommand):
    help = "Populate SureSine product option dimensions from variant SKU patterns"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without saving",
        )

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        if self.dry_run:
            self.stdout.write(self.style.WARNING("=== DRY RUN — no changes will be saved ===\n"))

        with transaction.atomic():
            self._run()
            if self.dry_run:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("\n=== DRY RUN complete — all changes rolled back ==="))
            else:
                self.stdout.write(self.style.SUCCESS("\n=== Done ==="))

    def _run(self):
        # 1. Find parent product
        try:
            parent = Product.objects.get(part_number="SI")
        except Product.DoesNotExist:
            self.stdout.write(self.style.ERROR("Parent product with part_number='SI' not found!"))
            return

        self.stdout.write(f"Found parent: {parent.title} (id={parent.id})")

        # 2. Create options and collect value objects
        # option_name → ProductOption
        option_map = {}
        # (option_name, value_label) → ProductOptionValue
        value_map = {}

        for odef in OPTION_DEFS:
            opt, created = ProductOption.objects.get_or_create(
                product=parent,
                name=odef["name"],
                defaults={
                    "sort_order": odef["sort_order"],
                },
            )
            action = "Created" if created else "Found"
            self.stdout.write(f"  {action} option: {odef['display']} (sort={odef['sort_order']})")
            option_map[odef["name"]] = opt

            for vi, val_label in enumerate(odef["values"]):
                ov, ov_created = ProductOptionValue.objects.get_or_create(
                    option=opt,
                    value=val_label,
                    defaults={"sort_order": vi},
                )
                ov_action = "Created" if ov_created else "Found"
                self.stdout.write(f"    {ov_action} value: {val_label}")
                value_map[(odef["name"], val_label)] = ov

        # 3. Parse variants and create junction records
        variants = ProductVariant.objects.filter(product=parent)
        self.stdout.write(f"\nProcessing {variants.count()} variants...")

        parsed = 0
        skipped = 0
        junctions_created = 0

        for variant in variants:
            if variant.sku in SKIP_SKUS:
                self.stdout.write(self.style.WARNING(f"  SKIP (legacy): {variant.sku}"))
                skipped += 1
                continue

            values = parse_sku(variant.sku)
            if values is None:
                self.stdout.write(self.style.WARNING(f"  SKIP (no match): {variant.sku}"))
                skipped += 1
                continue

            parsed += 1
            for opt_name, val_label in values.items():
                key = (opt_name, val_label)
                if key not in value_map:
                    # Value not in predefined list — create it dynamically
                    opt = option_map[opt_name]
                    ov, _ = ProductOptionValue.objects.get_or_create(
                        option=opt,
                        value=val_label,
                        defaults={"sort_order": 99},
                    )
                    value_map[key] = ov
                    self.stdout.write(self.style.WARNING(
                        f"    Auto-created unexpected value: {opt_name}={val_label}"
                    ))

                ov = value_map[key]
                _, jc = VariantOptionValue.objects.get_or_create(
                    variant=variant,
                    option_value=ov,
                )
                if jc:
                    junctions_created += 1

        self.stdout.write(f"\nParsed: {parsed}, Skipped: {skipped}")
        self.stdout.write(f"Junction records created: {junctions_created}")
