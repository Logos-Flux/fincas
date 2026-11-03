"""Field inventory format, validator and count filler (CRM Phase 4 step 1,
docs/crm/phase4-fields.md 1.2-1.4)."""

from __future__ import annotations

import csv
import io
import json
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from crm.fields import counts
from crm.fields.inventory import COLUMNS, provider_key, validate_file, validate_text

STANDARD_DIR = Path(settings.BASE_DIR) / "docs" / "crm" / "fields"


def row(**cells):
    base = {
        "provider": "hubspot", "object": "deal", "source_id": "package",
        "source_label": "Package", "source_type": "enumeration", "options": "",
        "populated": "", "total": "", "filtered": "unknown", "sorted": "unknown",
        "reported": "unknown", "evidence": "", "pii": "no", "decision": "sidecar",
        "target": "custom.package", "notes": "",
    }
    base.update(cells)
    return base


def to_csv(*rows, columns=COLUMNS):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), lineterminator="\n",
                            extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def codes(text, **kwargs):
    return [e.code for e in validate_text(text, **kwargs).errors]


class ValidRowsTests(SimpleTestCase):
    def test_every_decision_with_a_well_formed_target(self):
        text = to_csv(
            row(source_id="email", decision="map", target="PartyEmail.email", pii="yes"),
            row(source_id="mobile", decision="map", target="PartyPhone(line_type=mobile)", pii="yes"),
            row(source_id="optout", decision="map", target="ConsentRecord(withdrawn) + Suppression"),
            row(source_id="team", decision="promote", target="Engagement.owner_team"),
            row(source_id="package"),
            row(source_id="next_step", decision="sidecar", target="hubspot.hs_next_step"),
            row(source_id="counter", decision="drop", target="", populated="0", total="10"),
            row(source_id="ticket_x", decision="defer", target="Phase 5: partner relationships"),
            row(source_id="ab", decision="defer", target="Phase 4b"),
        )
        result = validate_text(text)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.rows), 9)

    def test_columns_in_any_order_and_extra_columns_are_accepted(self):
        columns = ["comment", *reversed(COLUMNS)]
        self.assertEqual(codes(to_csv(row(), columns=columns)), [])

    def test_blank_lines_are_skipped_and_header_only_is_valid(self):
        self.assertEqual(codes(to_csv() + "\n\n"), [])
        self.assertEqual(validate_text(to_csv()).rows, [])

    def test_provider_key_default(self):
        self.assertEqual(provider_key("hubspot", "Deal Type-2"), "hubspot.deal_type_2")


