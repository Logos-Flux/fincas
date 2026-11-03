"""
Create Kara Firma product line under "Sustainable Living" category.

Products:
  1. Kara Firma Standard Pack (3.5oz / 100g) - $14.95, free shipping
  2. Kara Firma Gift Box (4-pack)            - $59.95, free shipping
  3. Kara Firma 8-Pack                       - $79.95, free shipping
  4. Kara Firma Retail Box (20-count)        - $199.00, + shipping (5.25lb, 12x12x6)
  5. Kara Firma Bulk Pack (22lb)             - $199.00, + shipping (25lb, 12x12x12)

Usage:
  python manage.py create_kara_firma_products
  python manage.py create_kara_firma_products --dry-run
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from products.models import (
    Brand,
    Category,
    Product,
    ProductCategory,
    ProductVariant,
)

PRODUCTS = [
    {
        "title": "Kara Firma Standard Pack - 3.5oz (100g)",
        "slug": "kara-firma-standard-pack",
        "part_number": "KF-STD-100G",
        "short_description": "Kara Firma 3.5oz (100g) standard pack.",
        "description": (
            "Kara Firma Standard Pack — 3.5oz (100g). "
            "Premium sustainable living product. Free shipping included."
        ),
        "item_weight": Decimal("0.22"),  # 100g ≈ 0.22 lb
        "shipping_weight": Decimal("0.35"),
        "shipping_length": Decimal("6.00"),
        "shipping_width": Decimal("4.00"),
        "shipping_height": Decimal("3.00"),
        "gmc_shipping_label": "free_shipping",
        "variant": {
            "sku": "KF-STD-100G",
            "name": "Standard Pack - 3.5oz (100g)",
            "retail_price": Decimal("14.95"),
            "stock": 100,
        },
    },
    {
        "title": "Kara Firma Gift Box - 4 Pack",
        "slug": "kara-firma-gift-box-4pack",
        "part_number": "KF-GIFT-4PK",
        "short_description": "Kara Firma gift box containing 4 standard packs.",
        "description": (
            "Kara Firma Gift Box — 4 × 3.5oz (100g) packs in a premium gift box. "
            "Perfect for gifting. Free shipping included."
        ),
        "item_weight": Decimal("1.00"),
        "shipping_weight": Decimal("1.50"),
        "shipping_length": Decimal("10.00"),
        "shipping_width": Decimal("8.00"),
        "shipping_height": Decimal("4.00"),
        "gmc_shipping_label": "free_shipping",
        "gmc_multipack": 4,
        "variant": {
            "sku": "KF-GIFT-4PK",
            "name": "Gift Box - 4 Pack",
            "retail_price": Decimal("59.95"),
            "stock": 50,
        },
    },
    {
        "title": "Kara Firma 8-Pack",
        "slug": "kara-firma-8pack",
        "part_number": "KF-8PK",
        "short_description": "Kara Firma value 8-pack.",
        "description": (
            "Kara Firma 8-Pack — 8 × 3.5oz (100g) packs. "
            "Great value for regular use. Free shipping included."
        ),
        "item_weight": Decimal("2.00"),
        "shipping_weight": Decimal("2.75"),
        "shipping_length": Decimal("12.00"),
        "shipping_width": Decimal("8.00"),
        "shipping_height": Decimal("4.00"),
        "gmc_shipping_label": "free_shipping",
        "gmc_multipack": 8,
        "variant": {
            "sku": "KF-8PK",
            "name": "8-Pack",
            "retail_price": Decimal("79.95"),
            "stock": 50,
        },
    },
    {
        "title": "Kara Firma Retail Box - 20 Count",
        "slug": "kara-firma-retail-box-20ct",
        "part_number": "KF-RETAIL-20CT",
        "short_description": "Kara Firma retail display box, 20 count. Shipping charged at checkout.",
        "description": (
            "Kara Firma Retail Box — 20 × 3.5oz (100g) packs. "
            "Ideal for retail display and resale. Shipping calculated at checkout."
        ),
        "item_weight": Decimal("4.40"),
        "shipping_weight": Decimal("5.25"),
        "shipping_length": Decimal("12.00"),
        "shipping_width": Decimal("12.00"),
        "shipping_height": Decimal("6.00"),
        "gmc_shipping_label": "standard_shipping",
        "gmc_multipack": 20,
        "minimum_order_qty": 1,
        "variant": {
            "sku": "KF-RETAIL-20CT",
            "name": "Retail Box - 20 Count",
            "retail_price": Decimal("199.00"),
            "stock": 20,
        },
    },
    {
        "title": "Kara Firma Bulk Pack - 22lb",
        "slug": "kara-firma-bulk-22lb",
        "part_number": "KF-BULK-22LB",
        "short_description": "Kara Firma 22lb bulk pack. Shipping charged at checkout.",
        "description": (
            "Kara Firma Bulk Pack — 22 lbs. "
            "For high-volume use or wholesale. Shipping calculated at checkout."
        ),
        "item_weight": Decimal("22.00"),
        "shipping_weight": Decimal("25.00"),
        "shipping_length": Decimal("12.00"),
        "shipping_width": Decimal("12.00"),
        "shipping_height": Decimal("12.00"),
        "gmc_shipping_label": "standard_shipping",
        "selling_uom": "pack",
        "minimum_order_qty": 1,
        "variant": {
            "sku": "KF-BULK-22LB",
            "name": "Bulk Pack - 22lb",
            "retail_price": Decimal("199.00"),
            "stock": 10,
        },
    },
]


class Command(BaseCommand):
    help = "Create the Kara Firma product line under Sustainable Living category."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be created without writing to the database.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing will be saved.\n"))

        # ── Brand ───────────────────────────────────────────────
        brand_defaults = {
            "visible_on_website": True,
            "description": "Kara Firma — premium sustainable living products.",
        }
        if dry_run:
            self.stdout.write("  [brand] Kara Firma")
            brand = Brand(title="Kara Firma", slug="kara-firma", **brand_defaults)
        else:
            brand, created = Brand.objects.get_or_create(
                slug="kara-firma",
                defaults={"title": "Kara Firma", **brand_defaults},
            )
            self.stdout.write(
                f"  [brand] Kara Firma — {'created' if created else 'exists'}"
            )

        # ── Category ────────────────────────────────────────────
        cat_defaults = {
            "visible_on_website": True,
            "description": "Products for sustainable, eco-conscious living.",
        }
        if dry_run:
            self.stdout.write("  [category] Sustainable Living")
            category = Category(
                title="Sustainable Living", slug="sustainable-living", **cat_defaults
            )
        else:
            category, created = Category.objects.get_or_create(
                slug="sustainable-living",
                defaults={"title": "Sustainable Living", **cat_defaults},
            )
            self.stdout.write(
                f"  [category] Sustainable Living — {'created' if created else 'exists'}"
            )

        # ── Products + Variants ─────────────────────────────────
        for spec in PRODUCTS:
            variant_spec = spec.pop("variant")

            if dry_run:
                self.stdout.write(
                    f"  [product] {spec['title']}  →  "
                    f"SKU {variant_spec['sku']}  @ ${variant_spec['retail_price']}"
                    f"  ({spec.get('shipping_weight')}lb, "
                    f"{spec.get('shipping_length')}x{spec.get('shipping_width')}x{spec.get('shipping_height')})"
                    f"  [{spec.get('gmc_shipping_label')}]"
                )
                spec["variant"] = variant_spec
                continue

            # Build defaults dict, updating existing products if re-run
            product_defaults = {
                "title": spec["title"],
                "part_number": spec.get("part_number", ""),
                "product_type": Product.ProductType.PHYSICAL,
                "is_active": True,
                "visible_on_website": True,
                "has_variants": False,
                "brand": brand,
                "description": spec.get("description", ""),
                "short_description": spec.get("short_description", ""),
                "item_weight": spec.get("item_weight"),
                "item_weight_uom": "lb",
                "shipping_weight": spec.get("shipping_weight"),
                "shipping_weight_uom": "lb",
                "shipping_length": spec.get("shipping_length"),
                "shipping_width": spec.get("shipping_width"),
                "shipping_height": spec.get("shipping_height"),
                "shipping_dimension_uom": "in",
                "ships_usps": True,
                "ships_ups": True,
                "gmc_shipping_label": spec.get("gmc_shipping_label", ""),
                "gmc_multipack": spec.get("gmc_multipack"),
                "gmc_condition": "new",
                "gmc_availability": "in_stock",
                "selling_uom": spec.get("selling_uom", "each"),
                "minimum_order_qty": spec.get("minimum_order_qty", 1),
            }

            product, p_created = Product.objects.get_or_create(
                slug=spec["slug"],
                defaults=product_defaults,
            )

            # If product already existed, update shipping dims + label
            if not p_created:
                updated_fields = []
                for field_name in (
                    "shipping_weight", "shipping_length", "shipping_width",
                    "shipping_height", "shipping_dimension_uom", "gmc_shipping_label",
                    "item_weight",
                ):
                    new_val = product_defaults.get(field_name)
                    if new_val is not None and getattr(product, field_name) != new_val:
                        setattr(product, field_name, new_val)
                        updated_fields.append(field_name)
                if updated_fields:
                    product.save(update_fields=updated_fields + ["updated_at"])
                    self.stdout.write(
                        f"  [product] {product.title} — updated: {', '.join(updated_fields)}"
                    )
                else:
                    self.stdout.write(f"  [product] {product.title} — exists (no changes)")
            else:
                self.stdout.write(f"  [product] {product.title} — created")

            # Category junction
            ProductCategory.objects.get_or_create(
                product=product,
                category=category,
                defaults={"is_primary": True},
            )

            # Single variant per product (no option dimensions)
            variant, v_created = ProductVariant.objects.get_or_create(
                sku=variant_spec["sku"],
                defaults={
                    "product": product,
                    "name": variant_spec["name"],
                    "retail_price": variant_spec["retail_price"],
                    "stock": variant_spec.get("stock", 0),
                    "stock_status": ProductVariant.StockStatus.IN_STOCK,
                    "is_active": True,
                    "visible_on_website": True,
                },
            )
            self.stdout.write(
                f"    [variant] {variant.sku} @ ${variant.retail_price} — "
                f"{'created' if v_created else 'exists'}"
            )

            spec["variant"] = variant_spec

        self.stdout.write(self.style.SUCCESS("\nDone."))
