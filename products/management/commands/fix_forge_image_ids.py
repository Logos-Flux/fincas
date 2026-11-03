"""
One-time fix: prepend CF Images account hash to ProductImage.cf_image_id
records that are missing it (created by ProductForge publish before the fix).

Usage: python manage.py fix_forge_image_ids [--dry-run]
"""

from django.core.management.base import BaseCommand

from products.models import ProductImage

CF_IMAGES_HASH = "Fdrr4r8cVWsy-JJCR0JU_Q"


class Command(BaseCommand):
    help = "Fix ProductImage cf_image_id records missing the CF Images account hash prefix"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be fixed without making changes",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        # Find images that have a cf_image_id but no "/" in it (missing hash prefix)
        broken = ProductImage.objects.exclude(cf_image_id="").exclude(
            cf_image_id__contains="/"
        )
        count = broken.count()

        if count == 0:
            self.stdout.write(self.style.SUCCESS("No broken image IDs found."))
            return

        self.stdout.write(f"Found {count} ProductImage records missing hash prefix.")

        if dry_run:
            for img in broken[:10]:
                self.stdout.write(
                    f"  Would fix: {img.cf_image_id} -> {CF_IMAGES_HASH}/{img.cf_image_id}"
                )
            if count > 10:
                self.stdout.write(f"  ... and {count - 10} more")
            return

        fixed = 0
        for img in broken:
            img.cf_image_id = f"{CF_IMAGES_HASH}/{img.cf_image_id}"
            img.save(update_fields=["cf_image_id", "updated_at"])
            fixed += 1

        self.stdout.write(self.style.SUCCESS(f"Fixed {fixed} ProductImage records."))
