"""The canonical import format: schema rules, reader/writer, command, contract.

Every schema rule has a refusing fixture under ``fixtures/canonical/invalid/``
(see ``canonical_cases.py``); ``test_every_issue_code_has_a_fixture`` keeps it
that way. None of these tests touch the database.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from core.plugins.crm_export import BaseCrmExportAdapter, assert_crm_export_contract
from core.plugins.payroll import AdapterError
from crm_import.canonical import (
    ISSUE_CODES,
    MANIFEST_FILE,
    OBJECT_FILES,
    OBJECT_TYPES,
    SCHEMA_VERSION,
    CanonicalFormatError,
    dump_record,
    iter_records,
    make_record,
    read_manifest,
    validate_canonical,
    write_manifest,
    write_records,
)

from .canonical_cases import EXPECTED_NAME, VALID, build_case, case_dirs, copy_valid, refresh_manifest


class TmpDirMixin:
    def tmp(self) -> Path:
        path = Path(tempfile.mkdtemp(prefix="canonical-"))
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def valid_copy(self) -> Path:
        return copy_valid(self.tmp() / "export")

    def rewrite(self, directory: Path, object_type: str, mutate) -> None:
        """Mutate one type's records in place and refresh the manifest."""
        path = directory / OBJECT_FILES[object_type]
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        mutate(records)
        path.write_text("".join(dump_record(r) + "\n" for r in records), encoding="utf-8")
        refresh_manifest(directory)

    def codes(self, result) -> list[str]:
        return [i.code for i in result.errors]


class ValidFixtureTests(TmpDirMixin, SimpleTestCase):
    def test_valid_small_matches_expected(self):
        result = validate_canonical(VALID)
        expected = json.loads((VALID / "expected" / EXPECTED_NAME).read_text(encoding="utf-8"))
        self.assertEqual(result.to_dict(messages=False), expected)
        self.assertTrue(result.ok)

    def test_valid_small_covers_every_object_type(self):
        result = validate_canonical(VALID)
        self.assertEqual(set(result.counts), set(OBJECT_TYPES))
        self.assertTrue(all(result.counts[t] >= 1 for t in OBJECT_TYPES), result.counts)

    def test_fixture_uses_reserved_example_data_only(self):
        """Synthetic people only (spec 5.1): example domains, 555-01xx numbers."""
        import re

        text = "".join(p.read_text(encoding="utf-8") for p in VALID.glob("*.jsonl"))
        for address in re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", text):
            self.assertRegex(address, r"(^|\.)example\.(com|org|net)$")
        for digits in re.findall(r"555-?(\d{4})", text):
            self.assertTrue(digits.startswith("01"), digits)

    def test_alias_resolves_a_reference(self):
        """deal:401->contact:399 names the merged-away id of contact 301."""
        result = validate_canonical(VALID)
        self.assertEqual(result.warnings, [])


class RefusingFixtureTests(TmpDirMixin, SimpleTestCase):
    def test_each_case_matches_its_expected_issues(self):
        for case in case_dirs():
            with self.subTest(case=case.name):
                out = build_case(case, self.tmp() / case.name)
                result = validate_canonical(out)
                expected = json.loads((case / EXPECTED_NAME).read_text(encoding="utf-8"))
                self.assertEqual(
                    {"ok": result.ok,
                     "errors": [i.to_dict(messages=False) for i in result.errors],
                     "warnings": [i.to_dict(messages=False) for i in result.warnings]},
                    expected,
                )
                for issue in result.errors + result.warnings:
                    self.assertIn(issue.code, ISSUE_CODES)
                    self.assertTrue(issue.message)

    def test_every_issue_code_has_a_fixture(self):
        seen: set[str] = set()
        for case in case_dirs():
            expected = json.loads((case / EXPECTED_NAME).read_text(encoding="utf-8"))
            seen.update(i["code"] for i in expected["errors"] + expected["warnings"])
        self.assertEqual(set(ISSUE_CODES) - seen, set())

    def test_every_refusing_case_refuses(self):
        valid_cases = {"unresolved-ref-partial", "file-absent-unknown"}
        for case in case_dirs():
            expected = json.loads((case / EXPECTED_NAME).read_text(encoding="utf-8"))
            self.assertEqual(expected["ok"], case.name in valid_cases, case.name)


