"""One subject per provider per user: ``UniqueConstraint(user, provider)``.

The resolver already enforces it under the Customer → Party → User row locks
(``core.identity.resolve.attach_identity``), but the identity backfill, a
Django shell or raw SQL write rows without those locks. The constraint holds
it for every writer.

Existing duplicates are never deleted here: which subject is the live one is
a decision for an operator (ask the provider which subjects still exist). The
migration fails before touching the schema and lists each duplicate by user
id, provider and identity ids, so the operator can resolve them and re-run.
"""

from django.db import migrations, models
from django.db.models import Count


class DuplicateIdentities(Exception):
    pass


def refuse_duplicates(apps, schema_editor):
    identities = apps.get_model("core", "UserIdentity")._base_manager.using(
        schema_editor.connection.alias)
    pairs = list(
        identities.values("user_id", "provider").annotate(n=Count("pk")).filter(n__gt=1)
        .order_by("user_id", "provider").values_list("user_id", "provider")
    )
    if not pairs:
        return
    lines = []
    for user_id, provider in pairs:
        ids = identities.filter(user_id=user_id, provider=provider).order_by(
            "created_at", "pk").values_list("pk", flat=True)
        lines.append(f"  user {user_id}, provider {provider!r}: identities "
                     + ", ".join(str(pk) for pk in ids))
    raise DuplicateIdentities(
        f"core.0012: {len(pairs)} user(s) hold more than one subject of one provider, "
        "so UniqueConstraint(user, provider) cannot be added. Nothing was changed. "
        "Keep the subject the provider still knows, delete the other UserIdentity "
        "rows, and migrate again:\n" + "\n".join(lines)
    )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0011_remove_userprofile_supabase_uid"),
    ]

    operations = [
        migrations.RunPython(refuse_duplicates, migrations.RunPython.noop, elidable=False),
        migrations.RemoveIndex(
            model_name="useridentity",
            name="core_useridentity_user_prov",
        ),
        migrations.AddConstraint(
            model_name="useridentity",
            constraint=models.UniqueConstraint(
                fields=("user", "provider"), name="core_useridentity_user_provider_uniq",
            ),
        ),
    ]
