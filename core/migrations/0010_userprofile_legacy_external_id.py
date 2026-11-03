"""Identity step 8, part 1: the archive column for orphan profile uids.

``UserProfile.legacy_external_id`` receives the real Supabase uid of a
profile that has no user (a historical FK target, identity-unification §7),
copied by ``customers.0008_identity_retire_precheck`` before ``0011`` drops
``supabase_uid``. Added on every deployment, empty unless that copy found
something, so the schema is the same everywhere.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0009_api_service_account"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="legacy_external_id",
            field=models.CharField(blank=True, default="", editable=False, max_length=255),
        ),
    ]
