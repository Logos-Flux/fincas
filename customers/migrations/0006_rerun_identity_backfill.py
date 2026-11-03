"""Re-run the identity backfill before the step-4 resolver goes live.

Identity unification step 4 moves portal login onto ``UserIdentity`` and
stops reading ``Customer.supabase_user_id``. Between step 3's backfill
(``0004``) and this release, the old resolver kept linking new portal
customers through ``supabase_user_id`` only, so those customers have no
identity row yet. ``backfill_identities`` is idempotent: it adds exactly the
missing rows and leaves everything else alone.

No schema change. Reverse is a no-op: ``0004``'s reverse removes the rows.
"""

from django.db import migrations

from core.identity.backfill import backfill_identities, format_report


def forwards(apps, schema_editor):
    lines = format_report(backfill_identities(apps))
    if lines:
        print()
        print("\n".join(lines))


class Migration(migrations.Migration):

    dependencies = [
        ("customers", "0005_party_links"),
        ("core", "0006_identity_schema"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