class RefusalTests(SimpleTestCase):
    def test_missing_column(self):
        columns = [c for c in COLUMNS if c != "decision"]
        result = validate_text(to_csv(row(), columns=columns))
        self.assertEqual([e.code for e in result.errors], ["missing_column"])
        self.assertIn("decision", result.errors[0].message)

    def test_empty_file(self):
        self.assertEqual(codes(""), ["missing_column"])

    def test_duplicate_column(self):
        text = ",".join([*COLUMNS, "pii"]) + "\n"
        self.assertEqual(codes(text), ["duplicate_column"])

    def test_duplicate_provider_object_source_id(self):
        result = validate_text(to_csv(row(), row(source_label="Again")))
        self.assertEqual([e.code for e in result.errors], ["duplicate_row"])
        self.assertEqual(result.errors[0].line, 3)
        self.assertIn("line 2", result.errors[0].message)

    def test_same_source_id_on_another_object_is_not_a_duplicate(self):
        self.assertEqual(codes(to_csv(row(), row(object="contact"))), [])

    def test_decision_outside_the_five(self):
        for bad in ("", "keep", "MAP"):
            with self.subTest(decision=bad):
                self.assertEqual(codes(to_csv(row(decision=bad))), ["bad_decision"])

    def test_map_target_must_parse_and_exist(self):
        cases = {
            "": "bad_target",
            "party.email": "bad_target",
            "Party.given_name; Party.family_name": "bad_target",
            "Nope.field": "unknown_target",
            "Party.no_such_field": "unknown_target",
        }
        for target, code in cases.items():
            with self.subTest(target=target):
                self.assertEqual(codes(to_csv(row(decision="map", target=target))), [code])

    def test_model_checks_can_be_skipped(self):
        text = to_csv(row(decision="map", target="Nope.field"))
        self.assertEqual(codes(text, check_models=False), [])

    def test_promote_target_must_be_model_dot_field(self):
        for target, code in (("", "bad_target"), ("Party", "bad_target"),
                             ("custom.package", "bad_target"), ("Nope.col", "unknown_target")):
            with self.subTest(target=target):
                self.assertEqual(codes(to_csv(row(decision="promote", target=target))), [code])

    def test_sidecar_key_rule(self):
        cases = {
            "package": "bad_sidecar_key",  # undotted = a system key, golden's own
            "custom.Package": "bad_sidecar_key",
            "custom.": "bad_sidecar_key",
            "custom." + "x" * 65: "bad_sidecar_key",
            "klaviyo.package": "bad_sidecar_key",  # another provider's namespace
            "custom.a.b": "bad_sidecar_key",
        }
        for target, code in cases.items():
            with self.subTest(target=target):
                self.assertEqual(codes(to_csv(row(target=target))), [code])

    def test_pii_with_a_provider_key_is_refused(self):
        self.assertEqual(codes(to_csv(row(pii="yes", target="hubspot.package"))),
                         ["pii_provider_key"])
        # ...but a custom key (which needs a pii FieldDefinition) is fine.
        self.assertEqual(codes(to_csv(row(pii="yes", target="custom.package"))), [])

    def test_personal_data_in_a_key_name(self):
        for target in ("custom.ann@example_com", "hubspot.call_5551234567", "custom.x1234567"):
            with self.subTest(target=target):
                self.assertEqual(codes(to_csv(row(target=target))), ["personal_key"])
        self.assertEqual(codes(to_csv(row(target="custom.x123456"))), [])  # six digits
        self.assertEqual(codes(to_csv(row(decision="promote", target="Party.c1234567"))),
                         ["personal_key"])

    def test_drop_has_no_target_and_a_populated_drop_needs_a_reason(self):
        self.assertEqual(codes(to_csv(row(decision="drop", target="hubspot.x"))), ["bad_target"])
        self.assertEqual(codes(to_csv(row(decision="drop", target="", populated="12", total="40"))),
                         ["drop_needs_reason"])
        self.assertEqual(codes(to_csv(row(decision="drop", target="", populated="12", total="40",
                                          notes="derived counter"))), [])

    def test_defer_names_a_phase(self):
        for target in ("", "later", "phase5", "Phase five"):
            with self.subTest(target=target):
                self.assertEqual(codes(to_csv(row(decision="defer", target=target))), ["bad_target"])

    def test_malformed_cells(self):
        cases = [
            (row(provider="HubSpot"), "bad_provider"),
            (row(provider="custom"), "bad_provider"),
            (row(object="Deal"), "bad_object"),
            (row(source_id=""), "missing_source_id"),
            (row(populated="12.5"), "bad_count"),
            (row(total="-1"), "bad_count"),
            (row(populated="41", total="40"), "bad_count"),
            (row(filtered="maybe"), "bad_usage"),
            (row(sorted=""), "bad_usage"),
            (row(pii="unknown"), "bad_pii"),
            (row(options="a=1||b=2"), "bad_options"),
        ]
        for cells, code in cases:
            with self.subTest(code=code, cells={k: v for k, v in cells.items() if v != row()[k]}):
                self.assertEqual(codes(to_csv(cells)), [code])

    def test_two_properties_on_one_sidecar_key(self):
        result = validate_text(to_csv(
            row(source_id="hs-foo", target="hubspot.hs_foo"),
            row(source_id="hs_foo", target="hubspot.hs_foo"),
            row(object="contact", source_id="hs_foo", target="hubspot.hs_foo"),  # other object
        ))
        self.assertEqual([(e.line, e.code) for e in result.errors], [(3, "duplicate_target")])

    def test_bom_is_ignored(self):
        self.assertEqual(codes("\ufeff" + to_csv(row())), [])

    def test_too_many_cells(self):
        text = to_csv() + ",".join(row().values()) + ",surplus\n"
        self.assertEqual(codes(text), ["bad_row"])

    def test_every_error_is_reported_with_its_line(self):
        text = to_csv(row(), row(source_id="b", decision="nope"),
                      row(source_id="c", pii="?", target="custom.c"))
        errors = validate_text(text).errors
        self.assertEqual([(e.line, e.code) for e in errors], [(3, "bad_decision"), (4, "bad_pii")])
        self.assertIn("line 4, pii", str(errors[1]))


