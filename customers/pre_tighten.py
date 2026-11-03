"""Test support: run code on the schema as it was before step 11 (D13).

The step-5 backfill, the step-1 ``ActivityLog.company`` backfill and
``reconcile_parties`` exist to repair rows that the tightened schema can no
longer hold (a customer with no party, an activity with no company). Their
tests therefore relax the schema first, by unapplying the real tightening
migrations (``customers.0007_tighten``, ``crm.0014_tighten``), and tighten it
again afterwards, which runs the real pre-check (the tables must be clean by
then). Works whether the test database was built from migrations (Postgres,
``config.settings.dev``) or synced from the models (``config.settings.test``).

Not imported by application code.
"""

from __future__ import annotations

from django.db import connection, migrations
from django.db.migrations.loader import MigrationLoader
from django.test import override_settings

TIGHTEN = (("customers", "0007_tighten"), ("crm", "0014_tighten"))

_cache: dict = {}


def _revert(state, migration, historical) -> None:
    """Undo ``migration``'s schema operations on ``state``, a later state.

    ``historical`` is the state just before ``migration``; altered fields are
    copied back from it. Everything else stays as the later migrations left
    it, so a column added or dropped after the tightening is not resurrected
    when SQLite rebuilds the table.
    """
    for op in migration.operations:
        if isinstance(op, migrations.RunPython):
            continue
        key = (migration.app_label, op.model_name_lower)
        model = state.models[key]
        if isinstance(op, migrations.AlterField):
            model.fields[op.name] = historical.models[key].fields[op.name].clone()
        elif isinstance(op, migrations.AddConstraint):
            model.options["constraints"] = [
                c for c in model.options.get("constraints", []) if c.name != op.constraint.name
            ]
        else:
            raise RuntimeError(f"pre_tighten: cannot revert {type(op).__name__} in {migration}; update this helper")


def _plan():
    """``[(migration, state before it)]`` in apply order, rendered once."""
    if "plan" not in _cache:
        # config.settings.test disables migrations; the loader needs the files.
        with override_settings(MIGRATION_MODULES={}):
            loader = MigrationLoader(None, ignore_no_migrations=True)
        # Start from the latest state (what the test database holds) and undo
        # only the tightening, newest first; a historical pre-0007 state would
        # carry columns that later migrations dropped.
        state = loader.project_state()
        state.__dict__.pop("apps", None)  # edited below; render afterwards
        befores = {}
        for key in reversed(TIGHTEN):
            _revert(state, loader.get_migration(*key), loader.project_state(key, at_end=False))
            befores[key] = state.clone()
        plan = []
        for key in TIGHTEN:
            before = befores[key]
            before.apps  # noqa: B018 - render once; clones reuse it
            plan.append((loader.get_migration(*key), before))
        _cache["plan"] = plan
    return _cache["plan"]


def before_state():
    """The project state just before the tightening (for historical models)."""
    return _plan()[0][1].clone()


def relax() -> None:
    """Unapply the tightening: NULLs and case-duplicate emails fit again."""
    for migration, state in reversed(_plan()):
        with connection.schema_editor(atomic=migration.atomic) as editor:
            migration.unapply(state.clone(), editor)


def tighten() -> None:
    """Re-apply the tightening (its pre-check runs first and may refuse)."""
    for migration, state in _plan():
        with connection.schema_editor(atomic=migration.atomic) as editor:
            migration.apply(state.clone(), editor)


def _restore() -> None:
    """Re-apply whatever of the tightening is unapplied (after a partial relax too)."""
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, "customers_customer")
    customers_tight = "uniq_customer_email_ci_company" in constraints
    for migration, state in _plan():
        if migration.app_label == "customers" and customers_tight:
            continue
        if migration.app_label == "crm" and _crm_tight():
            continue
        with connection.schema_editor(atomic=migration.atomic) as editor:
            migration.apply(state.clone(), editor)


def _crm_tight() -> bool:
    with connection.cursor() as cursor:
        columns = connection.introspection.get_table_description(cursor, "crm_activitylog")
    return not next(c for c in columns if c.name == "company_id").null_ok


class PreTightenSchemaMixin:
    """For a ``TestCase``: the whole class runs on the pre-step-11 schema.

    The schema is changed outside the class's transaction (SQLite cannot
    alter tables inside one) and restored after it rolls back.
    """

    @classmethod
    def setUpClass(cls):
        try:
            relax()
            super().setUpClass()
        except Exception:
            _restore()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            _restore()
