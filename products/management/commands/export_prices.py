"""
Export current product/variant prices to CSV.

Usage:
    python manage.py export_prices [--output current_prices.csv] [--include-cost]

Exports: SKU, Product Name, Brand, Category, Retail Price, Sale Price,
         MSRP, MAP, Wholesale, Stock, Last Updated
         Optionally: primary SupplierProduct cost
"""
import csv

from django.core.management.base import BaseCommand

from core.csv_safety import csv_safe_row
from products.models import ProductVariant


class Command(BaseCommand):
    help = "Export product variant prices to CSV"

    def add_arguments(self, parser):
        parser.add_argument("--output", default="prices_export.csv", help="Output file path")
        parser.add_argument(
            "--include-cost",
            action="store_true",
            help="Include primary supplier cost (slower query)",
        )
        parser.add_argument(
            "--brand", default="", help="Filter by brand name (partial match)"
        )
        parser.add_argument(
            "--active-only",
            action="store_true",
            help="Only include active variants of visible products",
        )

    def handle(self, *args, **options):
        output = options["output"]
        include_cost = options["include_cost"]
        brand_filter = options["brand"]
        active_only = options["active_only"]

        qs = ProductVariant.objects.select_related(
            "product", "product__brand"
        ).order_by("product__brand__title", "sku")

        if brand_filter:
            qs = qs.filter(product__brand__title__icontains=brand_filter)

        if active_only:
            qs = qs.filter(is_active=True, product__visible_on_website=True)

        headers = [
            "SKU",
            "Product Name",
            "Brand",
            "Retail Price",
            "Sale Price",
            "MSRP",
            "MAP",
            "Wholesale",
            "Stock",
            "Last Updated",
        ]
        if include_cost:
            headers.insert(4, "Cost (Primary Supplier)")
            headers.insert(5, "Supplier")

        # Build bulk supplier-product lookup (single query, avoids N+1 per row)
        sp_map = {}
        if include_cost:
            from products.models import SupplierProduct

            variant_ids = list(qs.values_list("id", flat=True))
            if variant_ids:
                for sp in (
                    SupplierProduct.objects.filter(variant_id__in=variant_ids)
                    .select_related("supplier")
                    .order_by("variant_id", "-is_primary", "id")
                ):
                    if sp.variant_id not in sp_map:
                        sp_map[sp.variant_id] = sp

        with open(output, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            count = 0
            for v in qs.iterator():
                row = [
                    v.sku,
                    v.product.title if v.product else "",
                    v.product.brand.title if v.product and v.product.brand else "",
                    v.retail_price or "",
                    v.sale_price or "",
                    v.msrp_price or "",
                    v.map_price or "",
                    v.wholesale_price or "",
                    v.stock,
                    v.updated_at.strftime("%Y-%m-%d %H:%M") if v.updated_at else "",
                ]
                if include_cost:
                    primary_sp = sp_map.get(v.id)
                    row.insert(4, primary_sp.cost if primary_sp else "")
                    row.insert(5, str(primary_sp.supplier) if primary_sp else "")
                writer.writerow(csv_safe_row(row))
                count += 1

        self.stdout.write(self.style.SUCCESS(f"Exported {count} variants to {output}"))