class RuleTests(TmpDirMixin, SimpleTestCase):
    """Rules worth pinning beyond the fixtures."""

    def test_nan_is_not_json(self):
        d = self.valid_copy()
        path = d / OBJECT_FILES["deal"]
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[0] = lines[0].replace('"amount":"1200.00"', '"amount":NaN')
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        refresh_manifest(d)
        result = validate_canonical(d)
        self.assertEqual([(i.code, i.line) for i in result.errors], [("invalid_json", 1)])

    def test_bool_is_not_an_int(self):
        d = self.valid_copy()
        self.rewrite(d, "pipeline", lambda rs: rs[1]["data"]["stages"][0].update(probability=True))
        self.assertEqual(self.codes(validate_canonical(d)), ["wrong_type"])

    def test_probability_range(self):
        d = self.valid_copy()
        self.rewrite(d, "pipeline", lambda rs: rs[1]["data"]["stages"][0].update(probability=101))
        result = validate_canonical(d)
        self.assertEqual([(i.code, i.field) for i in result.errors],
                         [("invalid_value", "data.stages[0].probability")])

    def test_null_where_not_nullable(self):
        d = self.valid_copy()
        self.rewrite(d, "deal", lambda rs: rs[0]["data"].update(pipeline_ref=None))
        self.assertEqual([(i.code, i.field) for i in validate_canonical(d).errors],
                         [("wrong_type", "data.pipeline_ref")])

    def test_missing_amount_is_null_not_zero(self):
        """A null amount is valid; the importer must keep it NULL (spec I11)."""
        result = validate_canonical(VALID)
        records = [r for _, r in iter_records(VALID, "deal")]
        self.assertTrue(result.ok)
        self.assertIsNone(records[1]["data"]["amount"])

    def test_impossible_date_is_invalid(self):
        d = self.valid_copy()
        self.rewrite(d, "deal", lambda rs: rs[0]["data"].update(expected_close_date="2026-02-30"))
        self.assertEqual([(i.code, i.field) for i in validate_canonical(d).errors],
                         [("invalid_value", "data.expected_close_date")])

    def test_unknown_ref_object_type(self):
        d = self.valid_copy()
        self.rewrite(d, "task", lambda rs: rs[0]["data"]["target_refs"].append(
            {"object_type": "invoice", "source_id": "1"}))
        self.assertEqual([(i.code, i.field) for i in validate_canonical(d).errors],
                         [("invalid_value", "data.target_refs[2].object_type")])

    def test_dangling_stage_ref(self):
        d = self.valid_copy()
        self.rewrite(d, "deal", lambda rs: rs[0]["data"]["stage_ref"].update(source_id="s-gone"))
        self.assertEqual([(i.code, i.field) for i in validate_canonical(d).errors],
                         [("dangling_ref", "data.stage_ref")])

    def test_stage_ids_may_repeat_across_pipelines(self):
        """Provider stage ids are unique per pipeline (ticket stages "1".."4")."""
        d = self.valid_copy()

        def same_ids(rs):
            rs[0]["data"]["stages"][0]["source_id"] = "s-new"  # partners' t-open becomes s-new
        self.rewrite(d, "pipeline", same_ids)
        self.rewrite(d, "relationship", lambda rs: rs[0]["data"]["stage_ref"].update(source_id="s-new"))
        self.assertTrue(validate_canonical(d).ok, validate_canonical(d).errors)

    def test_suppression_only_consent_is_valid(self):
        """A hard bounce is a Suppression and no ConsentRecord (spec 2.11)."""
        records = {r["source_id"]: r for _, r in iter_records(VALID, "consent")}
        self.assertIsNone(records["803"]["data"]["state"])
        self.assertTrue(validate_canonical(VALID).ok)

    def test_declared_total_counts_live_records_only(self):
        manifest = read_manifest(VALID)
        self.assertEqual(manifest["objects"]["company"]["rows"], 2)  # one archived
        self.assertEqual(manifest["objects"]["company"]["declared_total"], 1)

    def test_communication_raw_envelope_needs_schema_version(self):
        d = self.valid_copy()
        self.rewrite(d, "communication", lambda rs: rs[0]["data"]["raw"].pop("schema_version"))
        self.assertEqual([(i.code, i.field) for i in validate_canonical(d).errors],
                         [("missing_key", "data.raw.schema_version")])

    def test_invalid_time_zone(self):
        d = self.valid_copy()
        manifest = json.loads((d / MANIFEST_FILE).read_text())
        manifest["input"]["time_zone"] = "Mars/Olympus_Mons"
        (d / MANIFEST_FILE).write_text(json.dumps(manifest))
        self.assertEqual([(i.code, i.field) for i in validate_canonical(d).errors],
                         [("invalid_value", "input.time_zone")])

    def test_max_errors_caps_and_counts_the_rest(self):
        d = self.valid_copy()
        self.rewrite(d, "contact", lambda rs: [r.update(archived="x", raw_ref=1) for r in rs])
        result = validate_canonical(d, max_errors=2)
        self.assertEqual(len(result.errors), 2)
        self.assertEqual(result.suppressed, 4)
        self.assertFalse(result.ok)

    def test_raise_for_errors(self):
        validate_canonical(VALID).raise_for_errors()
        case = next(c for c in case_dirs() if c.name == "dangling-association")
        result = validate_canonical(build_case(case, self.tmp() / "x"))
        with self.assertRaises(CanonicalFormatError) as cm:
            result.raise_for_errors()
        self.assertIn("dangling_ref", str(cm.exception))
        self.assertIsInstance(cm.exception, AdapterError)

    def test_not_a_directory(self):
        with self.assertRaises(FileNotFoundError):
            validate_canonical(self.tmp() / "nope")


