"""Identity step 8, part 2: capture, archive and verify before the drop.

``core.identity.retire.retire_legacy_columns`` re-runs the idempotent
identity backfill, archives user-less profile uids into
``UserProfile.legacy_external_id``, and refuses (the migration fails, nothing
is dropped) when a ``Customer.supabase_user_id`` or a linked profile's
``supabase_uid`` still has no ``UserIdentity`` row. The error lists primary
keys only. ``0009`` and ``core.0011`` drop the columns after it.

Reverse runs once both columns are back and refills them from the identity
table and the archive (best effort).
"""

from django.db import migrations

from core.identity.retire import (
    format_retire_report,
    restore_legacy_columns,
    retire_legacy_columns,
)


def forwards(apps, schema_editor):
    lines = format_retire_report(retire_legacy_columns(apps))
    if lines:
        print()
        print("\n".join(lines))


def backwards(apps, schema_editor):
    restore_legacy_columns(apps)


class Migration(migrations.Migration):

    dependencies = [
        ("customers", "0007_tighten"),
        ("core", "0010_userprofile_legacy_external_id"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
