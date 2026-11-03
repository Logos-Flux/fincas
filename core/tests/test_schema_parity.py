"""Schema parity checker (Pin M1): diff core, manifest classification, command.

Everything except ``PostgresSchemaParityTests`` runs on hand-built snapshot
dicts and needs no database. The Postgres test is skipped on SQLite; CI's
``migrations-postgres`` job runs it with ``--settings=config.settings.dev`` and
``DATABASE_URL`` pointing at a Postgres service.
"""

import copy
import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase

from core import schema_parity as sp


def _snapshot(tables):
    return {"format": sp.SNAPSHOT_FORMAT, "schema": "public", "postgres_major": 17, "tables": tables}


def _base():
    return _snapshot(
        {
            "orders_order": {
                "app": "orders",
                "columns": {
                    "id": {"type": "uuid", "nullable": False, "default": None},
                    "total": {"type": "numeric(10,2)", "nullable": False, "default": None},
                    "note": {"type": "character varying(255)", "nullable": True, "default": None},
                },
                "constraints": {
                    "orders_order_pkey": {"type": "primary_key", "columns": ["id"], "definition": "PRIMARY KEY (id)"},
                    "orders_order_total_gte_0": {"type": "check", "expression": "CHECK ((total >= (0)::numeric))"},
                    "orders_order_customer_fk": {
                        "type": "foreign_key",
                        "columns": ["customer_id"],
                        "references_table": "customers_customer",
                        "references_columns": ["id"],
                        "on_delete": "NO ACTION",
                        "on_update": "NO ACTION",
                        "deferrable": True,
                        "initially_deferred": True,
                    },
                },
                "indexes": {
                    "orders_order_note_idx": {
                        "definition": "CREATE INDEX orders_order_note_idx ON public.orders_order USING btree (note)"
                    }
                },
            },
            "llmhub_model": {
                "app": "llmhub",
                "columns": {"id": {"type": "bigint", "nullable": False, "default": None, "identity": "by default"}},
                "constraints": {},
                "indexes": {},
            },
        }
    )


