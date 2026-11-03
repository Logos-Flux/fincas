"""
Create "Grid Interactive Inverters" subcategory under Inverters.

Grid interactive inverters feed power to the grid but are NOT bidirectional
(hybrid inverters handle bidirectional battery charge/discharge).

Usage:
  python manage.py create_grid_interactive_inverters --dry-run
  python manage.py create_grid_interactive_inverters
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from products.models import Category

SLUG = "grid-interactive-inverters"
TITLE = "Grid Interactive Inverters"
PARENT_SLUG = "inverters"
DESCRIPTION = (
    "Grid interactive inverters connect to the utility grid and export excess "
    "solar power. Unlike hybrid inverters, they do not support bidirectional "
    "battery charge and discharge."
)
SEO_TITLE = "Grid Interactive Inverters"
SEO_META_DESCRIPTION = (
    "Shop grid interactive inverters for grid-connected solar systems. "
    "Export excess solar power to the utility grid."
)


class Command(BaseCommand):
    help = "Create Grid Interactive Inverters category under Inverters."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without writing to the database.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        dry = options["dry_run"]
        tag = "[DRY RUN] " if dry else ""

        if dry:
            transaction.set_rollback(True)

        # Ensure parent exists
        try:
            parent = Category.objects.get(slug=PARENT_SLUG)
        except Category.DoesNotExist:
            self.stderr.write(
                self.style.ERROR(f"Parent category '{PARENT_SLUG}' not found.")
            )
            return

        cat, created = Category.objects.get_or_create(
            slug=SLUG,
            defaults={
                "title": TITLE,
                "parent": parent,
                "visible_on_website": True,
                "description": DESCRIPTION,
                "seo_title": SEO_TITLE,
                "seo_meta_description": SEO_META_DESCRIPTION,
            },
        )

        if created:
            self.stdout.write(
                self.style.SUCCESS(
                    f"{tag}Created category '{cat.title}' "
                    f"(slug={cat.slug}, depth={cat.tree_depth}) "
                    f"under '{parent.title}'."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"{tag}Category '{cat.title}' already exists (id={cat.id})."
                )
            )
