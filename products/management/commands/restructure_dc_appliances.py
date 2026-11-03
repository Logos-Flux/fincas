"""
Management command: DC Appliance product/category restructuring.

Phases:
  1. Rename "Snap-Fan" category → "DC Appliance Accessories" (top-level)
  2. Create consolidated "DC Brushless Snap-Fans by Fogco" parent, move fan variants
  3. Create "Grundfos Well Pumps" and "Grundfos Pump Accessories" parents, split variants
  4. Category reassignments
  5. Update all Snap-Fan prices to 2026 Fogco MSRP
  6. Deactivate old parent products and bundle

Usage:
  python manage.py restructure_dc_appliances --dry-run   # preview only
  python manage.py restructure_dc_appliances              # execute
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from products.models import Category, Product, ProductCategory, ProductVariant

# -- Price data from 2026 Fogco price sheet ------------------------------------

PRICE_UPDATES = {
    # Fan variants (will be renamed from -STD)
    "SF.P12BLDC24": Decimal("523.20"),
    "SF.P16BLDC24": Decimal("550.45"),
    "SF.P20BLDC24": Decimal("599.50"),
    "SF.P20BLDC30": Decimal("828.40"),
    "SF.P24BLDC30": Decimal("970.10"),
    # Thermostat
    "SF.9939-STD": Decimal("107.91"),
    # Solar kits
    "SF.9912BL": Decimal("866.55"),
    "SF.9916BL": Decimal("975.55"),
    "SF.9920BL": Decimal("1084.55"),
    # Exhaust shutters
    "SFE.6900-12": Decimal("98.10"),
    "SFE.6900-16": Decimal("114.45"),
    "SFE.6900-20": Decimal("141.70"),
    "SFE.6900-24": Decimal("174.40"),
    # Intake guards
    "SF.1222": Decimal("68.67"),
    "SF.1622": Decimal("87.20"),
    "SF.2022": Decimal("128.62"),
    "SF.2422": Decimal("144.97"),
    # Exhaust guards
    "SF.1223": Decimal("68.67"),
    "SF.1623": Decimal("87.20"),
    "SF.2023": Decimal("128.62"),
    "SF.2423": Decimal("144.97"),
    # Bundle variants (SFS.P12BLDC parent stays inactive, but update prices)
    "SFS.P12BLDC-12": Decimal("523.20"),
    "SFS.P12BLDC-16": Decimal("550.45"),
    "SFS.P12BLDC-20": Decimal("599.50"),
    "SFS.P12BLDC-24": Decimal("970.10"),
}

# Fan SKUs: old parent part_number → variant SKU to rename from -STD
FAN_PARENT_SKUS = [
    "SF.P12BLDC24",
    "SF.P16BLDC24",
    "SF.P20BLDC24",
    "SF.P20BLDC30",
    "SF.P24BLDC30",
]

# Grundfos: well pump variant SKUs
GRUNDFOS_PUMP_SKUS = [
    "10SQ05-160-120V",
    "10SQ05-160-240V",
    "10SQ07-200-240V",
    "10SQ07-240-240V",
    "11SQF2",
    "15SQ07-180-240V",
    "15SQ10-250-240V",
    "22SQ15-220-240V",
    "6SQF3",
]

# Grundfos: accessory variant SKUs
GRUNDFOS_ACC_SKUS = [
    "CU200",
    "CU301",
    "E.SYBOXMINI",
    "GF96481502-V02",
    "GRFSFSW",
    "IO101",
    "PC10-211F",
    "PC12-211F",
]

# Products that go in DC Appliance Accessories category
ACCESSORY_PART_NUMBERS = ["SF.9939", "SFS.2423", "SFS.2022", "SFE.6900"]

# Products that go in DC Appliances category
DC_APPLIANCES_PART_NUMBERS = ["SF99BLKIT"]  # + SNAP-FAN-BLDC, GRUNDFOS-WELLPUMPS


class Command(BaseCommand):
    help = "Restructure DC Appliance products and categories (6 phases)"

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
            self.phase_1_rename_category()
            self.phase_2_consolidate_fans()
            self.phase_3_split_grundfos()
            self.phase_4_category_assignments()
            self.phase_5_update_prices()
            self.phase_6_deactivate_old()

            if self.dry_run:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("\n=== DRY RUN complete — all changes rolled back ==="))
            else:
                self.stdout.write(self.style.SUCCESS("\n=== All phases complete ==="))

    # -- Phase 1 ---------------------------------------------------------------

    def phase_1_rename_category(self):
        self.stdout.write(self.style.MIGRATE_HEADING("\nPhase 1: Rename Snap-Fan → DC Appliance Accessories"))

        try:
            cat = Category.objects.get(slug="snap-fan")
        except Category.DoesNotExist:
            # Maybe already renamed
            try:
                cat = Category.objects.get(slug="dc-appliance-accessories")
                self.stdout.write("  Already renamed — skipping")
                return
            except Category.DoesNotExist:
                self.stdout.write(self.style.ERROR("  Category 'snap-fan' not found!"))
                return

        old_title = cat.title
        cat.title = "DC Appliance Accessories"
        cat.slug = "dc-appliance-accessories"
        cat.parent = None  # Make top-level
        cat.tree_depth = 0
        cat.description = (
            "Accessories for DC appliances including Snap-Fan guards, shutters, "
            "thermostat kits, Grundfos pump controllers, switch boxes, and pump wire."
        )
        cat.save()
        self.stdout.write(f'  Renamed "{old_title}" → "{cat.title}" (top-level)')

    # -- Phase 2 ---------------------------------------------------------------

    def phase_2_consolidate_fans(self):
        self.stdout.write(self.style.MIGRATE_HEADING("\nPhase 2: Consolidate fans → single parent"))

        # Find or create the Fogco brand
        from products.models import Brand
        fogco_brand = Brand.objects.filter(title__icontains="fogco").first()

        # Create consolidated parent product
        new_parent, created = Product.objects.get_or_create(
            part_number="SNAP-FAN-BLDC",
            defaults={
                "title": "DC Brushless Snap-Fans by Fogco",
                "has_variants": True,
                "is_active": True,
                "visible_on_website": True,
                "brand": fogco_brand,
                "description": (
                    "Brushless DC Snap-Fans by Fogco for off-grid ventilation. "
                    "Solar-ready, 12-24V fans from 12\" to 24\" with airflow up to "
                    "4,739 CFM. Perfect for greenhouses, barns, workshops, and any "
                    "space needing efficient DC-powered cooling."
                ),
            },
        )
        action = "Created" if created else "Found existing"
        self.stdout.write(f"  {action} parent: SNAP-FAN-BLDC (id={new_parent.id})")

        # Move each fan's -STD variant to the new parent with cleaned SKU
        fan_names = {
            "SF.P12BLDC24": '12" Brushless DC Snap-Fan (12-24V)',
            "SF.P16BLDC24": '16" DC Brushless Snap-Fan (12-24V)',
            "SF.P20BLDC24": '20" DC Brushless Snap-Fan (12-24V)',
            "SF.P20BLDC30": '20" HP DC Brushless Snap-Fan (24V)',
            "SF.P24BLDC30": '24" DC Brushless Snap-Fan (24V DC)',
        }

        for i, parent_pn in enumerate(FAN_PARENT_SKUS):
            old_variant_sku = f"{parent_pn}-STD"
            new_variant_sku = parent_pn

            try:
                variant = ProductVariant.objects.get(sku=old_variant_sku)
            except ProductVariant.DoesNotExist:
                # Maybe already renamed
                try:
                    variant = ProductVariant.objects.get(sku=new_variant_sku)
                    if variant.product_id == new_parent.id:
                        self.stdout.write(f"  {new_variant_sku} already moved — skipping")
                        continue
                except ProductVariant.DoesNotExist:
                    self.stdout.write(self.style.ERROR(f"  Variant {old_variant_sku} not found!"))
                    continue

            variant.sku = new_variant_sku
            variant.product = new_parent
            variant.name = fan_names.get(parent_pn, variant.name)
            variant.sort_order = i
            variant.save()
            self.stdout.write(f"  Moved {old_variant_sku} → {new_variant_sku} under SNAP-FAN-BLDC")

    # -- Phase 3 ---------------------------------------------------------------

    def phase_3_split_grundfos(self):
        self.stdout.write(self.style.MIGRATE_HEADING("\nPhase 3: Split Grundfos into pumps + accessories"))

        from products.models import Brand
        grundfos_brand = Brand.objects.filter(title__icontains="grundfos").first()

        # Create Grundfos Well Pumps parent
        pumps_parent, created = Product.objects.get_or_create(
            part_number="GRUNDFOS-WELLPUMPS",
            defaults={
                "title": "Grundfos Well Pumps",
                "has_variants": True,
                "is_active": True,
                "visible_on_website": True,
                "brand": grundfos_brand,
                "description": (
                    "Grundfos SQ and SQFlex submersible well pumps. "
                    "SQ series for standard AC applications, SQFlex for "
                    "solar/wind/AC multi-source power. Lift water up to 820 ft."
                ),
            },
        )
        self.stdout.write(f"  {'Created' if created else 'Found'} parent: GRUNDFOS-WELLPUMPS")

        # Create Grundfos Pump Accessories parent
        acc_parent, created = Product.objects.get_or_create(
            part_number="GRUNDFOS-ACC",
            defaults={
                "title": "Grundfos Pump Accessories",
                "has_variants": True,
                "is_active": True,
                "visible_on_website": True,
                "brand": grundfos_brand,
                "description": (
                    "Controllers, switch boxes, float switches, pump wire, "
                    "and booster pumps for Grundfos well pump systems."
                ),
            },
        )
        self.stdout.write(f"  {'Created' if created else 'Found'} parent: GRUNDFOS-ACC")

        # Move pump variants
        prefix_to_strip = "Grundfos Pumps - "
        for i, sku in enumerate(GRUNDFOS_PUMP_SKUS):
            try:
                v = ProductVariant.objects.get(sku=sku)
                if v.product_id == pumps_parent.id:
                    self.stdout.write(f"  {sku} already under GRUNDFOS-WELLPUMPS — skipping")
                    continue
                v.product = pumps_parent
                v.sort_order = i
                if v.name.startswith(prefix_to_strip):
                    v.name = v.name[len(prefix_to_strip):]
                v.save()
                self.stdout.write(f"  Moved {sku} → GRUNDFOS-WELLPUMPS | {v.name}")
            except ProductVariant.DoesNotExist:
                self.stdout.write(self.style.ERROR(f"  Variant {sku} not found!"))

        # Move accessory variants
        for i, sku in enumerate(GRUNDFOS_ACC_SKUS):
            try:
                v = ProductVariant.objects.get(sku=sku)
                if v.product_id == acc_parent.id:
                    self.stdout.write(f"  {sku} already under GRUNDFOS-ACC — skipping")
                    continue
                v.product = acc_parent
                v.sort_order = i
                if v.name.startswith(prefix_to_strip):
                    v.name = v.name[len(prefix_to_strip):]
                v.save()
                self.stdout.write(f"  Moved {sku} → GRUNDFOS-ACC | {v.name}")
            except ProductVariant.DoesNotExist:
                self.stdout.write(self.style.ERROR(f"  Variant {sku} not found!"))

    # -- Phase 4 ---------------------------------------------------------------

    def phase_4_category_assignments(self):
        self.stdout.write(self.style.MIGRATE_HEADING("\nPhase 4: Category reassignments"))

        # Get categories
        try:
            dc_appliances = Category.objects.get(slug="dc-appliances")
        except Category.DoesNotExist:
            self.stdout.write(self.style.ERROR("  DC Appliances category not found!"))
            return

        try:
            dc_acc = Category.objects.get(slug="dc-appliance-accessories")
        except Category.DoesNotExist:
            # Fallback to snap-fan if phase 1 hasn't run
            try:
                dc_acc = Category.objects.get(slug="snap-fan")
            except Category.DoesNotExist:
                self.stdout.write(self.style.ERROR("  DC Appliance Accessories category not found!"))
                return

        # Accessories → DC Appliance Accessories
        for pn in ACCESSORY_PART_NUMBERS:
            self._assign_category(pn, dc_acc)

        # Grundfos Accessories → DC Appliance Accessories
        self._assign_category("GRUNDFOS-ACC", dc_acc)

        # DC Appliances: fans parent, solar kit, well pumps
        self._assign_category("SNAP-FAN-BLDC", dc_appliances)
        self._assign_category("GRUNDFOS-WELLPUMPS", dc_appliances)
        for pn in DC_APPLIANCES_PART_NUMBERS:
            self._assign_category(pn, dc_appliances)

    def _assign_category(self, part_number, category):
        try:
            product = Product.objects.get(part_number=part_number)
        except Product.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"  Product {part_number} not found!"))
            return

        pc, created = ProductCategory.objects.get_or_create(
            product=product,
            category=category,
            defaults={"is_primary": True},
        )
        if created:
            # Remove old category assignments for this product
            ProductCategory.objects.filter(product=product).exclude(
                category=category
            ).delete()
            self.stdout.write(f"  {part_number} → {category.title}")
        else:
            self.stdout.write(f"  {part_number} already in {category.title}")

    # -- Phase 5 ---------------------------------------------------------------

    def phase_5_update_prices(self):
        self.stdout.write(self.style.MIGRATE_HEADING("\nPhase 5: Update prices to 2026 Fogco MSRP"))

        updated = 0
        for sku, new_price in PRICE_UPDATES.items():
            try:
                v = ProductVariant.objects.get(sku=sku)
                old_price = v.retail_price
                if old_price != new_price:
                    v.retail_price = new_price
                    v.save()
                    self.stdout.write(f"  {sku}: ${old_price} → ${new_price}")
                    updated += 1
                else:
                    self.stdout.write(f"  {sku}: already ${new_price}")
            except ProductVariant.DoesNotExist:
                self.stdout.write(self.style.WARNING(f"  {sku}: variant not found (may have been renamed)"))

        self.stdout.write(f"  Updated {updated} variant prices")

    # -- Phase 6 ---------------------------------------------------------------

    def phase_6_deactivate_old(self):
        self.stdout.write(self.style.MIGRATE_HEADING("\nPhase 6: Deactivate old parent products"))

        # Old individual fan parents (now empty — variants moved to SNAP-FAN-BLDC)
        for pn in FAN_PARENT_SKUS:
            try:
                p = Product.objects.get(part_number=pn)
                if p.is_active:
                    p.is_active = False
                    p.visible_on_website = False
                    p.save()
                    self.stdout.write(f"  Deactivated: {pn} ({p.title})")
                else:
                    self.stdout.write(f"  Already inactive: {pn}")
            except Product.DoesNotExist:
                self.stdout.write(self.style.WARNING(f"  {pn} not found"))

        # Old bundle parent (SFS.P12BLDC) — already inactive but ensure
        try:
            p = Product.objects.get(part_number="SFS.P12BLDC")
            if p.is_active:
                p.is_active = False
                p.visible_on_website = False
                p.save()
                self.stdout.write(f"  Deactivated: SFS.P12BLDC ({p.title})")
            else:
                self.stdout.write("  Already inactive: SFS.P12BLDC")
        except Product.DoesNotExist:
            pass

        # Old Grundfos parent (SQF) — now empty, variants moved
        try:
            p = Product.objects.get(part_number="SQF")
            if p.is_active:
                p.is_active = False
                p.visible_on_website = False
                p.save()
                self.stdout.write(f"  Deactivated: SQF ({p.title})")
            else:
                self.stdout.write("  Already inactive: SQF")
        except Product.DoesNotExist:
            pass