class DiffTests(SimpleTestCase):
    def test_identical_snapshots_have_no_diff(self):
        self.assertEqual(sp.diff_snapshots(_base(), _base()), [])

    def test_ordering_never_produces_a_diff(self):
        base = _base()
        shuffled = copy.deepcopy(base)
        table = shuffled["tables"]["orders_order"]
        table["columns"] = dict(reversed(list(table["columns"].items())))
        table["constraints"] = dict(reversed(list(table["constraints"].items())))
        shuffled["tables"] = dict(reversed(list(shuffled["tables"].items())))
        self.assertEqual(sp.diff_snapshots(base, shuffled), [])
        self.assertEqual(sp.dump_snapshot(base), sp.dump_snapshot(shuffled))

    def test_whole_table_add_and_remove_are_single_entries(self):
        actual = _base()
        del actual["tables"]["llmhub_model"]
        actual["tables"]["unify_link"] = {"app": "unify", "columns": {"id": {}}, "constraints": {}, "indexes": {}}
        entries = sp.diff_snapshots(_base(), actual)
        self.assertEqual(
            [(e["key"], e["change"]) for e in entries],
            [("llmhub_model", "removed"), ("unify_link", "added")],
        )

    def test_column_added_removed_and_changed(self):
        actual = _base()
        cols = actual["tables"]["orders_order"]["columns"]
        del cols["note"]
        cols["stripe_pi"] = {"type": "character varying(255)", "nullable": True, "default": None}
        cols["total"] = {"type": "numeric(14,2)", "nullable": True, "default": None}
        entries = {e["key"]: e for e in sp.diff_snapshots(_base(), actual)}
        self.assertEqual(entries["orders_order.note"]["change"], "removed")
        self.assertEqual(entries["orders_order.stripe_pi"]["change"], "added")
        changed = entries["orders_order.total"]
        self.assertEqual(changed["change"], "changed")
        self.assertEqual(
            changed["changes"],
            [
                {"field": "nullable", "baseline": False, "actual": True},
                {"field": "type", "baseline": "numeric(10,2)", "actual": "numeric(14,2)"},
            ],
        )

    def test_partial_index_predicate_counts_as_a_change(self):
        actual = _base()
        actual["tables"]["orders_order"]["indexes"]["orders_order_note_idx"]["definition"] += " WHERE (note IS NOT NULL)"
        (entry,) = sp.diff_snapshots(_base(), actual)
        self.assertEqual((entry["key"], entry["change"]), ("orders_order.index:orders_order_note_idx", "changed"))

    def test_fk_on_delete_and_check_expression_changes(self):
        actual = _base()
        cons = actual["tables"]["orders_order"]["constraints"]
        cons["orders_order_customer_fk"]["on_delete"] = "CASCADE"
        cons["orders_order_total_gte_0"]["expression"] = "CHECK ((total > (0)::numeric))"
        entries = {e["key"]: e for e in sp.diff_snapshots(_base(), actual)}
        fk = entries["orders_order.constraint:orders_order_customer_fk"]
        self.assertEqual(fk["changes"], [{"field": "on_delete", "baseline": "NO ACTION", "actual": "CASCADE"}])
        self.assertIn("orders_order.constraint:orders_order_total_gte_0", entries)

    def test_same_shape_under_a_new_name_is_a_rename(self):
        actual = _base()
        idx = actual["tables"]["orders_order"]["indexes"]
        idx["orders_order_note_9f1a_idx"] = {
            "definition": "CREATE INDEX orders_order_note_9f1a_idx ON public.orders_order USING btree (note)"
        }
        del idx["orders_order_note_idx"]
        (entry,) = sp.diff_snapshots(_base(), actual)
        self.assertEqual(entry["change"], "renamed")
        self.assertEqual(entry["key"], "orders_order.index:orders_order_note_9f1a_idx")
        self.assertEqual(entry["alt_keys"], ["orders_order.index:orders_order_note_idx"])

    def test_different_shape_under_a_new_name_is_remove_plus_add(self):
        actual = _base()
        cons = actual["tables"]["orders_order"]["constraints"]
        cons["orders_order_total_gt_0"] = {"type": "check", "expression": "CHECK ((total > (0)::numeric))"}
        del cons["orders_order_total_gte_0"]
        changes = sorted((e["name"], e["change"]) for e in sp.diff_snapshots(_base(), actual))
        self.assertEqual(changes, [("orders_order_total_gt_0", "added"), ("orders_order_total_gte_0", "removed")])

    def test_collation_change(self):
        actual = _base()
        actual["tables"]["orders_order"]["columns"]["note"]["collation"] = "C"
        (entry,) = sp.diff_snapshots(_base(), actual)
        self.assertEqual(entry["changes"], [{"field": "collation", "baseline": None, "actual": "C"}])

    def test_table_properties_change_is_a_table_entry(self):
        actual = _base()
        actual["tables"]["orders_order"]["properties"] = {"row_security": True}
        (entry,) = sp.diff_snapshots(_base(), actual)
        self.assertEqual((entry["key"], entry["kind"], entry["change"]), ("orders_order", "table", "changed"))
        self.assertIn("row_security: None -> True", sp.format_report([entry], [], "a", "b"))

    def test_triggers_and_policies_diff_with_rename_pairing(self):
        base = _base()
        base["tables"]["orders_order"]["triggers"] = {
            "trg_a": {"definition": "CREATE TRIGGER trg_a BEFORE INSERT ON public.orders_order FOR EACH ROW "
                                    "EXECUTE FUNCTION f()", "enabled": "origin"},
        }
        actual = copy.deepcopy(base)
        trg = actual["tables"]["orders_order"]["triggers"].pop("trg_a")
        trg["definition"] = trg["definition"].replace("trg_a", "trg_b")
        actual["tables"]["orders_order"]["triggers"]["trg_b"] = trg
        actual["tables"]["orders_order"]["policies"] = {
            "own_rows": {"permissive": "PERMISSIVE", "roles": ["public"], "command": "ALL", "using": "(x)",
                         "with_check": None}
        }
        entries = [(e["key"], e["change"]) for e in sp.diff_snapshots(base, actual)]
        self.assertEqual(
            entries,
            [("orders_order.trigger:trg_b", "renamed"), ("orders_order.policy:own_rows", "added")],
        )

    def test_invalid_index_is_a_change(self):
        base = _base()
        base["tables"]["orders_order"]["indexes"]["orders_order_note_idx"]["valid"] = True
        actual = copy.deepcopy(base)
        actual["tables"]["orders_order"]["indexes"]["orders_order_note_idx"]["valid"] = False
        (entry,) = sp.diff_snapshots(base, actual)
        self.assertEqual(entry["changes"], [{"field": "valid", "baseline": True, "actual": False}])

    def test_major_version_warning(self):
        other = _base()
        other["postgres_major"] = 16
        self.assertIsNone(sp.major_version_warning(_base(), _base()))
        self.assertIn("baseline 17, actual 16", sp.major_version_warning(_base(), other))

    def test_apps_filter_uses_either_sides_label(self):
        actual = _base()
        actual["tables"]["llmhub_model"]["columns"]["name"] = {"type": "text", "nullable": False, "default": None}
        actual["tables"]["orders_order"]["columns"]["x"] = {"type": "text", "nullable": True, "default": None}
        self.assertEqual([e["key"] for e in sp.diff_snapshots(_base(), actual, apps=["orders"])], ["orders_order.x"])
        # An app-label move with the same db_table is still in scope under the old label.
        actual["tables"]["orders_order"]["app"] = "shop"
        self.assertEqual([e["key"] for e in sp.diff_snapshots(_base(), actual, apps=["orders"])], ["orders_order.x"])