class ReaderWriterTests(TmpDirMixin, SimpleTestCase):
    def test_iter_records_streams_in_file_order(self):
        rows = list(iter_records(VALID, "contact"))
        self.assertEqual([(n, r["source_id"]) for n, r in rows], [(1, "301"), (2, "302"), (3, "303")])

    def test_iter_records_missing_file_yields_nothing(self):
        d = self.valid_copy()
        (d / OBJECT_FILES["task"]).unlink()
        self.assertEqual(list(iter_records(d, "task")), [])

    def test_iter_records_refuses_newer_version(self):
        case = next(c for c in case_dirs() if c.name == "unknown-version-record")
        d = build_case(case, self.tmp() / "x")
        with self.assertRaises(CanonicalFormatError) as cm:
            list(iter_records(d, "contact"))
        self.assertIn("contacts.jsonl:2", str(cm.exception))

    def test_iter_records_refuses_bad_json_with_line(self):
        case = next(c for c in case_dirs() if c.name == "malformed-lines")
        d = build_case(case, self.tmp() / "x")
        with self.assertRaises(CanonicalFormatError) as cm:
            list(iter_records(d, "task"))
        self.assertIn("tasks.jsonl:3", str(cm.exception))

    def test_read_manifest(self):
        self.assertEqual(read_manifest(VALID)["source"], "examplecrm:4242")
        d = self.valid_copy()
        (d / MANIFEST_FILE).unlink()
        with self.assertRaises(CanonicalFormatError):
            read_manifest(d)
        d = self.valid_copy()
        (d / MANIFEST_FILE).write_text(json.dumps({**read_manifest(VALID), "schema_version": 99}))
        with self.assertRaises(CanonicalFormatError):
            read_manifest(d)

    def test_make_record_fills_the_envelope(self):
        rec = make_record("owner", source="x:1", source_id=7, data={"a": 1}, aliases=["9", "8"])
        self.assertEqual(rec["schema_version"], SCHEMA_VERSION)
        self.assertEqual(rec["source_id"], "7")
        self.assertEqual(rec["source_object_type"], "owner")
        self.assertEqual(rec["aliases"], ["8", "9"])
        self.assertNotIn("history", rec)  # absent, not [] (spec 1.2)
        with self.assertRaises(ValueError):
            make_record("ticket", source="x:1", source_id="1", data={})

    def test_writer_sorts_and_is_byte_identical(self):
        records = [r for _, r in iter_records(VALID, "contact")]
        a, b = self.tmp(), self.tmp()
        rows_a, digest_a = write_records(a, "contact", list(reversed(records)))
        rows_b, digest_b = write_records(b, "contact", records)
        self.assertEqual((rows_a, digest_a), (rows_b, digest_b))
        self.assertEqual((a / "contacts.jsonl").read_bytes(), (b / "contacts.jsonl").read_bytes())
        self.assertEqual((a / "contacts.jsonl").read_bytes(), (VALID / "contacts.jsonl").read_bytes())

    def test_writer_refuses_lone_surrogates(self):
        rec = make_record("owner", source="x:1", source_id="1", data={"display_name": "\ud83d"})
        with self.assertRaises(CanonicalFormatError) as cm:
            write_records(self.tmp(), "owner", [rec])
        self.assertIn("owner '1'", str(cm.exception))

    def test_dump_record_refuses_nan(self):
        with self.assertRaises(ValueError):
            dump_record({"x": float("nan")})

    def test_write_manifest_round_trip(self):
        out = self.tmp()
        manifest = read_manifest(VALID)
        for otype in OBJECT_TYPES:
            write_records(out, otype, [r for _, r in iter_records(VALID, otype)])
        objects = {t: {k: v for k, v in e.items() if k not in ("file", "rows")}
                   for t, e in manifest["objects"].items()}
        written = write_manifest(out, provider=manifest["provider"], source=manifest["source"],
                                 adapter=manifest["adapter"], exported_at=manifest["exported_at"],
                                 input=manifest["input"], objects=objects)
        self.assertEqual(written, manifest)
        self.assertEqual((out / MANIFEST_FILE).read_bytes(), (VALID / MANIFEST_FILE).read_bytes())


