"""Identity step 8, part 4: drop ``UserProfile.supabase_uid``.

Nothing authenticated through it (identity-unification §1.4). Values on
profiles with a user live on as inert ``legacy_supabase`` / ``local``
identities; real uids on user-less profiles were archived to
``legacy_external_id`` by ``customers.0008``, which this depends on.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0010_userprofile_legacy_external_id"),
        ("customers", "0008_identity_retire_precheck"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="userprofile",
            name="supabase_uid",
        ),
    ]
