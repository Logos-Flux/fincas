"""
Audit MPN coverage for active variants; optionally backfill from staging data.

Brand+MPN is the T1 match key for the price scraper (F3), so coverage here
directly bounds matcher recall. "Effective MPN" = variant.mpn falling back
to product.mpn.

Usage:
    python manage.py audit_mpn_coverage                     # per-brand report
    python manage.py audit_mpn_coverage --csv mpn_gaps.csv  # emit gap list
    python manage.py audit_mpn_coverage --backfill --dry-run
    python manage.py audit_mpn_coverage --backfill

--backfill copies a staging MPN onto ProductVariant.mpn where the variant has
no MPN of its own. Staging MPNs come from the `products.staging_mpns_by_variant`
hook (core/hook_registry.py; a downstream forge add-on provides
ForgeItem.staging_mpn through the published_variant FK). Without that hook --backfill refuses and the
CSV's "Forge Staging MPN" column is empty. Saves go through the ORM (per-instance .save())
so the storefront MV refresh signals fire. --dry-run previews without writing.
"""
import csv

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Q

from core import hook_registry
from core.csv_safety import csv_safe_row
from products.models import ProductVariant

# Effective MPN: the variant's own MPN, else the parent product's.
HAS_EFFECTIVE_MPN = ~Q(mpn="") | ~Q(product__mpn="")
NO_BRAND_LABEL = "(no brand)"


class Command(BaseCommand):
    help = "Report per-brand MPN coverage for active variants; --backfill from staging data"

    def add_arguments(self, parser):
        parser.add_argument(
            "--backfill",
            action="store_true",
            help="Copy the staging MPN to ProductVariant.mpn where the variant MPN is empty",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="With --backfill: show what would change without saving",
        )
        parser.add_argument(
            "--csv",
            default="",
            metavar="PATH",
            help="Write the gap list (active variants with no effective MPN) to PATH",
        )

    def handle(self, *args, **options):
        backfill = options["backfill"]
        dry_run = options["dry_run"]
        csv_path = options["csv"]

        if dry_run and not backfill:
            self.stdout.write(self.style.WARNING("--dry-run has no effect without --backfill"))
        if backfill and hook_registry.get("products.staging_mpns_by_variant") is None:
            raise CommandError(
                "--backfill needs a staging MPN source "
                "(hook products.staging_mpns_by_variant), and none is installed."
            )

        self._print_report()

        if csv_path:
            self._write_gap_csv(csv_path)

        if backfill:
            self._run_backfill(dry_run)

    # ---- report ----

    def _active_variants(self):
        return ProductVariant.objects.filter(is_active=True)

    def _overall_coverage(self):
        """Return (total, with_mpn) across all active variants."""
        agg = self._active_variants().aggregate(
            total=Count("id"), with_mpn=Count("id", filter=HAS_EFFECTIVE_MPN)
        )
        return agg["total"], agg["with_mpn"]

    @staticmethod
    def _pct(with_mpn, total):
        return (100.0 * with_mpn / total) if total else 0.0

    def _print_report(self):
        rows = (
            self._active_variants()
            .values("product__brand__title")
            .annotate(total=Count("id"), with_mpn=Count("id", filter=HAS_EFFECTIVE_MPN))
            .order_by("product__brand__title")
        )

        header = f"{'Brand':<40} {'Active':>7} {'With MPN':>9} {'Coverage':>9}"
        rule = "-" * len(header)
        self.stdout.write(
            "MPN coverage — active variants (effective MPN = variant.mpn or product.mpn)"
        )
        self.stdout.write(header)
        self.stdout.write(rule)
        for row in rows:
            brand = row["product__brand__title"] or NO_BRAND_LABEL
            self.stdout.write(
                f"{brand:<40} {row['total']:>7} {row['with_mpn']:>9} "
                f"{self._pct(row['with_mpn'], row['total']):>8.1f}%"
            )
        total, with_mpn = self._overall_coverage()
        self.stdout.write(rule)
        self.stdout.write(
            f"{'OVERALL':<40} {total:>7} {with_mpn:>9} {self._pct(with_mpn, total):>8.1f}%"
        )

    # ---- gap list ----

    def _write_gap_csv(self, path):
        gaps = (
            self._active_variants()
            .filter(mpn="", product__mpn="")
            .select_related("product", "product__brand")
            .order_by("product__brand__title", "product__title", "sku")
        )
        staging = self._staging_mpn_by_variant()
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Brand", "Product", "SKU", "Variant Name", "Forge Staging MPN"])
            count = 0
            for v in gaps.iterator():
                brand = v.product.brand.title if v.product.brand else NO_BRAND_LABEL
                writer.writerow(csv_safe_row([brand, v.product.title, v.sku, v.name, staging.get(v.id, "")]))
                count += 1
        self.stdout.write(self.style.SUCCESS(f"Wrote {count} MPN gaps to {path}"))

    # ---- backfill ----

    @staticmethod
    def _staging_mpn_by_variant():
        """Latest staging MPN per published variant, or {} with no source installed."""
        source = hook_registry.get("products.staging_mpns_by_variant")
        return source() if source is not None else {}

    def _run_backfill(self, dry_run):
        before_total, before_with = self._overall_coverage()

        staging = self._staging_mpn_by_variant()
        candidates = (
            ProductVariant.objects.filter(mpn="", id__in=staging.keys())
            .select_related("product")
            .order_by("sku")
        )

        verb = "Would update" if dry_run else "Updating"
        updated = 0
        with transaction.atomic():
            for v in candidates:
                new_mpn = staging[v.id]
                self.stdout.write(f"  {verb} {v.sku}: mpn '' -> '{new_mpn}'")
                if not dry_run:
                    v.mpn = new_mpn
                    # Per-instance save so post_save signals queue the
                    # storefront.products MV refresh on commit.
                    v.save(update_fields=["mpn", "updated_at"])
                updated += 1

        if dry_run:
            self.stdout.write(self.style.WARNING(f"Dry run: would backfill {updated} variants"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Backfilled {updated} variants"))

        after_total, after_with = self._overall_coverage()
        self.stdout.write(
            f"Overall coverage before: {before_with}/{before_total} "
            f"({self._pct(before_with, before_total):.1f}%)"
        )
        self.stdout.write(
            f"Overall coverage after:  {after_with}/{after_total} "
            f"({self._pct(after_with, after_total):.1f}%)"
        )
