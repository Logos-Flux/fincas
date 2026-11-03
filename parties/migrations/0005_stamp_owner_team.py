"""CRM Phase 4 step 4: stamp ``Party.owner_team`` from the owner's team.

docs/crm/phase4-fields.md 2.1 and 2.7. Rows with an owner whose profile has
a team of the party's company and no ``owner_team`` yet get that team; every
other row stays NULL. Idempotent. On an upgrade this finds nothing (no
profile has a team before ``core.0012_team``); it exists so that a re-run
or a partial rollback never leaves an owned row unstamped. The reverse is a
no-op: the column itself goes with ``0004_promoted_columns``.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    from core.teams import backfill_owner_teams

    backfill_owner_teams(apps.get_model("parties", "Party"), apps.get_model("core", "UserProfile"))


class Migration(migrations.Migration):
    dependencies = [
        ("parties", "0004_promoted_columns"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
