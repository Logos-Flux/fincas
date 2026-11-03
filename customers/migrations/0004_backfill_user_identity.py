"""Identity backfill (identity-unification §3.3, step 3). Reversible, idempotent.

The logic lives in ``core.identity.backfill`` (historical-model safe) so
tests and a later step can call it. Counts are printed only when the
database had something to backfill.
"""

from django.db import migrations

from core.identity.backfill import backfill_identities, format_report, reverse_backfill


def forwards(apps, schema_editor):
    lines = format_report(backfill_identities(apps))
    if lines:
        print()
        print("\n".join(lines))


def backwards(apps, schema_editor):
    reverse_backfill(apps)


class Migration(migrations.Migration):

    dependencies = [
        ("customers", "0003_customer_user"),
        ("core", "0006_identity_schema"),
        # User.last_login is nullable from auth.0005 on; depending on the
        # latest auth migration puts it in this migration's model state.
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
