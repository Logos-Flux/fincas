"""Migration operations that only mean something on Postgres.

``PostgresGinIndex`` adds a GIN index on a ``JSONField`` (CRM Phase 4 3.4: the
sidecar ``metadata`` of Party and Engagement, ``jsonb_path_ops``). It is a
database-only operation: model state is unchanged, so ``makemigrations`` stays
clean on every vendor, and on SQLite (tests, local dev) it does nothing.

Because the index is not in model state, a database built from the models
instead of the migrations (``config.settings.test`` disables migrations) does
not have it. A test that needs it calls :func:`create_gin_index` itself.
"""

from django.db.migrations.operations.base import Operation

# The opclass the spec picks: smaller than the default ``jsonb_ops`` and serves
# exactly the ``@>`` containment that ``core.sidecar.filter_q`` compiles to.
JSONB_PATH_OPS = "jsonb_path_ops"

# The sidecar GIN indexes golden ships (3.4), by model label. Task gets one when
# a Task definition is first marked ``filterable``.
SIDECAR_GIN_INDEXES = {
    "parties.Party": "party_metadata_gin",
    "crm.Engagement": "engagement_metadata_gin",
}


def gin_index_sql(schema_editor, table, column, name, opclass=JSONB_PATH_OPS):
    q = schema_editor.quote_name
    return f"CREATE INDEX IF NOT EXISTS {q(name)} ON {q(table)} USING gin ({q(column)} {opclass})"


def create_gin_index(schema_editor, model, field_name, name, opclass=JSONB_PATH_OPS):
    """Create the index on Postgres; no-op elsewhere. Idempotent."""
    if schema_editor.connection.vendor != "postgresql":
        return
    column = model._meta.get_field(field_name).column
    schema_editor.execute(gin_index_sql(schema_editor, model._meta.db_table, column, name, opclass))


def drop_gin_index(schema_editor, name):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(f"DROP INDEX IF EXISTS {schema_editor.quote_name(name)}")


class PostgresGinIndex(Operation):
    """``CREATE INDEX … USING gin (<field> <opclass>)`` on Postgres only."""

    reversible = True
    reduces_to_sql = True

    def __init__(self, model_name, field_name, name, opclass=JSONB_PATH_OPS):
        self.model_name = model_name
        self.field_name = field_name
        self.name = name
        self.opclass = opclass

    def deconstruct(self):
        kwargs = {"model_name": self.model_name, "field_name": self.field_name, "name": self.name}
        if self.opclass != JSONB_PATH_OPS:
            kwargs["opclass"] = self.opclass
        return (self.__class__.__qualname__, [], kwargs)

    def state_forwards(self, app_label, state):
        pass  # database-only: the index is not part of model state

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        if self.allow_migrate_model(schema_editor.connection.alias, model):
            create_gin_index(schema_editor, model, self.field_name, self.name, self.opclass)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        model = from_state.apps.get_model(app_label, self.model_name)
        if self.allow_migrate_model(schema_editor.connection.alias, model):
            drop_gin_index(schema_editor, self.name)

    def describe(self):
        return f"Create GIN index {self.name} ({self.opclass}) on {self.model_name}.{self.field_name} (Postgres only)"

    @property
    def migration_name_fragment(self):
        return self.name.lower()