class ManifestTests(SimpleTestCase):
    def test_parse_expected_skips_comments_and_blanks(self):
        text = "# header\n\norders_order.stripe_pi   # (a) port\n  llmhub_model  \norders_order.stripe_pi\n"
        self.assertEqual(sp.parse_expected(text), {"orders_order.stripe_pi": None, "llmhub_model": None})

    def test_parse_expected_field_pins_merge_and_bare_key_wins(self):
        text = "t.c nullable\nt.c type\nt.d default\nt.d\nt.e collation default\n"
        self.assertEqual(
            sp.parse_expected(text), {"t.c": {"nullable", "type"}, "t.d": None, "t.e": {"collation", "default"}}
        )

    def _changed_total(self):
        actual = _base()
        actual["tables"]["orders_order"]["columns"]["total"] = {"type": "numeric(14,2)", "nullable": True, "default": None}
        return sp.diff_snapshots(_base(), actual)

    def test_pinned_field_does_not_bless_other_changes(self):
        entries, stale = sp.classify(self._changed_total(), sp.parse_expected("orders_order.total nullable\n"))
        self.assertEqual(entries[0]["status"], "unexpected")
        self.assertEqual(entries[0]["uncovered_fields"], ["type"])
        self.assertEqual(stale, [])

    def test_all_changed_fields_pinned_is_expected(self):
        manifest = sp.parse_expected("orders_order.total nullable type\n")
        entries, stale = sp.classify(self._changed_total(), manifest)
        self.assertEqual(entries[0]["status"], "expected")
        self.assertEqual(stale, [])

    def test_bare_key_covers_whole_object(self):
        entries, _ = sp.classify(self._changed_total(), sp.parse_expected("orders_order.total\n"))
        self.assertEqual(entries[0]["status"], "expected")

    def test_pinned_field_that_did_not_change_is_stale(self):
        manifest = sp.parse_expected("orders_order.total nullable type default\n")
        entries, stale = sp.classify(self._changed_total(), manifest)
        self.assertEqual(entries[0]["status"], "expected")
        self.assertEqual(stale, ["orders_order.total default"])

    def test_pinned_key_does_not_cover_add_or_remove(self):
        actual = _base()
        del actual["tables"]["orders_order"]["columns"]["note"]
        entries, stale = sp.classify(sp.diff_snapshots(_base(), actual), sp.parse_expected("orders_order.note type\n"))
        self.assertEqual(entries[0]["status"], "unexpected")
        self.assertEqual(stale, ["orders_order.note type"])

    def test_report_prints_changed_fields_of_expected_entries(self):
        entries, stale = sp.classify(self._changed_total(), sp.parse_expected("orders_order.total nullable\n"))
        report = sp.format_report(entries, stale, "a", "b")
        self.assertIn("nullable: False -> True", report)
        self.assertIn("type: 'numeric(10,2)' -> 'numeric(14,2)'  (not in manifest)", report)

    def _entries(self):
        actual = _base()
        actual["tables"]["orders_order"]["columns"]["stripe_pi"] = {"type": "text", "nullable": True, "default": None}
        del actual["tables"]["llmhub_model"]
        return sp.diff_snapshots(_base(), actual)

    def test_classify_marks_expected_and_unexpected(self):
        entries, stale = sp.classify(self._entries(), ["orders_order.stripe_pi"])
        status = {e["key"]: e["status"] for e in entries}
        self.assertEqual(status, {"llmhub_model": "unexpected", "orders_order.stripe_pi": "expected"})
        self.assertEqual(stale, [])

    def test_classify_reports_stale_entries(self):
        _, stale = sp.classify(self._entries(), ["orders_order.stripe_pi", "llmhub_model", "chat_message.model_id"])
        self.assertEqual(stale, ["chat_message.model_id"])

    def test_rename_matches_old_or_new_key(self):
        actual = _base()
        idx = actual["tables"]["orders_order"]["indexes"]
        idx["renamed_idx"] = {"definition": "CREATE INDEX renamed_idx ON public.orders_order USING btree (note)"}
        del idx["orders_order_note_idx"]
        entries = sp.diff_snapshots(_base(), actual)
        for key in ("orders_order.index:renamed_idx", "orders_order.index:orders_order_note_idx"):
            classified, stale = sp.classify(entries, [key])
            self.assertEqual(classified[0]["status"], "expected")
            self.assertEqual(stale, [])

    def test_whole_table_key_does_not_cover_its_columns(self):
        actual = _base()
        actual["tables"]["orders_order"]["columns"]["stripe_pi"] = {"type": "text", "nullable": True, "default": None}
        entries, stale = sp.classify(sp.diff_snapshots(_base(), actual), ["orders_order"])
        self.assertEqual(entries[0]["status"], "unexpected")
        self.assertEqual(stale, ["orders_order"])