class StandardFilesTests(SimpleTestCase):
    def test_the_hubspot_standard_file_validates(self):
        result = validate_file(STANDARD_DIR / "hubspot-standard.csv")
        self.assertEqual([str(e) for e in result.errors], [])
        objects = {r["object"] for r in result.rows}
        self.assertEqual(objects, {"contact", "company", "deal", "ticket"})
        self.assertTrue(all(r["provider"] == "hubspot" for r in result.rows))
        # Standard properties only: no tenant counts, every usage unknown.
        for r in result.rows:
            self.assertEqual((r["populated"], r["total"]), ("", ""), r["source_id"])
        # Tickets are partner relationships: every row is deferred (1.5).
        self.assertEqual({r["decision"] for r in result.rows if r["object"] == "ticket"}, {"defer"})

    def test_the_stubs_exist_and_validate(self):
        for provider in ("clickup", "fathom", "aircall", "google_ads", "klaviyo"):
            with self.subTest(provider=provider):
                self.assertTrue(validate_file(STANDARD_DIR / f"{provider}-standard.csv").ok)

    def test_command_checks_golden_files_by_default(self):
        out = io.StringIO()
        call_command("check_field_inventory", stdout=out, stderr=io.StringIO())
        self.assertIn("hubspot-standard.csv: ", out.getvalue())
        self.assertNotIn("error", out.getvalue())


class CheckCommandTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_sample_inventory_passes(self):
        path = self.dir / "hubspot.csv"
        path.write_text(to_csv(row(), row(source_id="ip_status", target="custom.ip_status",
                                          populated="4012", total="6200", filtered="yes",
                                          evidence="view:123")))
        out = io.StringIO()
        call_command("check_field_inventory", str(path), stdout=out, stderr=io.StringIO())
        self.assertIn("2 row(s): sidecar 2 — ok", out.getvalue())

    def test_a_malformed_inventory_fails_with_located_errors(self):
        good = self.dir / "a-good.csv"
        good.write_text(to_csv(row()))
        bad = self.dir / "b-bad.csv"
        bad.write_text(to_csv(row(), row(), row(source_id="x", pii="yes", target="hubspot.x")))
        err = io.StringIO()
        with self.assertRaisesMessage(CommandError, "1 of 2 inventory file(s) failed"):
            call_command("check_field_inventory", str(self.dir), stdout=io.StringIO(), stderr=err)
        lines = err.getvalue().splitlines()
        self.assertIn(f"{bad}:3: [duplicate_row]", lines[0])
        self.assertIn(f"{bad}:4: [pii_provider_key]", lines[1])
        self.assertNotIn(str(good), err.getvalue())

    def test_a_non_utf8_file_is_reported_not_crashed(self):
        bad = self.dir / "excel.csv"
        bad.write_bytes(to_csv(row(notes="caf\u00e9")).encode("cp1252"))
        err = io.StringIO()
        with self.assertRaises(CommandError):
            call_command("check_field_inventory", str(bad), stdout=io.StringIO(), stderr=err)
        self.assertIn(":1: [bad_encoding]", err.getvalue())

    def test_a_missing_path_is_an_error(self):
        with self.assertRaisesMessage(CommandError, "no such file"):
            call_command("check_field_inventory", str(self.dir / "nope.csv"))

    def test_an_empty_directory_is_an_error(self):
        with self.assertRaisesMessage(CommandError, "no inventory CSVs"):
            call_command("check_field_inventory", str(self.dir))


class FieldInventoryCommandTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.defs = self.dir / "defs.json"
        self.defs.write_text(json.dumps({"results": [
            {"name": "package", "label": "Package", "type": "enumeration",
             "options": [{"value": "accelerator", "label": "Accelerator"},
                         {"value": "other", "label": "Other"},
                         {"value": "unused", "label": "Unused"}]},
            {"name": "budget", "label": "Budget", "type": "string"},
            {"name": "services", "label": "Services", "type": "enumeration", "options": []},
            {"name": "tags", "label": "Tags", "type": "enumeration",
             "options": [{"value": "a"}, {"value": "b"}]},
            {"name": "not_exported", "label": "Not exported", "type": "string"},
        ]}))
        self.records = self.dir / "deals.csv"
        self.records.write_text(
            "Record ID,Package,budget,Services,tags\n"
            "1,accelerator,10k,Ann Owner,a;b\n"
            "2,accelerator,,Bob Owner,a\n"
            "3,other,secret free text,,\n"
            "4,,, ,\n"
            "\n",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_command(self, **extra):
        out, err = io.StringIO(), io.StringIO()
        options = {"provider": "hubspot", "object_type": "deal",
                   "definitions": str(self.defs), "records": str(self.records), **extra}
        call_command("field_inventory", stdout=out, stderr=err, **options)
        return out.getvalue(), err.getvalue()

    def rows(self, text):
        return {r["source_id"]: r for r in csv.DictReader(io.StringIO(text))}

    def test_new_inventory_from_definitions_and_records(self):
        out, err = self.run_command()
        rows = self.rows(out)
        self.assertEqual(set(rows), {"package", "budget", "services", "tags", "not_exported"})
        self.assertEqual((rows["package"]["populated"], rows["package"]["total"]), ("3", "4"))
        self.assertEqual(rows["package"]["options"], "accelerator=2|other=1|unused=0")
        self.assertEqual(rows["budget"]["populated"], "2")
        self.assertEqual(rows["budget"]["options"], "")  # free text never leaves the export
        self.assertNotIn("secret free text", out)
        self.assertNotIn("Owner", out)
        self.assertEqual(rows["tags"]["options"], "a=2|b=1")  # multi-select split on ;
        # No option list (an owner picker, say): values could be names, so none
        # are written.
        self.assertEqual(rows["services"]["options"], "")
        self.assertEqual(rows["services"]["populated"], "2")
        self.assertEqual(rows["not_exported"]["populated"], "")  # not in the export
        self.assertEqual(rows["package"]["source_label"], "Package")
        for r in rows.values():
            self.assertEqual((r["filtered"], r["decision"]), ("unknown", ""))
        self.assertIn("4 record(s), 5 definition(s), 5 new row(s)", err)
        self.assertIn("not in the export: not_exported", err)
        # Undecided rows (no decision, no pii call) fail the validator until a
        # person decides.
        self.assertEqual(set(codes(out)), {"bad_decision", "bad_pii"})

    def test_updates_an_existing_inventory_without_touching_decisions(self):
        inventory = self.dir / "hubspot.csv"
        inventory.write_text(to_csv(
            row(source_id="package", populated="1", total="1", filtered="yes",
                evidence="view:9", notes="kept"),
            row(object="contact", source_id="email", decision="map", target="PartyEmail.email",
                pii="yes", populated="7", total="9"),
        ))
        output = self.dir / "out.csv"
        self.run_command(inventory=str(inventory), output=str(output))
        rows = list(csv.DictReader(output.open()))
        package = next(r for r in rows if r["source_id"] == "package")
        self.assertEqual((package["populated"], package["total"]), ("3", "4"))
        self.assertEqual((package["filtered"], package["evidence"], package["decision"],
                          package["target"], package["notes"]),
                         ("yes", "view:9", "sidecar", "custom.package", "kept"))
        email = next(r for r in rows if r["source_id"] == "email")
        self.assertEqual((email["populated"], email["total"]), ("7", "9"))  # other object untouched
        self.assertEqual(len(rows), 6)  # 2 existing + 4 new deal properties

    def test_jsonl_canonical_envelope_and_object_filter(self):
        self.defs.write_text("\n".join(json.dumps(r) for r in (
            {"object_type": "property_def", "source_id": "deal/package",
             "data": {"object_type": "deal", "name": "package", "label": "Package",
                      "type": "enumeration", "options": [{"value": "accelerator"}]}},
            {"object_type": "property_def", "source_id": "contact/email",
             "data": {"object_type": "contact", "name": "email", "label": "Email",
                      "type": "string"}},
        )))
        out, _err = self.run_command()
        rows = self.rows(out)
        self.assertEqual(set(rows), {"package"})
        self.assertEqual(rows["package"]["options"], "accelerator=2|other=1")

    def test_refusals(self):
        with self.assertRaisesMessage(CommandError, "not a provider namespace"):
            self.run_command(provider="Hub Spot")
        self.defs.write_text("{not json")
        with self.assertRaisesMessage(CommandError, "--definitions"):
            self.run_command()
        self.defs.write_text(json.dumps([{"name": "x", "object_type": "contact"}]))
        with self.assertRaisesMessage(CommandError, "no definitions for 'deal'"):
            self.run_command()
        with self.assertRaisesMessage(CommandError, "no such file"):
            self.run_command(inventory=str(self.dir / "missing.csv"))
        bad_inventory = self.dir / "bad.csv"
        bad_inventory.write_text("provider,object\n")
        self.defs.write_text(json.dumps([{"name": "x"}]))
        with self.assertRaisesMessage(CommandError, "missing column(s)"):
            self.run_command(inventory=str(bad_inventory))


class CountsUnitTests(SimpleTestCase):
    def test_format_options_keeps_defined_order_then_extras_by_count(self):
        from collections import Counter

        self.assertEqual(counts.format_options({"options": []}, Counter({"Ann": 3})), "")
        definition = {"options": ["b", "a"]}
        self.assertEqual(counts.format_options(definition, Counter({"a": 1, "z": 5, "y": 5, "x|y": 1})),
                         "b=0|a=1|y=5|z=5|x/y=1")
