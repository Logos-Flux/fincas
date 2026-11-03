"""Provision the least-privilege storefront service identity (F-037).

Idempotent data migration: creates/repairs the ``storefront`` group and
``storefront-api`` user (see core/storefront.py). Best-effort by design —
on a fresh ``migrate`` the Permission rows for other apps may not exist
yet when this runs (they are created by the post-migrate signal), so
missing codenames are skipped here and picked up by the post_migrate
receiver (core.storefront.ensure_storefront_identity_after_migrate) at the
end of the same migrate. Re-running converges to the same state.
"""

from django.db import migrations

from core.storefront import ensure_storefront_identity


def _provision(apps, schema_editor):
    ensure_storefront_identity(apps)  # historical models only


def _noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
        ("auth", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(_provision, _noop, elidable=True),
    ]
