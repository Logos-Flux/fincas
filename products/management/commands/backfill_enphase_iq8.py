"""
Backfill Enphase IQ8 microinverter products with formatted descriptions,
structured specs (inverters schema), shipping flags, and variant dimension fixes.

These 3 products were created manually and need enrichment.
Each product.save() triggers the D1 webhook sync.

Products:
  enphase-iq8mc-microinverter  (IQ8MC, 330VA peak, 260-460W panels)
  enphase-iq8ac-microinverter  (IQ8AC, 366VA peak, 295-500W panels)
  enphase-iq8hc-microinverter  (IQ8HC, 384VA peak, 320-540W+, DOM-US variant)

Usage:
  python manage.py backfill_enphase_iq8
  python manage.py backfill_enphase_iq8 --dry-run
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from products.models import Product, ProductVariant

# ── Descriptions ──────────────────────────────────────────────────

DESCRIPTIONS = {
    "enphase-iq8mc-microinverter": (
        "Enphase IQ8MC Microinverter\n\n"
        "The Enphase IQ8MC is a microgrid-forming, software-defined microinverter "
        "with split-phase power conversion capability. Built on Enphase's proprietary "
        "ASIC with advanced 55nm technology, the IQ8MC enables operation in both "
        "grid-tied and off-grid modes with superfast response times to changing loads "
        "and grid events. Designed for standard-power solar modules rated 260-460W, "
        "it supports 54/108, 60/120, 66/132, and 72/144 half-cell PV modules.\n\n"
        "Key Features\n"
        "- Microgrid-Forming Technology: Enables both grid-tied and off-grid operation with Enphase Sunlight Backup\n"
        "- 330 VA Peak Output: Optimized for standard-power 260-460W solar modules\n"
        "- 97.4% Peak Efficiency: Maximum energy harvest with minimal conversion loss\n"
        "- Software-Defined Architecture: Over-the-air updates via Enphase Envoy gateway\n"
        "- Panel-Level Optimization: Independent MPPT per module eliminates mismatch losses\n"
        "- Rapid Shutdown Compliant: Meets NEC 690.12 module-level rapid shutdown requirements\n\n"
        "Specifications Highlights\n"
        "- Peak Output Power: 330 VA at 240V, 315 VA at 208V\n"
        "- Continuous Output Power: 320 VA at 240V, 310 VA at 208V\n"
        "- MPPT Range: 25-45 VDC\n"
        "- Max Input Voltage: 60 VDC\n"
        "- Branch Circuit: Up to 12 units per 20A branch at 240V\n"
        "- Communication: Power Line Communication (PLC)\n\n"
        "Applications\n"
        "- Residential rooftop solar installations\n"
        "- Off-grid and backup power systems with Enphase Sunlight Backup\n"
        "- New construction and solar-ready homes\n"
        "- Solar panel upgrade and retrofit projects\n\n"
        "Warranty and Certifications\n"
        "Up to 25-year limited warranty. UL 1741-SA/SB, IEEE 1547, NEC 690.12 compliant. "
        "NEMA Type 6 and IP67 rated for all-weather outdoor installation."
    ),
    "enphase-iq8ac-microinverter": (
        "Enphase IQ8AC Microinverter\n\n"
        "The Enphase IQ8AC is a microgrid-forming, software-defined microinverter "
        "with split-phase power conversion capability. Built on Enphase's proprietary "
        "ASIC with advanced 55nm technology, the IQ8AC enables operation in both "
        "grid-tied and off-grid modes with superfast response times to changing loads "
        "and grid events. Designed for mid-power solar modules rated 295-500W, "
        "it supports 54/108, 60/120, 66/132, and 72/144 half-cell PV modules.\n\n"
        "Key Features\n"
        "- Microgrid-Forming Technology: Enables both grid-tied and off-grid operation with Enphase Sunlight Backup\n"
        "- 366 VA Peak Output: Optimized for mid-power 295-500W solar modules\n"
        "- 97.3% Peak Efficiency: Maximum energy harvest with minimal conversion loss\n"
        "- Software-Defined Architecture: Over-the-air updates via Enphase Envoy gateway\n"
        "- Panel-Level Optimization: Independent MPPT per module eliminates mismatch losses\n"
        "- Rapid Shutdown Compliant: Meets NEC 690.12 module-level rapid shutdown requirements\n\n"
        "Specifications Highlights\n"
        "- Peak Output Power: 366 VA at 240V, 350 VA at 208V\n"
        "- Continuous Output Power: 349 VA at 240V, 345 VA at 208V\n"
        "- MPPT Range: 28-45 VDC\n"
        "- Max Input Voltage: 60 VDC\n"
        "- Branch Circuit: Up to 11 units per 20A branch at 240V\n"
        "- Communication: Power Line Communication (PLC)\n\n"
        "Applications\n"
        "- Residential rooftop solar installations\n"
        "- Off-grid and backup power systems with Enphase Sunlight Backup\n"
        "- New construction and solar-ready homes\n"
        "- Solar panel upgrade and retrofit projects\n\n"
        "Warranty and Certifications\n"
        "Up to 25-year limited warranty. UL 1741-SA/SB, IEEE 1547, NEC 690.12 compliant. "
        "NEMA Type 6 and IP67 rated for all-weather outdoor installation."
    ),
    "enphase-iq8hc-microinverter": (
        "Enphase IQ8HC Microinverter\n\n"
        "The Enphase IQ8HC is the industry's first microgrid-forming, software-defined "
        "microinverter with split-phase power conversion capability. Built on Enphase's "
        "proprietary ASIC with advanced 55nm technology, the IQ8HC enables operation in "
        "both grid-tied and off-grid modes with superfast response times to changing loads "
        "and grid events. Designed for today's high-power solar modules rated 320-540W+, "
        "it supports 54/108, 60/120, 66/132, and 72/144 half-cell PV modules.\n\n"
        "Key Features\n"
        "- Microgrid-Forming Technology: Enables both grid-tied and off-grid operation with Enphase Sunlight Backup\n"
        "- 384 VA Peak Output: Designed for high-power 320-540W+ solar modules\n"
        "- 97.3% Peak Efficiency: Maximum energy harvest with minimal conversion loss\n"
        "- DOM-US Variant Available: Domestically manufactured PCB and enclosure qualifies for IRS Domestic Content Bonus Credit\n"
        "- Software-Defined Architecture: Over-the-air updates via Enphase Envoy gateway\n"
        "- Panel-Level Optimization: Independent MPPT per module eliminates mismatch losses\n"
        "- Rapid Shutdown Compliant: Meets NEC 690.12 module-level rapid shutdown requirements\n\n"
        "Specifications Highlights\n"
        "- Peak Output Power: 384 VA at 240V, 366 VA at 208V\n"
        "- Continuous Output Power: 380 VA at 240V, 360 VA at 208V\n"
        "- MPPT Range: 29.5-45 VDC\n"
        "- Max Input Voltage: 60 VDC\n"
        "- Branch Circuit: Up to 10 units per 20A branch at 240V\n"
        "- Communication: Power Line Communication (PLC)\n\n"
        "Applications\n"
        "- Residential rooftop solar installations\n"
        "- Off-grid and backup power systems with Enphase Sunlight Backup\n"
        "- New construction and solar-ready homes\n"
        "- Solar panel upgrade and retrofit projects\n"
        "- Projects requiring IRS Domestic Content Bonus Credit (DOM-US variant)\n\n"
        "Warranty and Certifications\n"
        "Up to 25-year limited warranty. UL 1741-SA/SB, IEEE 1547, NEC 690.12 compliant. "
        "NEMA Type 6 and IP67 rated for all-weather outdoor installation."
    ),
}


# ── Structured Specs (inverters schema) ───────────────────────────

STRUCTURED_SPECS = {
    "enphase-iq8mc-microinverter": {
        "rated_power_continuous_w": {"value": 320, "unit": "VA"},
        "peak_power_w": {"value": 330, "unit": "VA"},
        "peak_efficiency": {"value": 97.4, "unit": "%"},
        "cec_efficiency": {"value": 97.0, "unit": "%"},
        "mppt_voltage_range_min": {"value": 25, "unit": "V"},
        "mppt_voltage_range_max": {"value": 45, "unit": "V"},
        "max_dc_input_voltage": {"value": 60, "unit": "V"},
        "ac_output_voltage": {"value": "240V split-phase / 208V"},
        "ac_output_frequency": {"value": 60, "unit": "Hz"},
        "phases": {"value": "Split-phase"},
        "inverter_type": {"value": "Microinverter"},
        "ip_rating": {"value": "IP67 / NEMA Type 6"},
        "operating_temp_max": {"value": 65, "unit": "\u00b0C"},
        "weight_kg": {"value": 1.1, "unit": "kg"},
        "dimensions_mm": {"value": "212 x 175 x 30.2", "unit": "mm"},
        "warranty_years": {"value": 25, "unit": "years"},
    },
    "enphase-iq8ac-microinverter": {
        "rated_power_continuous_w": {"value": 349, "unit": "VA"},
        "peak_power_w": {"value": 366, "unit": "VA"},
        "peak_efficiency": {"value": 97.3, "unit": "%"},
        "cec_efficiency": {"value": 97.0, "unit": "%"},
        "mppt_voltage_range_min": {"value": 28, "unit": "V"},
        "mppt_voltage_range_max": {"value": 45, "unit": "V"},
        "max_dc_input_voltage": {"value": 60, "unit": "V"},
        "ac_output_voltage": {"value": "240V split-phase / 208V"},
        "ac_output_frequency": {"value": 60, "unit": "Hz"},
        "phases": {"value": "Split-phase"},
        "inverter_type": {"value": "Microinverter"},
        "ip_rating": {"value": "IP67 / NEMA Type 6"},
        "operating_temp_max": {"value": 65, "unit": "\u00b0C"},
        "weight_kg": {"value": 1.1, "unit": "kg"},
        "dimensions_mm": {"value": "212 x 175 x 30.2", "unit": "mm"},
        "warranty_years": {"value": 25, "unit": "years"},
    },
    "enphase-iq8hc-microinverter": {
        "rated_power_continuous_w": {"value": 380, "unit": "VA"},
        "peak_power_w": {"value": 384, "unit": "VA"},
        "peak_efficiency": {"value": 97.3, "unit": "%"},
        "cec_efficiency": {"value": 97.0, "unit": "%"},
        "mppt_voltage_range_min": {"value": 29.5, "unit": "V"},
        "mppt_voltage_range_max": {"value": 45, "unit": "V"},
        "max_dc_input_voltage": {"value": 60, "unit": "V"},
        "ac_output_voltage": {"value": "240V split-phase / 208V"},
        "ac_output_frequency": {"value": 60, "unit": "Hz"},
        "phases": {"value": "Split-phase"},
        "inverter_type": {"value": "Microinverter"},
        "ip_rating": {"value": "IP67 / NEMA Type 6"},
        "operating_temp_max": {"value": 65, "unit": "\u00b0C"},
        "weight_kg": {"value": 1.1, "unit": "kg"},
        "dimensions_mm": {"value": "212 x 175 x 30.2", "unit": "mm"},
        "warranty_years": {"value": 25, "unit": "years"},
    },
}


SLUGS = [
    "enphase-iq8mc-microinverter",
    "enphase-iq8ac-microinverter",
    "enphase-iq8hc-microinverter",
]


class Command(BaseCommand):
    help = (
        "Backfill Enphase IQ8 products with formatted descriptions, "
        "structured specs, shipping flags, and variant dimension fixes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be updated without writing to the database.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing will be saved.\n"))

        for slug in SLUGS:
            try:
                product = Product.objects.get(slug=slug)
            except Product.DoesNotExist:
                self.stdout.write(self.style.ERROR(f"  [MISSING] {slug} — skipping"))
                continue

            self.stdout.write(f"\n  [product] {product.title} ({slug})")

            # ── 1. Description ────────────────────────────────────
            new_description = DESCRIPTIONS[slug]
            desc_changed = product.description != new_description
            if desc_changed:
                self.stdout.write(f"    [description] will update ({len(new_description)} chars)")
            else:
                self.stdout.write("    [description] already current")

            # ── 2. Structured specs ───────────────────────────────
            new_specs = STRUCTURED_SPECS[slug]
            specs_changed = product.structured_specs != new_specs
            if specs_changed:
                self.stdout.write(f"    [structured_specs] will set {len(new_specs)} keys")
            else:
                self.stdout.write("    [structured_specs] already current")

            # ── 3. Shipping flags ─────────────────────────────────
            shipping_updates = {}
            flag_targets = {
                "ships_usps": True,
                "ships_ups": True,
                "ships_ltl": False,
                "free_shipping": False,
                "hazmat_flag": False,
                "oversized_flag": False,
            }
            for field, target in flag_targets.items():
                if getattr(product, field) != target:
                    shipping_updates[field] = target

            if shipping_updates:
                self.stdout.write(
                    f"    [shipping flags] will update: {', '.join(shipping_updates.keys())}"
                )
            else:
                self.stdout.write("    [shipping flags] already correct")

            # ── Apply product updates ─────────────────────────────
            update_fields = []
            if desc_changed:
                product.description = new_description
                update_fields.append("description")
            if specs_changed:
                product.structured_specs = new_specs
                update_fields.append("structured_specs")
            for field, value in shipping_updates.items():
                setattr(product, field, value)
                update_fields.append(field)

            if update_fields and not dry_run:
                product.save(update_fields=update_fields + ["updated_at"])
                self.stdout.write(
                    self.style.SUCCESS(
                        f"    -> saved: {', '.join(update_fields)}"
                    )
                )
            elif update_fields:
                self.stdout.write(f"    -> would save: {', '.join(update_fields)}")
            else:
                self.stdout.write("    -> no product changes needed")

            # ── 4. Variant dimension fixes ────────────────────────
            variants = ProductVariant.objects.filter(product=product)
            for variant in variants:
                self.stdout.write(f"    [variant] {variant.sku}")
                v_updates = {}

                # Fix wrong dimensions (shipping_height=7 or shipping_length=2)
                needs_dim_fix = (
                    variant.shipping_height == Decimal("7")
                    or variant.shipping_height == Decimal("7.00")
                    or variant.shipping_length == Decimal("2")
                    or variant.shipping_length == Decimal("2.00")
                    or variant.shipping_length is None
                    or variant.shipping_width is None
                    or variant.shipping_height is None
                )

                if needs_dim_fix:
                    dim_targets = {
                        "shipping_length": Decimal("10.00"),
                        "shipping_width": Decimal("8.00"),
                        "shipping_height": Decimal("3.00"),
                    }
                    for field, target in dim_targets.items():
                        if getattr(variant, field) != target:
                            v_updates[field] = target
                    if v_updates:
                        self.stdout.write(
                            f"      [dimensions] will fix: {', '.join(v_updates.keys())} "
                            f"(was {variant.shipping_length}x{variant.shipping_width}x{variant.shipping_height})"
                        )

                # Shipping carrier flags on variant
                v_flag_targets = {
                    "ships_usps": True,
                    "ships_ups": True,
                }
                for field, target in v_flag_targets.items():
                    if getattr(variant, field) != target:
                        v_updates[field] = target

                if v_updates and not dry_run:
                    for field, value in v_updates.items():
                        setattr(variant, field, value)
                    variant.save(update_fields=list(v_updates.keys()) + ["updated_at"])
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"      -> saved: {', '.join(v_updates.keys())}"
                        )
                    )
                elif v_updates:
                    self.stdout.write(f"      -> would save: {', '.join(v_updates.keys())}")
                else:
                    self.stdout.write("      -> no variant changes needed")

        self.stdout.write(self.style.SUCCESS("\nDone."))
