"""Create the dedicated ``api-service`` account for the legacy ``API_KEY``
(identity-unification §2.6, step 5).

The legacy key used to act as "the first superuser", i.e. a human admin. It
now acts as ``settings.GOLDEN_LEGACY_API_KEY_USER`` (default
``api-service``): a superuser with ``is_staff`` (so it keeps every endpoint
the old principal reached) and an unusable password (so it can never sign in
to /erp/ or the admin). Requests never create it: once deleted, it stays
deleted (the legacy key switched off) until ``manage.py ensure_legacy_api_user``.

An existing account of that name is left exactly as it is: the runtime
refuses it if it has a usable password or an external login (a person), so
nobody's password is wiped here. Reverse is a no-op: by then the account may
be the actor on audit rows.
"""

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import migrations
from django.utils import timezone


def create_api_service(apps, schema_editor):
    username = getattr(settings, "GOLDEN_LEGACY_API_KEY_USER", "api-service")
    if not username:
        return
    User = apps.get_model("auth", "User")
    if User.objects.filter(username=username).exists():
        return
    User.objects.create(
        username=username,
        email="api-service@example.com",
        is_superuser=True,
        is_staff=True,
        is_active=True,
        password=make_password(None),
        last_login=timezone.now(),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("core", "0008_apikey"),
    ]

    operations = [
        migrations.RunPython(create_api_service, migrations.RunPython.noop),
    ]
