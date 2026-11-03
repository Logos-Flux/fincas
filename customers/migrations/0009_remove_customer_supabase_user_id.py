"""Identity step 8, part 3: drop ``Customer.supabase_user_id``.

A portal login is ``Customer.user`` plus its ``UserIdentity`` rows since step
4; ``0008`` proved every value is captured. A separate migration from the
data step, so Postgres never alters a table with pending deferred-FK events.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("customers", "0008_identity_retire_precheck"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="customer",
            name="supabase_user_id",
        ),
    ]
