"""CRM Phase 4 step 3: ``filter_q`` filters identically on SQLite and Postgres.

docs/crm/phase4-fields.md 3.4. ``core.sidecar.filter_q`` compiles to a JSON
key lookup on SQLite and to ``metadata @> {key: value}`` on Postgres. The same
fixtures run through whichever compiler the test database selects, against one
hard-coded table of expected matches, so a vendor that disagrees fails here.
The main CI job runs the SQLite half; "Test Suite (Postgres 17)"
(``TEST_DATABASE_URL``) runs the Postgres half, plus the index tests below.

The fixture rows carry the traps where JSON equality differs between engines:
``true`` vs ``1`` vs ``"1"``, case, accents, a scalar where a list is expected,
the same value under another key or namespace, and a value nested one level
down.
"""

from __future__ import annotations

import importlib
import os

from django.apps import apps
from django.db import connection
from django.db.migrations.state import ProjectState
from django.test import TestCase, override_settings

from core.migration_ops import JSONB_PATH_OPS, SIDECAR_GIN_INDEXES, PostgresGinIndex, create_gin_index
from core.models import Company
from core.sidecar import filter_q, set_fields
from crm import engagements
from crm.models import Engagement, Pipeline
from parties.models import Party

_FIELDS = [
    {"key": "custom.package", "label": "Package", "type": "choice", "filterable": True,
     "choices": [["accelerator", "Accelerator"], ["other", "Other"]]},
    {"key": "custom.vip", "label": "VIP", "type": "boolean", "filterable": True},
    {"key": "custom.seats", "label": "Seats", "type": "integer", "filterable": True},
    {"key": "custom.region", "label": "Region", "type": "string", "filterable": True},
    {"key": "custom.channels", "label": "Channels", "type": "multi_choice", "filterable": True,
     "choices": [["email", "Email"], ["email_bulk", "Bulk email"], ["phone", "Phone"], ["post", "Post"]]},
]
DEFINITIONS = {"party": _FIELDS, "engagement": _FIELDS}

# label -> metadata. Defined keys go through set_fields (the canonical encoding);
# provider keys (undefined, so not type-checked) are written as an import would.
ROWS = {
    "r1": {"custom.package": "accelerator", "custom.vip": True, "custom.seats": 1,
           "custom.region": "Zoë café", "custom.channels": ["email", "post"],
           "hubspot.flag": True, "hubspot.status": "Open"},
    "r2": {"custom.package": "other", "custom.vip": False, "custom.seats": 50,
           "custom.region": "zoe cafe", "custom.channels": ["email_bulk"],
           "hubspot.flag": 1, "hubspot.status": "open"},
    "r3": {"custom.vip": True, "custom.seats": 5, "custom.region": "1",
           "custom.channels": ["phone"], "hubspot.flag": "1", "hubspot.status": ["open"]},
    "r4": {"custom.region": "North ", "hubspot.package": "accelerator",
           "hubspot.flag": False, "hubspot.status": "1"},
    "r5": {},  # nothing but its system keys
}
# Raw writes golden code could make (system keys are not validated). They must
# never match a filter on a custom or provider key.
SYSTEM_EXTRAS = {"r5": {"backfill": {"custom.package": "accelerator"}, "seed": True}}

EXPECTED = [
    ("custom.package", "accelerator", {"r1"}),
    ("custom.package", "other", {"r2"}),
    ("custom.vip", True, {"r1", "r3"}),
    ("custom.vip", False, {"r2"}),
    ("custom.seats", 1, {"r1"}),
    ("custom.seats", "50", {"r2"}),
    ("custom.region", "Zoë café", {"r1"}),
    ("custom.region", "zoe cafe", {"r2"}),
    ("custom.region", "1", {"r3"}),
    ("custom.region", "North ", {"r4"}),
    ("custom.channels", "email", {"r1"}),  # not email_bulk
    ("custom.channels", "email_bulk", {"r2"}),
    ("custom.channels", "post", {"r1"}),
    ("custom.channels", "phone", {"r3"}),
    ("hubspot.flag", True, {"r1"}),  # not the integer 1 (SQLite's JSON says true == 1)
    ("hubspot.flag", 1, {"r2"}),
    ("hubspot.flag", "1", {"r3"}),
    ("hubspot.flag", False, {"r4"}),
    ("hubspot.status", "open", {"r2"}),  # not "Open", not ["open"]
    ("hubspot.status", "Open", {"r1"}),
    ("hubspot.status", "1", {"r4"}),
    ("hubspot.package", "accelerator", {"r4"}),
]


