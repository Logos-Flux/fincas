"""CRM Phase 4 step 3: GIN index on ``Party.metadata`` (Postgres only).

docs/crm/phase4-fields.md 3.4. ``jsonb_path_ops`` serves the ``@>``
containment ``core.sidecar.filter_q`` compiles to on Postgres. A database-only
operation: model state is unchanged, and on SQLite it does nothing.
"""

from django.db import migrations

from core.migration_ops import PostgresGinIndex


class Migration(migrations.Migration):

    dependencies = [
        ("parties", "0005_stamp_owner_team"),
    ]

    operations = [
        PostgresGinIndex(model_name="party", field_name="metadata", name="party_metadata_gin"),
    ]