class CommandOfflineTests(SimpleTestCase):
    """--compare/--against needs no database, so it runs under SQLite test settings."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _write(self, name, content):
        path = self.dir / name
        path.write_text(content if isinstance(content, str) else sp.dump_snapshot(content))
        return str(path)

    def _run(self, *args):
        out = StringIO()
        try:
            call_command("check_schema_parity", *args, stdout=out, stderr=StringIO())
        except CommandError as exc:
            return exc.returncode, out.getvalue()
        return 0, out.getvalue()

    def _pair(self):
        actual = _base()
        actual["tables"]["orders_order"]["columns"]["stripe_pi"] = {"type": "text", "nullable": True, "default": None}
        return self._write("golden.json", _base()), self._write("downstream.json", actual)

    def test_identical_snapshots_exit_zero(self):
        a = self._write("a.json", _base())
        code, out = self._run("--compare", a, "--against", a)
        self.assertEqual(code, 0)
        self.assertIn("No differences.", out)

    def test_unexpected_diff_exits_one_with_report(self):
        golden, downstream = self._pair()
        code, out = self._run("--compare", golden, "--against", downstream)
        self.assertEqual(code, 1)
        self.assertIn("orders_order\n  + column     stripe_pi  text NULL  [unexpected]", out)

    def test_expected_diff_exits_zero(self):
        golden, downstream = self._pair()
        manifest = self._write("expected.txt", "# M1 manifest\norders_order.stripe_pi\n")
        code, out = self._run("--compare", golden, "--against", downstream, "--expect-diff", manifest)
        self.assertEqual(code, 0)
        self.assertIn("[expected]", out)

    def test_stale_manifest_exits_one_unless_allowed(self):
        golden, downstream = self._pair()
        manifest = self._write("expected.txt", "orders_order.stripe_pi\nchat_message.model_id\n")
        code, out = self._run("--compare", golden, "--against", downstream, "--expect-diff", manifest)
        self.assertEqual(code, 1)
        self.assertIn("! chat_message.model_id", out)
        code, _ = self._run(
            "--compare", golden, "--against", downstream, "--expect-diff", manifest, "--allow-stale"
        )
        self.assertEqual(code, 0)

    def test_json_output(self):
        golden, downstream = self._pair()
        code, out = self._run("--compare", golden, "--against", downstream, "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["summary"], {"total": 1, "expected": 0, "unexpected": 1, "stale": 0})
        self.assertEqual(payload["entries"][0]["key"], "orders_order.stripe_pi")
        self.assertEqual(payload["entries"][0]["status"], "unexpected")

    def test_unreadable_inputs_exit_two(self):
        a = self._write("a.json", _base())
        self.assertEqual(self._run("--compare", str(self.dir / "missing.json"), "--against", a)[0], 2)
        self.assertEqual(self._run("--compare", self._write("bad.json", "{}"), "--against", a)[0], 2)
        self.assertEqual(self._run("--compare", a, "--against", a, "--expect-diff", str(self.dir / "nope"))[0], 2)

    def test_live_modes_refuse_non_postgres(self):
        if connection.vendor == "postgresql":
            self.skipTest("only meaningful on a non-Postgres database")
        with self.assertRaises(CommandError) as ctx:
            call_command("check_schema_parity", "--snapshot", str(self.dir / "s.json"), stderr=StringIO())
        self.assertEqual(ctx.exception.returncode, 2)
        self.assertIn("needs PostgreSQL", str(ctx.exception))
        a = self._write("a.json", _base())
        with self.assertRaises(CommandError):
            call_command("check_schema_parity", "--compare", a, stdout=StringIO(), stderr=StringIO())

    def test_major_version_mismatch_warns_on_stderr_and_in_json(self):
        a = self._write("a.json", _base())
        other = _base()
        other["postgres_major"] = 16
        b = self._write("b.json", other)
        out, err = StringIO(), StringIO()
        call_command("check_schema_parity", "--compare", a, "--against", b, "--json", stdout=out, stderr=err)
        self.assertIn("major versions differ", err.getvalue())
        self.assertEqual(len(json.loads(out.getvalue())["warnings"]), 1)

    def test_unknown_database_alias_exits_two(self):
        a = self._write("a.json", _base())
        code, _ = self._run("--compare", a, "--database", "nope")
        self.assertEqual(code, 2)

    def test_snapshot_rejects_compare_only_flags(self):
        code, _ = self._run("--snapshot", str(self.dir / "s.json"), "--json")
        self.assertEqual(code, 2)


class PostgresSchemaParityTests(TestCase):
    """Snapshot the migrated test database, then compare it against itself."""

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("needs DATABASE_URL pointing at Postgres (run with --settings=config.settings.dev)")

    def test_snapshot_then_compare_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snap.json"
            call_command("check_schema_parity", "--snapshot", str(path), stderr=StringIO())
            snapshot = sp.load_snapshot(path)
            self.assertIn("orders_order", snapshot["tables"])
            self.assertEqual(snapshot["tables"]["orders_order"]["app"], "orders")
            self.assertIn("orders_order_pkey", snapshot["tables"]["orders_order"]["constraints"])

            out = StringIO()
            call_command("check_schema_parity", "--compare", str(path), "--json", stdout=out)
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["entries"], [])

    def test_live_apps_filter_keeps_an_app_label_move_in_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snap.json"
            call_command("check_schema_parity", "--snapshot", str(path), stderr=StringIO())
            snapshot = sp.load_snapshot(path)
            snapshot["tables"]["orders_order"]["app"] = "shop"  # same db_table, label moved
            path.write_text(sp.dump_snapshot(snapshot))
            out = StringIO()
            call_command("check_schema_parity", "--compare", str(path), "--apps", "shop", "--json", stdout=out)
            self.assertEqual(json.loads(out.getvalue())["entries"], [])


# Each case: (label, DDL for the baseline side, DDL for the actual side, expected diff keys).
# Tables are TEMP tables in a savepoint that is rolled back, so nothing persists.
_P = "CREATE TEMP TABLE p (id int PRIMARY KEY, k int UNIQUE)"
_TRIGGER_FN = "CREATE FUNCTION pg_temp.parity_trg() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'"
_PART = [
    "CREATE TEMP TABLE pt (c int) PARTITION BY RANGE (c)",
]
_VARIANTS = [
    ("collation", ["CREATE TEMP TABLE t (c varchar(10))"], ['CREATE TEMP TABLE t (c varchar(10) COLLATE "C")'],
     ["t.c"]),
    ("column type", ["CREATE TEMP TABLE t (c int[], j json)"], ["CREATE TEMP TABLE t (c bigint[], j jsonb)"],
     ["t.c", "t.j"]),
    ("varchar length", ["CREATE TEMP TABLE t (c varchar(10))"], ["CREATE TEMP TABLE t (c varchar(20))"], ["t.c"]),
    ("fk NOT VALID",
     [_P, "CREATE TEMP TABLE t (pid int)", "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (pid) REFERENCES p(id)"],
     [_P, "CREATE TEMP TABLE t (pid int)",
      "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (pid) REFERENCES p(id) NOT VALID"],
     ["t.constraint:fk"]),
    ("fk MATCH FULL",
     ["CREATE TEMP TABLE t (a int, b int, UNIQUE (a, b))",
      "CREATE TEMP TABLE u (a int, b int, CONSTRAINT fk FOREIGN KEY (a, b) REFERENCES t(a, b))"],
     ["CREATE TEMP TABLE t (a int, b int, UNIQUE (a, b))",
      "CREATE TEMP TABLE u (a int, b int, CONSTRAINT fk FOREIGN KEY (a, b) REFERENCES t(a, b) MATCH FULL)"],
     ["u.constraint:fk"]),
    ("fk SET NULL column list",
     ["CREATE TEMP TABLE t (a int, b int, UNIQUE (a, b))",
      "CREATE TEMP TABLE u (a int, b int, CONSTRAINT fk FOREIGN KEY (a, b) REFERENCES t(a, b) ON DELETE SET NULL)"],
     ["CREATE TEMP TABLE t (a int, b int, UNIQUE (a, b))",
      "CREATE TEMP TABLE u (a int, b int, "
      "CONSTRAINT fk FOREIGN KEY (a, b) REFERENCES t(a, b) ON DELETE SET NULL (a))"],
     ["u.constraint:fk"]),
    ("fk on delete",
     [_P, "CREATE TEMP TABLE t (pid int, CONSTRAINT fk FOREIGN KEY (pid) REFERENCES p(id))"],
     [_P, "CREATE TEMP TABLE t (pid int, CONSTRAINT fk FOREIGN KEY (pid) REFERENCES p(id) ON DELETE CASCADE)"],
     ["t.constraint:fk"]),
    ("check NOT VALID",
     ["CREATE TEMP TABLE t (c int)", "ALTER TABLE t ADD CONSTRAINT ck CHECK (c > 0)"],
     ["CREATE TEMP TABLE t (c int)", "ALTER TABLE t ADD CONSTRAINT ck CHECK (c > 0) NOT VALID"],
     ["t.constraint:ck"]),
    ("unique nulls not distinct",
     ["CREATE TEMP TABLE t (c int, CONSTRAINT u UNIQUE (c))"],
     ["CREATE TEMP TABLE t (c int, CONSTRAINT u UNIQUE NULLS NOT DISTINCT (c))"],
     ["t.constraint:u"]),
    ("pk fillfactor",
     ["CREATE TEMP TABLE t (c int PRIMARY KEY)"],
     ["CREATE TEMP TABLE t (c int PRIMARY KEY WITH (fillfactor=50))"],
     ["t.constraint:t_pkey"]),
    ("partial index predicate",
     ["CREATE TEMP TABLE t (c int)", "CREATE UNIQUE INDEX i ON t (c)"],
     ["CREATE TEMP TABLE t (c int)", "CREATE UNIQUE INDEX i ON t (c) WHERE c IS NOT NULL"],
     ["t.index:i"]),
    ("index opclass",
     ["CREATE TEMP TABLE t (c text)", "CREATE INDEX i ON t (c)"],
     ["CREATE TEMP TABLE t (c text)", "CREATE INDEX i ON t (c text_pattern_ops)"],
     ["t.index:i"]),
    # ON ONLY on a partitioned table leaves the index invalid while a partition
    # lacks an attached index: the same definition text, only indisvalid differs.
    # (The partition itself is excluded from snapshots.)
    ("invalid index",
     [*_PART, "CREATE INDEX i ON ONLY pt (c)"],
     [*_PART, "CREATE TEMP TABLE pt1 PARTITION OF pt FOR VALUES FROM (0) TO (10)", "CREATE INDEX i ON ONLY pt (c)"],
     ["pt.index:i"]),
    ("trigger",
     ["CREATE TEMP TABLE t (c int)"],
     ["CREATE TEMP TABLE t (c int)", _TRIGGER_FN,
      "CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION pg_temp.parity_trg()"],
     ["t.trigger:tr"]),
    ("trigger timing",
     ["CREATE TEMP TABLE t (c int)", _TRIGGER_FN,
      "CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION pg_temp.parity_trg()"],
     ["CREATE TEMP TABLE t (c int)", _TRIGGER_FN,
      "CREATE TRIGGER tr AFTER INSERT ON t FOR EACH ROW EXECUTE FUNCTION pg_temp.parity_trg()"],
     ["t.trigger:tr"]),
    ("row level security + policy",
     ["CREATE TEMP TABLE t (c int)"],
     ["CREATE TEMP TABLE t (c int)", "ALTER TABLE t ENABLE ROW LEVEL SECURITY",
      "CREATE POLICY pol ON t USING (c > 0)"],
     ["t", "t.policy:pol"]),
]


class PostgresCaptureRegressionTests(TestCase):
    """Each variant pair must diff: guards against capture blind spots.

    The self-diff test above passes even if a property is never captured; this
    one fails the moment a captured property stops reaching the snapshot.
    """

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("needs DATABASE_URL pointing at Postgres (run with --settings=config.settings.dev)")

    def _snap(self, ddl):
        with transaction.atomic():
            with connection.cursor() as cursor:
                for statement in ddl:
                    cursor.execute(statement)
                cursor.execute("SELECT nspname FROM pg_namespace WHERE oid = pg_my_temp_schema()")
                schema = cursor.fetchone()[0]
                snapshot = sp.capture_snapshot(cursor, schema=schema)
            transaction.set_rollback(True)
        return snapshot

    def test_identical_ddl_does_not_diff(self):
        ddl = [_P, "CREATE TEMP TABLE t (pid int REFERENCES p(id), c varchar(5) CHECK (c <> ''))",
               "CREATE INDEX i ON t (c) WHERE c IS NOT NULL"]
        self.assertEqual(sp.diff_snapshots(self._snap(ddl), self._snap(ddl)), [])

    def test_variant_pairs_diff(self):
        for label, base_ddl, actual_ddl, keys in _VARIANTS:
            with self.subTest(label):
                entries = sp.diff_snapshots(self._snap(base_ddl), self._snap(actual_ddl))
                self.assertEqual(sorted(e["key"] for e in entries), sorted(keys), entries)