@override_settings(GOLDEN_FIELD_DEFINITIONS=DEFINITIONS)
class FilterQVendorAgreementTests(TestCase):
    """One fixture set, one expected table, whichever vendor runs it."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        pipeline = Pipeline.objects.get(company=cls.company, key="sales")
        cls.rows = {Party: {}, Engagement: {}}
        for label, values in ROWS.items():
            party = Party.objects.create(company=cls.company, kind=Party.Kind.ORGANISATION,
                                         display_name=f"Org {label}", origin=Party.Origin.ERP)
            engagement = engagements.create(company=cls.company, party=party, pipeline=pipeline,
                                            title=f"Deal {label}")
            for obj in (party, engagement):
                cls._write(obj, values, SYSTEM_EXTRAS.get(label, {}))
                cls.rows[type(obj)][obj.pk] = label

    @staticmethod
    def _write(obj, values, extras):
        custom = {k: v for k, v in values.items() if k.startswith("custom.")}
        provider = {k: v for k, v in values.items() if not k.startswith("custom.")}
        if custom:
            set_fields(obj, custom, source="api")
        if provider:
            set_fields(obj, provider, source="import:hubspot")
        if extras:
            obj.refresh_from_db(fields=["metadata"])
            type(obj).objects.filter(pk=obj.pk).update(metadata={**obj.metadata, **extras})

    def matches(self, model, key, value):
        pks = model.objects.filter(company=self.company).filter(filter_q(model, key, value))
        return {self.rows[model][pk] for pk in pks.values_list("pk", flat=True)}

    def test_every_case_matches_the_expected_rows(self):
        for model in (Party, Engagement):
            for key, value, expected in EXPECTED:
                with self.subTest(vendor=connection.vendor, model=model.__name__, key=key, value=value):
                    self.assertEqual(self.matches(model, key, value), expected)

    def test_the_fixtures_are_stored_canonically(self):
        """The traps are real: the stored JSON keeps the types apart."""
        party = Party.objects.get(company=self.company, display_name="Org r2")
        self.assertIs(party.metadata["custom.vip"], False)
        self.assertEqual(party.metadata["hubspot.flag"], 1)
        self.assertNotIsInstance(party.metadata["hubspot.flag"], bool)
        self.assertEqual(party.metadata["custom.channels"], ["email_bulk"])


class PostgresGinIndexOperationTests(TestCase):
    """The migration operation: real DDL on Postgres, nothing elsewhere."""

    class _Editor:
        """Records what the operation would execute (a SQLite schema editor
        cannot open inside a TestCase transaction)."""

        def __init__(self):
            self.connection = connection
            self.quote_name = connection.ops.quote_name
            self.collected_sql = []

        def execute(self, sql, params=()):
            self.collected_sql.append(sql)

    def collected(self, backwards=False):
        state = ProjectState.from_apps(apps)
        op = PostgresGinIndex(model_name="party", field_name="metadata", name="party_metadata_gin")
        editor = self._Editor()
        if backwards:
            op.database_backwards("parties", editor, state, state)
        else:
            op.database_forwards("parties", editor, state, state)
        return editor.collected_sql

    def test_forwards_and_backwards(self):
        if connection.vendor == "postgresql":
            self.assertEqual(self.collected(), [
                'CREATE INDEX IF NOT EXISTS "party_metadata_gin" ON "parties_party" '
                f'USING gin ("metadata" {JSONB_PATH_OPS})'
            ])
            self.assertEqual(self.collected(backwards=True), ['DROP INDEX IF EXISTS "party_metadata_gin"'])
        else:
            self.assertEqual(self.collected(), [])
            self.assertEqual(self.collected(backwards=True), [])

    def test_state_is_unchanged_and_it_deconstructs(self):
        op = PostgresGinIndex(model_name="party", field_name="metadata", name="party_metadata_gin")
        name, args, kwargs = op.deconstruct()
        self.assertEqual((name, args), ("PostgresGinIndex", []))
        self.assertEqual(kwargs, {"model_name": "party", "field_name": "metadata", "name": "party_metadata_gin"})
        self.assertIn("Postgres only", op.describe())

    def test_the_migrations_declare_the_shipped_indexes(self):
        # Imported directly: the test settings disable migration modules.
        declared = {}
        for module, label in (("parties.migrations.0006_party_metadata_gin", "parties.Party"),
                              ("crm.migrations.0017_engagement_metadata_gin", "crm.Engagement")):
            ops = importlib.import_module(module).Migration.operations
            for op in ops:
                self.assertIsInstance(op, PostgresGinIndex)
                self.assertEqual((op.field_name, op.opclass), ("metadata", JSONB_PATH_OPS))
                self.assertEqual(apps.get_model(label)._meta.model_name, op.model_name)
                declared[label] = op.name
        self.assertEqual(declared, SIDECAR_GIN_INDEXES)


@override_settings(GOLDEN_FIELD_DEFINITIONS=DEFINITIONS)
class PostgresGinIndexPlanTests(TestCase):
    """At a few thousand rows the planner serves ``filter_q`` from the index.

    ``config.settings.test`` builds the test database from the models, not the
    migrations, so the index is created here with the migration's own helper.
    """

    ROWS = 4000

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("needs Postgres: run with TEST_DATABASE_URL (CI job 'Test Suite (Postgres 17)')")

    def explain(self, model, key, value, analyze=False):
        qs = model.objects.filter(filter_q(model, key, value)).only("pk")
        sql, params = qs.query.sql_with_params()
        with connection.cursor() as cursor:
            cursor.execute(f"EXPLAIN {'(ANALYZE, COSTS) ' if analyze else ''}{sql}", params)
            return "\n".join(row[0] for row in cursor.fetchall())

    def test_party_and_engagement_filters_use_the_gin_index(self):
        company = Company.objects.create(name="Tenant A")
        pipeline = Pipeline.objects.get(company=company, key="sales")
        packages = ["other"] * 199 + ["accelerator"]  # 0.5% selective
        parties = Party.objects.bulk_create([
            Party(company=company, kind=Party.Kind.ORGANISATION, display_name=f"Org {i}",
                  origin=Party.Origin.ERP,
                  metadata={"custom.package": packages[i % 200], "custom.seats": i,
                            "hubspot.hs_object_id": str(i)})
            for i in range(self.ROWS)
        ])
        stage = pipeline.stages.order_by("sort_order").first()
        Engagement.objects.bulk_create([
            Engagement(company=company, party=p, pipeline=pipeline, stage=stage, title=p.display_name,
                       number=f"DL-{p.display_name[4:]}",
                       metadata=p.metadata)
            for p in parties
        ])
        # Built over existing rows, as the migration does on a live table: rows
        # inserted after CREATE INDEX sit in GIN's pending list until a VACUUM,
        # and the planner prices that list high. SET CONSTRAINTS fires the
        # deferred FK checks first; CREATE INDEX refuses pending trigger events.
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        with connection.schema_editor(atomic=False) as editor:
            for label, name in SIDECAR_GIN_INDEXES.items():
                create_gin_index(editor, apps.get_model(label), "metadata", name)
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE parties_party")
            cursor.execute("ANALYZE crm_engagement")

        for model, name in ((Party, "party_metadata_gin"), (Engagement, "engagement_metadata_gin")):
            plan = self.explain(model, "custom.package", "accelerator")
            with self.subTest(model=model.__name__):
                self.assertIn(f"Bitmap Index Scan on {name}", plan, plan)
                self.assertEqual(model.objects.filter(filter_q(model, "custom.package", "accelerator")).count(),
                                 self.ROWS // 200)
        if os.environ.get("SIDECAR_EXPLAIN"):  # the plans quoted in the step 3 PR
            for model in (Party, Engagement):
                print(f"\nEXPLAIN ANALYZE ({model.__name__}, {self.ROWS} rows, custom.package = accelerator):\n"
                      f"{self.explain(model, 'custom.package', 'accelerator', analyze=True)}")
