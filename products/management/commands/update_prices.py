"""
Bulk update product prices from CSV.

Usage:
    python manage.py update_prices --file prices.csv [--dry-run]

CSV format (header row required):
    sku,retail_price,sale_price,map_price
    CWT410WP,134.94,,
    CWT545WP,197.99,179.99,189.99

Notes:
    - Matches by SKU on ProductVariant
    - Blank columns are skipped (existing value preserved)
    - Cost price lives on SupplierProduct, not variant — use --update-cost
      with a supplier column to update cost
    - D1 sync signals fire automatically on save
"""
import csv
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand

from products.models import ProductVariant


class Command(BaseCommand):
    help = "Bulk update product/variant prices from CSV"

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to CSV file")
        parser.add_argument(
            "--dry-run", action="store_true", help="Show changes without saving"
        )
        parser.add_argument(
            "--update-cost",
            action="store_true",
            help="Also update SupplierProduct.cost (requires cost_price column and optionally supplier_code column)",
        )

    def handle(self, *args, **options):
        filepath = options["file"]
        dry_run = options["dry_run"]
        update_cost = options["update_cost"]
        updated = 0
        skipped = 0
        not_found = 0
        errors = 0

        # Fields that live directly on ProductVariant
        VARIANT_PRICE_FIELDS = {
            "retail_price": "retail_price",
            "sale_price": "sale_price",
            "msrp_price": "msrp_price",
            "map_price": "map_price",
            "wholesale_price": "wholesale_price",
        }

        with open(filepath, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row_num, row in enumerate(reader, start=2):
                sku = row.get("sku", "").strip()
                if not sku:
                    continue

                variant = ProductVariant.objects.filter(sku=sku).first()
                if not variant:
                    self.stderr.write(self.style.WARNING(f"  Row {row_num}: NOT FOUND — {sku}"))
                    not_found += 1
                    continue

                changes = []
                variant_changed = False
                variant_update_fields = []
                row_error = False

                # Update variant price fields
                for csv_col, model_field in VARIANT_PRICE_FIELDS.items():
                    val = row.get(csv_col, "").strip()
                    if not val:
                        continue
                    try:
                        new_val = Decimal(val)
                    except (InvalidOperation, ValueError):
                        self.stderr.write(
                            self.style.ERROR(f"  Row {row_num}: BAD VALUE {sku}.{csv_col} = '{val}'")
                        )
                        errors += 1
                        row_error = True
                        continue
                    old_val = getattr(variant, model_field, None)
                    if old_val != new_val:
                        changes.append((model_field, old_val, new_val))
                        if not dry_run:
                            setattr(variant, model_field, new_val)
                            variant_changed = True
                            variant_update_fields.append(model_field)

                # Persist variant price changes now, before cost processing.
                # Decoupled so a cost failure (invalid value, DB error) never
                # silently drops a valid price update.
                if variant_changed and not dry_run:
                    # Only persist the price columns that actually changed, so a
                    # stale in-memory instance can't silently revert unrelated
                    # fields (e.g. stock) and we don't trigger needless D1 sync.
                    variant.save(  # triggers D1 sync via post_save signal
                        update_fields=variant_update_fields + ["updated_at"]
                    )

                # Update cost on primary SupplierProduct
                if update_cost:
                    cost_val = row.get("cost_price", "").strip()
                    if cost_val:
                        try:
                            new_cost = Decimal(cost_val)
                        except (InvalidOperation, ValueError):
                            self.stderr.write(
                                self.style.ERROR(f"  Row {row_num}: BAD VALUE {sku}.cost_price = '{cost_val}'")
                            )
                            if not row_error:
                                errors += 1
                                row_error = True
                            new_cost = None

                        if new_cost is not None:
                            supplier_code = row.get("supplier_code", "").strip()
                            sp_qs = variant.supplier_products.all()
                            if supplier_code:
                                sp_qs = sp_qs.filter(supplier__supplier_code=supplier_code)
                            sp = sp_qs.filter(is_primary=True).first() or sp_qs.first()
                            if sp:
                                if sp.cost != new_cost:
                                    changes.append(("supplier_cost", sp.cost, new_cost))
                                    if not dry_run:
                                        sp.cost = new_cost
                                        sp.save(update_fields=["cost", "updated_at"])
                            else:
                                self.stderr.write(
                                    self.style.WARNING(
                                        f"  Row {row_num}: {sku} — no SupplierProduct found for cost update"
                                    )
                                )

                if changes:
                    if dry_run:
                        for field, old, new in changes:
                            self.stdout.write(f"  {sku}: {field} {old} → {new}")
                    else:
                        self.stdout.write(f"  UPDATED: {sku} ({len(changes)} fields)")
                    updated += 1
                else:
                    skipped += 1

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{prefix}Done: {updated} updated, {skipped} unchanged, "
                f"{not_found} not found, {errors} errors"
            )
        )