class CheckCanonicalExportCommandTests(TmpDirMixin, SimpleTestCase):
    def test_valid_directory(self):
        out = io.StringIO()
        call_command("check_canonical_export", str(VALID), stdout=out)
        self.assertIn("OK: 24 records, 0 warning(s).", out.getvalue())
        self.assertIn("contact        contacts.jsonl               3  complete", out.getvalue())

    def test_invalid_directory_fails_and_lists_issues(self):
        case = next(c for c in case_dirs() if c.name == "dangling-association")
        d = build_case(case, self.tmp() / "x")
        out = io.StringIO()
        with self.assertRaises(CommandError) as cm:
            call_command("check_canonical_export", str(d), stdout=out)
        self.assertIn("1 error(s)", str(cm.exception))
        self.assertIn("associations.jsonl:4 data.to_ref: dangling_ref", out.getvalue())

    def test_json_output(self):
        out = io.StringIO()
        call_command("check_canonical_export", str(VALID), "--json", stdout=out)
        self.assertTrue(json.loads(out.getvalue())["ok"])

    def test_not_a_directory(self):
        with self.assertRaises(CommandError):
            call_command("check_canonical_export", str(self.tmp() / "missing"))


class _CopyAdapter(BaseCrmExportAdapter):
    """Reference adapter for the contract: its "provider files" are a
    hand-written canonical directory, re-written through the writer."""

    provider_id = "examplecrm"
    schema_version = 1

    def export(self, input_dir, out_dir, *, mapping, time_zone):
        manifest = read_manifest(input_dir)
        for otype in OBJECT_TYPES:
            write_records(out_dir, otype, [r for _, r in iter_records(input_dir, otype)])
        objects = {t: {k: v for k, v in e.items() if k not in ("file", "rows")}
                   for t, e in manifest["objects"].items()}
        return write_manifest(out_dir, provider=self.provider_id, source=manifest["source"],
                              adapter={"id": "copy", "version": "1.0.0", "schema_version": self.schema_version},
                              exported_at=manifest["exported_at"], input=manifest["input"], objects=objects)


class CrmExportContractTests(TmpDirMixin, SimpleTestCase):
    def test_contract_passes_on_hand_written_directory(self):
        assert_crm_export_contract(_CopyAdapter, [VALID])

    def test_contract_requires_a_fixture(self):
        with self.assertRaisesMessage(AssertionError, "at least one"):
            assert_crm_export_contract(_CopyAdapter, [])

    def test_contract_catches_non_deterministic_output(self):
        class Clocked(_CopyAdapter):
            runs = 0

            def export(self, input_dir, out_dir, *, mapping, time_zone):
                manifest = super().export(input_dir, out_dir, mapping=mapping, time_zone=time_zone)
                type(self).runs += 1
                manifest["exported_at"] = f"2026-10-01T09:00:0{type(self).runs}Z"
                (Path(out_dir) / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
                return manifest

        with self.assertRaisesMessage(AssertionError, "not byte-identical"):
            assert_crm_export_contract(Clocked, [VALID])

    def test_contract_catches_invalid_output(self):
        class Unsorted(_CopyAdapter):
            def export(self, input_dir, out_dir, *, mapping, time_zone):
                manifest = super().export(input_dir, out_dir, mapping=mapping, time_zone=time_zone)
                path = Path(out_dir) / OBJECT_FILES["contact"]
                path.write_text("".join(reversed(path.read_text().splitlines(keepends=True))))
                return manifest

        with self.assertRaisesMessage(AssertionError, "fails validation"):
            assert_crm_export_contract(Unsorted, [VALID])

    def test_contract_catches_wrong_provider(self):
        class Other(_CopyAdapter):
            provider_id = "othercrm"

        # the manifest it writes says provider othercrm but source examplecrm:… -> invalid
        with self.assertRaises(AssertionError):
            assert_crm_export_contract(Other, [VALID])

    def test_contract_rejects_unknown_schema_version(self):
        class Future(_CopyAdapter):
            schema_version = 99

        with self.assertRaisesMessage(AssertionError, "schema_version"):
            assert_crm_export_contract(Future, [VALID])

    def test_provider_id_required(self):
        with self.assertRaises(TypeError):
            class Nameless(BaseCrmExportAdapter):
                def export(self, input_dir, out_dir, *, mapping, time_zone):
                    return {}
