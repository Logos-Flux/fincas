"""
Import new products from CSV.

Usage:
    python manage.py import_products --file new_products.csv [--brand "CW Enerji"] [--category "Solar Panels"] [--dry-run]

CSV format (header row required):
    sku,name,description,retail_price,cost_price,weight_lbs,length_in,width_in,height_in,wattage

Notes:
    - Products created with visible_on_website=False (needs review before publishing)
    - Brand and Category auto-created if they don't exist
    - Duplicate SKUs are skipped
    - D1 sync signals fire on save
    - If cost_price provided with --supplier, creates SupplierProduct
"""
import csv
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify

from products.models import (
    Brand,
    Category,
    Product,
    ProductCategory,
    ProductVariant,
    SupplierProduct,
)


class Command(BaseCommand):
    help = "Import new products from CSV"

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to CSV file")
        parser.add_argument("--brand", default="", help="Default brand name")
        parser.add_argument("--category", default="", help="Default category name")
        parser.add_argument(
            "--supplier",
            default="",
            help="Supplier code for cost price (creates SupplierProduct)",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Show what would be created"
        )

    def handle(self, *args, **options):
        filepath = options["file"]
        dry_run = options["dry_run"]
        default_brand_name = options["brand"]
        default_category_name = options["category"]
        supplier_code = options["supplier"]
        created = 0
        skipped = 0
        errors = 0

        # Resolve default brand/category as a READ-ONLY lookup here. Actual
        # creation (when missing) happens lazily INSIDE each row's
        # transaction.atomic() block via get_or_create — so a default
        # brand/category never persists as an orphan when the only rows that
        # would have referenced it roll back (or when every row is skipped).
        brand = None
        if default_brand_name:
            brand = Brand.objects.filter(title=default_brand_name).first()
            if not brand and dry_run:
                self.stdout.write(f"  WOULD CREATE brand: {default_brand_name}")

        category = None
        if default_category_name:
            category = Category.objects.filter(title=default_category_name).first()
            if not category and dry_run:
                self.stdout.write(f"  WOULD CREATE category: {default_category_name}")

        # Resolve supplier (always read-only)
        supplier = None
        if supplier_code:
            from purchasing.models import Supplier

            supplier = Supplier.objects.filter(supplier_code=supplier_code).first()
            if not supplier:
                self.stderr.write(
                    self.style.WARNING(f"  Supplier '{supplier_code}' not found — cost will be skipped")
                )

        def _dec(row, key, default=0):
            val = row.get(key, "").strip()
            if not val:
                return Decimal(str(default))
            try:
                return Decimal(val)
            except (InvalidOperation, ValueError):
                return Decimal(str(default))

        with open(filepath, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row_num, row in enumerate(reader, start=2):
                sku = row.get("sku", "").strip()
                name = row.get("name", "").strip()
                if not sku or not name:
                    self.stderr.write(
                        self.style.WARNING(f"  Row {row_num}: SKIP — missing sku or name")
                    )
                    skipped += 1
                    continue

                # Check for duplicate
                if ProductVariant.objects.filter(sku=sku).exists():
                    self.stderr.write(f"  Row {row_num}: SKIP (exists) — {sku}")
                    skipped += 1
                    continue

                if dry_run:
                    retail = row.get("retail_price", "?")
                    parts = [f"WOULD CREATE: {sku} — {name} @ ${retail}"]

                    # Per-row brand override preview (read-only)
                    row_brand_name = row.get("brand", "").strip() or default_brand_name
                    if row_brand_name:
                        found = Brand.objects.filter(title=row_brand_name).first()
                        if found:
                            parts.append(f"brand={row_brand_name}")
                        else:
                            parts.append(f"WOULD CREATE Brand: {row_brand_name}")

                    # Per-row category override preview (read-only)
                    row_cat_name = row.get("category", "").strip() or default_category_name
                    if row_cat_name:
                        found = Category.objects.filter(title=row_cat_name).first()
                        if found:
                            parts.append(f"category={row_cat_name}")
                        else:
                            parts.append(f"WOULD CREATE Category: {row_cat_name}")

                    # SupplierProduct preview
                    cost_val = row.get("cost_price", "").strip()
                    if cost_val and supplier:
                        try:
                            cost = Decimal(cost_val)
                            parts.append(f"WOULD CREATE SupplierProduct cost={cost} ({supplier.supplier_code})")
                        except (InvalidOperation, ValueError):
                            parts.append(f"INVALID cost_price={cost_val}")
                    elif cost_val and supplier_code and not supplier:
                        parts.append(f"cost_price={cost_val} (supplier '{supplier_code}' not found, skipped)")

                    self.stdout.write("  " + " | ".join(parts))
                    created += 1
                    continue

                # Generate unique slug
                slug = slugify(name)
                if not slug:
                    slug = slugify(sku)
                suffix = 1
                base_slug = slug
                while Product.objects.filter(slug=slug).exists():
                    slug = f"{base_slug}-{suffix}"
                    suffix += 1

                try:
                    with transaction.atomic():
                        # Per-row brand/category resolution lives INSIDE the
                        # atomic block so a get_or_create'd Brand/Category rolls
                        # back together with the Product/Variant on row failure
                        # (no orphaned lookup rows). Dry-run never reaches here —
                        # it does read-only previews above and continues.
                        row_brand = brand
                        row_brand_name = row.get("brand", "").strip() or default_brand_name
                        if row_brand_name:
                            row_brand, _ = Brand.objects.get_or_create(
                                title=row_brand_name,
                                defaults={"slug": slugify(row_brand_name)},
                            )

                        row_category = category
                        row_category_name = (
                            row.get("category", "").strip() or default_category_name
                        )
                        if row_category_name:
                            row_category, _ = Category.objects.get_or_create(
                                title=row_category_name,
                                defaults={"slug": slugify(row_category_name)},
                            )

                        product = Product.objects.create(
                            title=name,
                            slug=slug,
                            part_number=sku,
                            brand=row_brand,
                            description=row.get("description", ""),
                            visible_on_website=False,
                        )

                        if row_category:
                            ProductCategory.objects.create(
                                product=product, category=row_category, is_primary=True
                            )

                        variant = ProductVariant.objects.create(
                            product=product,
                            sku=sku,
                            name=name,
                            retail_price=_dec(row, "retail_price"),
                            sale_price=_dec(row, "sale_price") or None,
                            msrp_price=_dec(row, "msrp_price") or None,
                            map_price=_dec(row, "map_price") or None,
                            item_weight=_dec(row, "weight_lbs") or None,
                            item_weight_uom="lb" if row.get("weight_lbs", "").strip() else "",
                            item_length=_dec(row, "length_in") or None,
                            item_width=_dec(row, "width_in") or None,
                            item_height=_dec(row, "height_in") or None,
                            item_dimension_uom="in"
                            if row.get("length_in", "").strip()
                            else "",
                        )

                        # Create SupplierProduct if cost and supplier available
                        cost_val = row.get("cost_price", "").strip()
                        if cost_val and supplier:
                            try:
                                cost = Decimal(cost_val)
                                SupplierProduct.objects.create(
                                    variant=variant,
                                    supplier=supplier,
                                    supplier_sku=row.get("supplier_sku", sku),
                                    cost=cost,
                                    is_primary=True,
                                )
                            except (InvalidOperation, ValueError):
                                pass

                    self.stdout.write(self.style.SUCCESS(f"  CREATED: {sku} — {name}"))
                    created += 1

                except Exception as e:
                    self.stderr.write(self.style.ERROR(f"  Row {row_num}: ERROR — {sku}: {e}"))
                    errors += 1

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{prefix}Done: {created} created, {skipped} skipped, {errors} errors"
            )
        )
