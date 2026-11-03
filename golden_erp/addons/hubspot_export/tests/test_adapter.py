"""The HubSpot export adapter (docs/crm/phase5-importer.md section 2, step 7).

Every test reads recorded fixtures only: the adapter is a pure function of
files, and the suite's no-network guard (core.testing.no_network) fails any
test that opens a socket. None of these tests touch the database.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from core.plugins import plugin_registry
from core.plugins.base import BasePlugin
from core.plugins.crm_export import AdapterError, BaseCrmExportAdapter, assert_crm_export_contract
from crm_import.canonical import COVERAGE_REASONS, MANIFEST_FILE, read_manifest, validate_canonical
from golden_erp.addons.hubspot_export import reader_crawl, reader_csv
from golden_erp.addons.hubspot_export.adapter import HubSpotExportAdapter
from golden_erp.addons.hubspot_export.convert import RAW_SCHEMA_VERSION
from golden_erp.addons.hubspot_export.mapping import STANDARD, validate_mapping
from golden_erp.addons.hubspot_export.plugin import HubSpotExportPlugin
from golden_erp.addons.hubspot_export.values import html_to_text, parse_decimal, parse_instant, slug_key

from .helpers import BASES, FIXTURES, UPDATE, build_case, case_dirs, case_spec, copy_input, expected_name, reason_codes

PLUGIN = "golden_erp.addons.hubspot_export.plugin.HubSpotExportPlugin"


class TmpMixin:
    def tmp(self) -> Path:
        path = Path(tempfile.mkdtemp(prefix="hubspot-export-"))
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def export(self, input_dir: Path, **kwargs) -> tuple[Path, dict]:
        out = self.tmp() / "canonical"
        kwargs = {"mapping": None, "time_zone": None, **kwargs}
        manifest = HubSpotExportAdapter().export(input_dir, out, **kwargs)
        return out, manifest

    def records(self, out: Path, file_name: str) -> dict[str, dict]:
        lines = (out / file_name).read_text(encoding="utf-8").splitlines()
        return {r["source_id"]: r for r in map(json.loads, lines)}

    def crawl(self) -> tuple[Path, dict]:
        return self.export(FIXTURES / "crawl-small")


def _check_expected(test: SimpleTestCase, out: Path, expected_dir: Path, names: list[str]) -> None:
    if UPDATE:
        expected_dir.mkdir(exist_ok=True)
        for old in expected_dir.glob("*.expected.*"):
            old.unlink()
        for name in names:
            shutil.copyfile(out / name, expected_dir / expected_name(name))
    for name in names:
        expected = expected_dir / expected_name(name)
        test.assertTrue(expected.is_file(), f"{expected} is missing; run with HUBSPOT_EXPORT_UPDATE_EXPECTED=1")
        test.assertEqual((out / name).read_bytes(), expected.read_bytes(), f"{name} differs from {expected}")


class ContractTests(SimpleTestCase):
    def test_contract_passes_on_every_fixture_directory(self):
        """assert_crm_export_contract on every input that converts: the two bases and every limit case."""
        work = Path(tempfile.mkdtemp(prefix="hubspot-contract-"))
        self.addCleanup(shutil.rmtree, work, True)
        fixtures = [(FIXTURES / name, kwargs) for name, kwargs in BASES.items()]
        for case in case_dirs():
            if "refuses" not in case_spec(case):
                fixtures.append(build_case(case, work / case.name))
        self.assertGreaterEqual(len(fixtures), 14)
        assert_crm_export_contract(HubSpotExportAdapter, fixtures)

    def test_adapter_declares_provider_and_schema(self):
        self.assertEqual(HubSpotExportAdapter.provider_id, "hubspot")
        self.assertTrue(issubclass(HubSpotExportAdapter, BaseCrmExportAdapter))
        self.assertEqual(HubSpotExportAdapter.schema_version, 1)


class ExpectedOutputTests(TmpMixin, SimpleTestCase):
    """Each base fixture's canonical output equals expected/ byte for byte."""

    def _base(self, name: str):
        out, manifest = self.export(FIXTURES / name, **BASES[name])
        names = sorted(p.name for p in out.iterdir())
        _check_expected(self, out, FIXTURES / name / "expected", names)
        result = validate_canonical(out)
        self.assertTrue(result.ok, result.errors[:5])
        self.assertEqual(result.warnings, [])
        return out, manifest

    def test_crawl_small(self):
        out, manifest = self._base("crawl-small")
        self.assertEqual({e["coverage"] for e in manifest["objects"].values()}, {"complete"})
        self.assertEqual(manifest["input"], {"kind": "crawl", "time_zone": None})
        self.assertEqual(manifest["source"], "hubspot:1234567")
        self.assertEqual(manifest["exported_at"], "2026-10-01T09:00:00Z")

    def test_export_csv(self):
        out, manifest = self._base("export-csv")
        self.assertEqual(manifest["input"], {"kind": "export", "time_zone": "America/New_York"})
        self.assertEqual(manifest["objects"]["communication"]["coverage"], "unknown")
        self.assertFalse((out / "communications.jsonl").exists())

    def test_output_is_byte_identical_across_runs(self):
        for name, kwargs in BASES.items():
            first, _ = self.export(FIXTURES / name, **kwargs)
            second, _ = self.export(FIXTURES / name, **kwargs)
            tree = lambda d: {p.name: p.read_bytes() for p in sorted(d.iterdir())}  # noqa: E731
            self.assertEqual(tree(first), tree(second), name)

    def test_refusal_writes_nothing(self):
        out = self.tmp() / "canonical"
        with self.assertRaises(AdapterError):
            HubSpotExportAdapter().export(FIXTURES / "export-csv", out, mapping=None, time_zone=None)
        self.assertFalse(out.exists())


class LimitCaseTests(TmpMixin, SimpleTestCase):
    """Each 2.13 limit has a fixture that produces exactly its coverage reason."""

    def setUp(self):
        self.base_manifests = {}
        for name, kwargs in BASES.items():
            _, self.base_manifests[name] = self.export(FIXTURES / name, **kwargs)

    def test_each_case(self):
        for case in case_dirs():
            spec = case_spec(case)
            with self.subTest(case=case.name):
                input_dir, kwargs = build_case(case, self.tmp() / "input")
                out = self.tmp() / "canonical"
                if "refuses" in spec:
                    with self.assertRaises(AdapterError) as caught:
                        HubSpotExportAdapter().export(input_dir, out, **kwargs)
                    self.assertIn(spec["refuses"], str(caught.exception))
                    self.assertFalse((out / MANIFEST_FILE).exists())
                    continue
                manifest = HubSpotExportAdapter().export(input_dir, out, **kwargs)
                _check_expected(self, out, case / "expected", [MANIFEST_FILE])
                self.assertTrue(validate_canonical(out).ok)
                base, now = reason_codes(self.base_manifests[spec["base"]]), reason_codes(manifest)
                added = {t: sorted(now[t] - base[t]) for t in now if now[t] - base[t]}
                removed = {t: sorted(base[t] - now[t]) for t in now if base[t] - now[t]}
                self.assertEqual(added, spec["added"], case.name)
                self.assertEqual(removed, {}, case.name)
                for otype, history in spec.get("history", {}).items():
                    self.assertEqual(manifest["objects"][otype].get("history", []), history)
                    self.assertEqual(manifest["objects"][otype]["coverage"], "complete")

    def test_every_hubspot_reason_has_a_case(self):
        """Every closed-list reason the adapter emits is triggered by a case. history_not_exported is
        never emitted: per 2.13 a missing history leaves the object complete and the manifest's
        history list says which properties were exported (the history-absent case)."""
        produced = set()
        for case in case_dirs():
            for codes in case_spec(case).get("added", {}).values():
                produced.update(codes)
        self.assertEqual(produced, set(COVERAGE_REASONS) - {"history_not_exported"})
        self.assertTrue((FIXTURES / "cases" / "history-absent").is_dir())

    def test_search_cap_at_exactly_ten_thousand_rows(self):
        """A search crawl that returns exactly the cap is capped even when total is not recorded."""
        input_dir, kwargs = build_case(FIXTURES / "cases" / "search-capped", self.tmp() / "input")
        for page in sorted((input_dir / "crawl" / "contacts").glob("page-*.json")):
            data = json.loads(page.read_text(encoding="utf-8"))
            del data["payload"]["total"]
            page.write_text(json.dumps(data), encoding="utf-8")
        with mock.patch.object(reader_crawl, "SEARCH_CAP", 7):
            _, manifest = self.export(input_dir, time_zone=kwargs["time_zone"])
        self.assertEqual(reason_codes(manifest)["contact"], {"search_cap"})

    def test_export_row_limit_is_truncation(self):
        with mock.patch.object(reader_csv, "EXPORT_ROW_LIMITS", (2,)):
            _, manifest = self.export(FIXTURES / "export-csv", time_zone="America/New_York")
        self.assertIn("truncated_export", reason_codes(manifest)["company"])

    def test_server_error_refuses(self):
        input_dir = copy_input(FIXTURES / "crawl-small", self.tmp() / "input")
        errors = input_dir / "crawl" / "errors.json"
        data = json.loads(errors.read_text(encoding="utf-8"))
        data["payload"]["results"] = [{"method": "GET", "path": "/crm/v3/objects/notes", "status": 502}]
        errors.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesMessage(AdapterError, "the crawl failed"):
            self.export(input_dir)


class SemanticsTests(TmpMixin, SimpleTestCase):
    """What the canonical records say about the source (spec 2.3-2.11)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.work = Path(tempfile.mkdtemp(prefix="hubspot-semantics-"))
        cls.out = cls.work / "out"
        HubSpotExportAdapter().export(FIXTURES / "crawl-small", cls.out, mapping=None, time_zone=None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, True)
        super().tearDownClass()

    def test_archived_owner_and_owner_without_a_user(self):
        owners = self.records(self.out, "owners.jsonl")
        self.assertTrue(owners["104"]["archived"])
        self.assertFalse(owners["104"]["data"]["active"])
        self.assertEqual(owners["104"]["data"]["display_name"], "Dana Former")
        self.assertNotIn("userId", owners["103"]["properties"])  # never had a login
        self.assertEqual(owners["102"]["data"]["teams"][0], {"source_id": "12", "name": "West Team", "primary": True})

    def test_pipeline_keys_outcomes_and_mapping(self):
        pipes = self.records(self.out, "pipelines.jsonl")
        self.assertEqual(pipes["12345678"]["data"]["key"], "partner_referral")  # mapping file
        self.assertEqual(pipes["default"]["data"]["key"], "default")  # readable id
        stages = {s["source_id"]: s for s in pipes["12345678"]["data"]["stages"]}
        self.assertEqual(stages["12345679"]["key"], "hs_12345679")  # numeric id, prefixed
        self.assertEqual(stages["12345680"]["outcome"], "closed")  # closed at 50 %: never guessed
        self.assertEqual(stages["12345681"]["outcome"], "won")
        self.assertEqual(pipes["0"]["data"]["applies_to"], "ticket")
        self.assertEqual([s["key"] for s in pipes["0"]["data"]["stages"]], ["new", "onboarding", "active", "ended"])

    def test_deal_outcome_amount_currency_follow_up(self):
        deals = self.records(self.out, "deals.jsonl")
        self.assertEqual(deals["401"]["data"]["status"], "open")
        self.assertEqual(deals["401"]["data"]["expected_close_date"], "2026-11-30")
        self.assertIsNone(deals["401"]["data"]["closed_at"])
        self.assertEqual(deals["402"]["data"]["status"], "won")
        self.assertEqual(deals["402"]["data"]["amount"], "4500.50")
        self.assertEqual(deals["403"]["data"]["status"], "lost")
        self.assertIsNone(deals["403"]["data"]["amount"])  # missing is not zero
        self.assertEqual(deals["403"]["data"]["loss_reason_raw"], "Went with competitor")
        self.assertIsNone(deals["404"]["data"]["status"])  # a "closed" stage without flags
        self.assertEqual(deals["404"]["data"]["currency"], "USD")  # the portal's currency
        self.assertEqual(deals["405"]["data"]["currency"], "EUR")
        self.assertEqual(deals["405"]["data"]["follow_up"], [{"field": "sales_followup_date", "value": "2026-10-20"}])
        self.assertEqual([h["value"] for h in deals["401"]["history"] if h["property"] == "dealstage"],
                         ["appointmentscheduled", "qualifiedtobuy", "contractsent"])
        self.assertEqual(deals["405"]["data"]["contact_refs"], [])  # the deal with no contact
        self.assertEqual([r["source_id"] for r in deals["406"]["data"]["contact_refs"]], ["305", "306"])
        self.assertNotIn("history", deals["407"])  # archived: history not enumerated for it

    def test_contacts_keep_semantics(self):
        contacts = self.records(self.out, "contacts.jsonl")
        ann = contacts["301"]
        self.assertEqual(ann["aliases"], ["18", "33"])
        self.assertEqual([e["address"] for e in ann["data"]["emails"]], ["ann@example.com", "ann.alt@example.com"])
        self.assertEqual(ann["properties"]["lead_type"], "Patent Attorney")  # byte for byte
        self.assertEqual(contacts["302"]["properties"]["lead_type"], "patent attorney")
        self.assertEqual(contacts["302"]["data"]["emails"], [])  # no email is normal
        self.assertTrue(contacts["308"]["archived"])
        self.assertEqual([r["source_id"] for r in contacts["306"]["data"]["company_refs"]], ["202", "201"])
        self.assertEqual(contacts["307"]["properties"]["internal_scratch"], "call back")  # drop is the importer's

    def test_tickets_are_relationships(self):
        rels = self.records(self.out, "relationships.jsonl")
        self.assertEqual(set(rels), {"501", "502"})
        self.assertEqual(rels["501"]["source_object_type"], "ticket")
        self.assertEqual(rels["501"]["data"]["state"], "closed")
        self.assertEqual(rels["501"]["data"]["description"], "Refers patent work to us.")
        self.assertEqual(rels["501"]["properties"]["content"], "<p>Refers patent work to us.</p>")
        self.assertEqual(rels["502"]["data"]["state"], "open")
        deals = self.records(self.out, "deals.jsonl")
        self.assertFalse(set(rels) & set(deals))

    def test_tasks(self):
        tasks = self.records(self.out, "tasks.jsonl")
        self.assertEqual(tasks["603"]["data"]["kind"], "linkedin")  # mapping [task.kinds]
        self.assertEqual(tasks["603"]["data"]["status"], "waiting")
        self.assertEqual(tasks["604"]["data"]["completed_at"], "2023-12-01T09:00:00Z")  # fallback
        self.assertEqual(tasks["601"]["data"]["body_text"], "Ask about timing.")
        self.assertEqual(tasks["602"]["data"]["target_refs"][1], {"object_type": "relationship", "source_id": "502"})

    def test_communications_carry_the_verbatim_payload(self):
        comms = self.records(self.out, "communications.jsonl")
        for obj, sid in (("notes", "701"), ("meetings", "801"), ("calls", "901"), ("emails", "1001")):
            page = json.loads((FIXTURES / "crawl-small" / "crawl" / obj / "page-0001.json").read_text(encoding="utf-8"))
            recorded = next(r for r in page["payload"]["results"] if r["id"] == sid)
            raw = comms[sid]["data"]["raw"]
            self.assertEqual(raw, {"schema_version": RAW_SCHEMA_VERSION, "provider": "hubspot", "payload": recorded})
        self.assertEqual(comms["701"]["data"]["body_text"], "Met at the expo.\nWants a quote & samples.")
        self.assertEqual(comms["901"]["data"]["duration_seconds"], 45)
        self.assertEqual(comms["901"]["data"]["status"], "failed")
        self.assertEqual(comms["802"]["data"]["direction"], "internal")  # staff-only meeting
        self.assertEqual([p["role"] for p in comms["1001"]["data"]["participants"]], ["from", "to", "cc"])
        self.assertEqual(comms["1002"]["data"]["direction"], "inbound")

    def test_consent(self):
        consent = list(self.records(self.out, "consent.jsonl").values())
        optouts = [c for c in consent if c["data"]["suppress"] == "opt_out"]
        self.assertEqual([c["data"]["address"] for c in optouts], ["cara@example.org"])
        self.assertEqual(optouts[0]["data"]["state"], "withdrawn")
        self.assertEqual(optouts[0]["data"]["occurred_at"], "2024-04-01T08:00:00Z")  # from history
        bounce = [c for c in consent if c["data"]["suppress"] == "hard_bounce"]
        self.assertEqual(bounce[0]["data"]["state"], None)
        grants = [c for c in consent if c["data"]["state"] == "granted"]
        self.assertEqual(sorted(c["data"]["address"] for c in grants), ["ann@example.com", "eve@example.com"])
        for record in consent:
            self.assertNotIn("@", record["source_id"])  # ids carry no address

    def test_associations(self):
        assoc = self.records(self.out, "associations.jsonl")
        self.assertTrue(assoc["contact:306/company:202"]["data"]["primary"])
        self.assertEqual(assoc["contact:306/company:201"]["data"]["label"], "Billing contact")
        self.assertEqual(assoc["deal:401/contact:301"]["data"]["label"], "Decision maker")
        self.assertTrue(assoc["deal:401/company:201"]["data"]["primary"])
        self.assertEqual(assoc["ticket:501/contact:302"]["data"]["from_ref"],
                         {"object_type": "relationship", "source_id": "501"})


class CsvExportTests(TmpMixin, SimpleTestCase):
    def test_labels_map_back_to_internal_values(self):
        out, _ = self.export(FIXTURES / "export-csv", time_zone="America/New_York")
        deals = self.records(out, "deals.jsonl")
        self.assertEqual(deals["402"]["data"]["stage_ref"]["source_id"], "closedwon")  # label shared by 2 pipelines
        self.assertEqual(deals["404"]["data"]["stage_ref"]["source_id"], "12345680")
        self.assertEqual(deals["401"]["data"]["currency"], "USD")  # "US Dollar"
        self.assertEqual(deals["401"]["data"]["owner_ref"], {"object_type": "owner", "source_id": "101"})
        self.assertEqual(deals["401"]["properties"]["closedate"], "2026-11-30T17:00:00Z")  # 12:00 EST
        contacts = self.records(out, "contacts.jsonl")
        self.assertEqual(contacts["301"]["properties"]["lifecyclestage"], "customer")
        # 2022-03-13 02:30 does not exist in New York (spring forward); fold=0 reads it as EST.
        self.assertEqual(contacts["303"]["created_at"], "2022-03-13T07:30:00Z")

    def test_unknown_header_is_refused(self):
        input_dir = copy_input(FIXTURES / "export-csv", self.tmp() / "input")
        path = input_dir / "export" / "companies.csv"
        path.write_text(path.read_text(encoding="utf-8-sig").replace("Industry", "Sector"), encoding="utf-8")
        with self.assertRaisesMessage(AdapterError, "'Sector' match no companies property"):
            self.export(input_dir, time_zone="America/New_York")

    def test_bad_time_zone_is_refused(self):
        with self.assertRaisesMessage(AdapterError, "not an IANA time zone"):
            self.export(FIXTURES / "export-csv", time_zone="Mars/Olympus")

    def test_activity_csv_is_refused(self):
        input_dir = copy_input(FIXTURES / "export-csv", self.tmp() / "input")
        (input_dir / "export" / "notes.csv").write_text("Record ID\n1\n", encoding="utf-8")
        with self.assertRaisesMessage(AdapterError, "only from a recorded crawl"):
            self.export(input_dir, time_zone="America/New_York")


class MappingTests(TmpMixin, SimpleTestCase):
    NAMES = {"contacts": {"lead_type", "email"}, "deals": {"package"}}

    def test_valid(self):
        mapping = validate_mapping({"source": "hubspot:1", "contact": {"properties": {"lead_type": "sidecar"}},
                                    "deal": {"properties": {"package": "follow_up"}},
                                    "communication": {"default_direction": "inbound"}}, self.NAMES)
        self.assertEqual(mapping["source"], "hubspot:1")

    def test_refusals(self):
        cases = [
            ({"sources": "hubspot:1"}, "unknown top-level key"),
            ({"contact": {"properties": {"lead_typo": "sidecar"}}}, "not a contacts property"),
            ({"contact": {"properties": {"lead_type": "keep"}}}, "the target must be"),
            ({"contact": {"properties": {"lead_type": "follow_up"}}}, "deals only"),
            ({"default_currency": "usd"}, "ISO 4217"),
            ({"pipelines": {"default": {"key": "Sales Pipeline"}}}, "must match"),
            ({"tickets": {"relationship_kind": "partnership", "end": []}}, "[tickets] may hold only"),
            ({"associations": {"contact_deal": {}}}, "not an association pair"),
            ({"communication": {"default_direction": "sideways"}}, "'inbound' or 'outbound'"),
            ({"communication": {"direction": "inbound"}}, "[communication] may hold only"),
        ]
        for mapping, message in cases:
            with self.subTest(message=message), self.assertRaisesMessage(AdapterError, message):
                validate_mapping(mapping, self.NAMES)

    def test_source_must_match_the_recorded_portal(self):
        with self.assertRaisesMessage(AdapterError, "belongs to another portal"):
            self.export(FIXTURES / "crawl-small", mapping={"source": "hubspot:7654321"})

    def test_mapping_argument_overrides_the_input_file(self):
        out, manifest = self.export(FIXTURES / "crawl-small", mapping={"source": "hubspot:1234567"})
        self.assertEqual(manifest["source"], "hubspot:1234567")
        # The fixture's own mapping.toml (which keys pipeline 12345678) was not used.
        self.assertEqual(self.records(out, "pipelines.jsonl")["12345678"]["data"]["key"], "hs_12345678")

    def test_standard_map_follows_the_field_catalog(self):
        """Every standard property the adapter reads is in docs/crm/fields/hubspot-standard.csv
        (CRM Phase 4 step 1), except the listed extensions the catalog does not carry."""
        catalog = Path(settings.BASE_DIR) / "docs" / "crm" / "fields" / "hubspot-standard.csv"
        if not catalog.is_file():
            self.skipTest("docs/crm/fields/hubspot-standard.csv is not on this branch yet (CRM Phase 4 step 1)")
        listed: dict[str, set[str]] = {}
        with catalog.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                listed.setdefault(row["object"], set()).add(row["source_id"])
        # Standard HubSpot properties the adapter reads that the catalog does not (yet) list.
        extensions = {
            "contacts": {"hs_additional_emails", "fax", "hs_merged_object_ids"},
            "companies": {"hs_additional_domains", "address2", "hs_merged_object_ids"},
            "deals": {"hs_merged_object_ids"},
            "tickets": {"hs_lastmodifieddate"},
        }
        objects = {"contacts": "contact", "companies": "company", "deals": "deal", "tickets": "ticket"}
        for obj, catalog_obj in objects.items():
            missing = set(STANDARD[obj]) - listed.get(catalog_obj, set()) - extensions[obj]
            self.assertEqual(missing, set(), f"{obj}: properties the catalog does not list")


class ValueTests(SimpleTestCase):
    def test_instants(self):
        self.assertEqual(parse_instant("2024-03-04T10:15:00.000Z", where="t"), "2024-03-04T10:15:00Z")
        self.assertEqual(parse_instant("2024-03-04T10:15:00.120Z", where="t"), "2024-03-04T10:15:00.12Z")
        self.assertEqual(parse_instant("1709547300000", where="t"), "2024-03-04T10:15:00Z")
        self.assertEqual(parse_instant("2024-03-04T05:15:00-05:00", where="t"), "2024-03-04T10:15:00Z")
        self.assertIsNone(parse_instant("", where="t"))
        with self.assertRaisesMessage(AdapterError, "--time-zone"):
            parse_instant("2024-03-04 05:15", where="t")
        from zoneinfo import ZoneInfo

        # Fall back: 01:30 happens twice; fold=0 takes the first (EDT).
        self.assertEqual(parse_instant("2024-11-03 01:30", where="t", local_zone=ZoneInfo("America/New_York")),
                         "2024-11-03T05:30:00Z")

    def test_decimals(self):
        self.assertEqual(parse_decimal("1,200.50"), "1200.50")
        self.assertIsNone(parse_decimal(""))
        self.assertIsNone(parse_decimal("n/a"))
        self.assertIsNone(parse_decimal("1.1234567"))

    def test_html_and_keys(self):
        self.assertEqual(html_to_text("<p>a&nbsp;&amp; b</p><ul><li>one</li><li>two</li></ul>"), "a & b\none\ntwo")
        self.assertIsNone(html_to_text("<p> </p>"))
        self.assertEqual(slug_key("appointmentscheduled"), "appointmentscheduled")
        self.assertEqual(slug_key("12345678"), "hs_12345678")
        self.assertEqual(slug_key("Closed-Won Stage"), "closed_won_stage")


class PluginAndCommandTests(TmpMixin, SimpleTestCase):
    def test_registered_through_the_plugin_registry(self):
        with override_settings(GOLDEN_PLUGINS=[PLUGIN]):
            self.assertIs(plugin_registry.get_crm_export_adapters()["hubspot"], HubSpotExportAdapter)
        with override_settings(GOLDEN_PLUGINS=[]):
            self.assertEqual(plugin_registry.get_crm_export_adapters(), {})

    def test_duplicate_provider_fails_naming_both(self):
        class Other(BasePlugin):
            name = "other_hubspot"

            def get_crm_export_adapters(self):
                return {"hubspot": HubSpotExportAdapter}

        with override_settings(GOLDEN_PLUGINS=[PLUGIN, Other]):
            with self.assertRaisesMessage(ImproperlyConfigured, "hubspot_export and other_hubspot"):
                plugin_registry.get_crm_export_adapters()

    def test_bad_registrations_fail(self):
        class NotAnAdapter(BasePlugin):
            name = "not_an_adapter"

            def get_crm_export_adapters(self):
                return {"hubspot": object}

        class WrongKey(BasePlugin):
            name = "wrong_key"

            def get_crm_export_adapters(self):
                return {"hs": HubSpotExportAdapter}

        for plugin, message in ((NotAnAdapter, "expected a BaseCrmExportAdapter"), (WrongKey, "its provider_id")):
            with self.subTest(plugin=plugin.name), override_settings(GOLDEN_PLUGINS=[plugin]):
                with self.assertRaisesMessage(ImproperlyConfigured, message):
                    plugin_registry.get_crm_export_adapters()

    def test_plugin_metadata(self):
        self.assertEqual(HubSpotExportPlugin().get_crm_export_adapters(), {"hubspot": HubSpotExportAdapter})

    def test_command_writes_a_directory_check_canonical_export_accepts(self):
        out = self.tmp() / "canonical"
        stdout = io.StringIO()
        with override_settings(GOLDEN_PLUGINS=[PLUGIN]):
            call_command("crm_export_adapt", "--provider", "hubspot", "--input", str(FIXTURES / "export-csv"),
                         "--output", str(out), "--time-zone", "America/New_York", stdout=stdout)
        self.assertIn("archived_not_enumerated", stdout.getvalue())
        self.assertIn("fix for archived_not_enumerated", stdout.getvalue())
        call_command("check_canonical_export", str(out), stdout=io.StringIO())  # raises on any error
        self.assertEqual(read_manifest(out)["provider"], "hubspot")

    def test_command_mapping_option(self):
        mapping = self.tmp() / "mapping.toml"
        mapping.write_text('source = "hubspot:1234567"\n[pipelines.default]\nkey = "sales"\n', encoding="utf-8")
        out = self.tmp() / "canonical"
        with override_settings(GOLDEN_PLUGINS=[PLUGIN]):
            call_command("crm_export_adapt", "--provider", "hubspot", "--input", str(FIXTURES / "crawl-small"),
                         "--output", str(out), "--mapping", str(mapping), stdout=io.StringIO())
        keys = {json.loads(line)["data"]["key"] for line in (out / "pipelines.jsonl").read_text().splitlines()}
        self.assertIn("sales", keys)

    def test_command_refusals(self):
        out = self.tmp() / "canonical"
        with override_settings(GOLDEN_PLUGINS=[]):
            with self.assertRaisesMessage(CommandError, "No CRM export adapter 'hubspot' is installed"):
                call_command("crm_export_adapt", "--provider", "hubspot", "--input", str(FIXTURES / "crawl-small"),
                             "--output", str(out))
        with override_settings(GOLDEN_PLUGINS=[PLUGIN]):
            with self.assertRaisesMessage(CommandError, "--time-zone"):
                call_command("crm_export_adapt", "--provider", "hubspot", "--input", str(FIXTURES / "export-csv"),
                             "--output", str(out), stdout=io.StringIO())
            self.assertFalse((out / MANIFEST_FILE).exists())
            busy = self.tmp()
            (busy / "stale.jsonl").write_text("", encoding="utf-8")
            with self.assertRaisesMessage(CommandError, "not an empty directory"):
                call_command("crm_export_adapt", "--provider", "hubspot", "--input", str(FIXTURES / "crawl-small"),
                             "--output", str(busy))


class HygieneTests(SimpleTestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def test_fixtures_use_reserved_example_data_only(self):
        """Invented people only (spec 5.1): example domains, 555-01xx numbers, no real exports."""
        for path in sorted(FIXTURES.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8-sig")
            for domain in re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", text):
                self.assertRegex(domain, r"(^|\.)example\.(com|org|net)$", path)
            for host in re.findall(r"https?://([\w.-]+)", text):
                self.assertRegex(host, r"(^|\.)(example\.(com|org|net)|hubapi\.com)$", path)
            for number in re.findall(r"\b\d{3}[ .)-]+\d{3}[ .-]\d{4}\b|\b\d{3}-\d{4}\b", text):
                digits = re.sub(r"\D", "", number)
                self.assertRegex(digits[-7:], r"^55501\d\d$", f"{path}: {number}")

    def test_adapter_code_opens_no_sockets(self):
        """The adapter reads files only; it imports no network client at all."""
        banned = re.compile(r"^\s*(import|from)\s+(socket|ssl|http|urllib\.request|requests|httpx|aiohttp)\b", re.M)
        for path in sorted(self.ROOT.glob("*.py")):
            self.assertIsNone(banned.search(path.read_text(encoding="utf-8")), path.name)


class ReviewFindingTests(TmpMixin, SimpleTestCase):
    """Regressions for the code-review findings on the first version of the adapter."""

    def crawl_copy(self) -> Path:
        return copy_input(FIXTURES / "crawl-small", self.tmp() / "input")

    @staticmethod
    def edit(path: Path, change) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_a_subscription_403_never_copies_the_address(self):
        input_dir = self.crawl_copy()
        shutil.rmtree(input_dir / "crawl" / "subscriptions")
        self.edit(input_dir / "crawl" / "errors.json", lambda d: d["payload"]["results"].append(
            {"method": "GET", "path": "/communication-preferences/v3/status/email/ann@example.com", "status": 403}))
        out, manifest = self.export(input_dir)
        self.assertNotIn("@", json.dumps(manifest["objects"]["consent"]))
        self.assertEqual(reason_codes(manifest)["consent"], {"forbidden"})

    def test_a_recipient_read_twice_is_one_fact(self):
        input_dir = self.crawl_copy()
        page = input_dir / "crawl" / "subscriptions" / "page-0001.json"
        self.edit(page, lambda d: d["payload"]["results"].append(d["payload"]["results"][0]))
        out, _ = self.export(input_dir)
        self.assertTrue(validate_canonical(out).ok)

        def flip(d):
            copy = json.loads(json.dumps(d["payload"]["results"][0]))
            copy["subscriptionStatuses"][0]["status"] = "NOT_SUBSCRIBED"
            d["payload"]["results"].append(copy)

        self.edit(page, flip)
        with self.assertRaisesMessage(AdapterError, "two different statuses"):
            self.export(input_dir)

    def test_consent_is_partial_when_an_address_was_not_read(self):
        input_dir = self.crawl_copy()
        self.edit(input_dir / "crawl" / "subscriptions" / "page-0001.json",
                  lambda d: d["payload"]["results"].pop())
        _, manifest = self.export(input_dir)
        self.assertEqual(reason_codes(manifest)["consent"], {"count_mismatch"})

    def test_history_must_cover_every_live_record(self):
        input_dir = self.crawl_copy()
        self.edit(input_dir / "crawl" / "history" / "deals" / "page-0001.json",
                  lambda d: d["payload"]["results"].pop(0))
        out, manifest = self.export(input_dir)
        self.assertNotIn("history", manifest["objects"]["deal"])
        self.assertNotIn("history", self.records(out, "deals.jsonl")["402"])  # never "no changes"

    def test_batch_read_must_cover_every_live_record(self):
        input_dir = self.crawl_copy()
        self.edit(input_dir / "crawl" / "associations" / "deals-contacts" / "page-0001.json",
                  lambda d: d["request"]["body"]["inputs"].pop(0))
        _, manifest = self.export(input_dir)
        self.assertEqual(reason_codes(manifest)["association"], {"pair_not_exported"})

    def test_close_date_is_a_date_in_the_export_zone(self):
        input_dir = copy_input(FIXTURES / "export-csv", self.tmp() / "input")
        path = input_dir / "export" / "deals.csv"
        path.write_text(path.read_text(encoding="utf-8-sig").replace("2026-11-30 12:00", "2026-11-30 00:00"),
                        encoding="utf-8")
        out, _ = self.export(input_dir, time_zone="Europe/Berlin")
        self.assertEqual(self.records(out, "deals.jsonl")["401"]["data"]["expected_close_date"], "2026-11-30")

    def test_a_mapped_pipeline_id_must_exist(self):
        with self.assertRaisesMessage(AdapterError, "pipeline '1234567'"):
            self.export(FIXTURES / "crawl-small", mapping={"pipelines": {"1234567": {"key": "partner_referral"}}})
        with self.assertRaisesMessage(AdapterError, "stage '9'"):
            self.export(FIXTURES / "crawl-small", mapping={"pipelines": {"0": {"stages": {"9": "gone"}}}})

    def test_csv_trailing_blank_line_is_not_truncation(self):
        input_dir = copy_input(FIXTURES / "export-csv", self.tmp() / "input")
        with (input_dir / "export" / "deals.csv").open("a", encoding="utf-8") as fh:
            fh.write("\r\n")
        _, manifest = self.export(input_dir, time_zone="America/New_York")
        self.assertNotIn("truncated_export", reason_codes(manifest)["deal"])

    def test_csv_owner_names(self):
        input_dir = copy_input(FIXTURES / "export-csv", self.tmp() / "input")
        path = input_dir / "export" / "companies.csv"
        path.write_text(path.read_text(encoding="utf-8-sig").replace("Blair Sample", "Former Person"), encoding="utf-8")
        out, _ = self.export(input_dir, time_zone="America/New_York")
        company = self.records(out, "companies.jsonl")["202"]
        self.assertIsNone(company["data"]["owner_ref"])  # never a name as an id
        self.assertEqual(company["properties"]["hubspot_owner_name"], "Former Person")
        self.edit(input_dir / "export" / "owners.json",
                  lambda d: d["payload"]["results"].append({**d["payload"]["results"][0], "id": "105"}))
        with self.assertRaisesMessage(AdapterError, "matches owners 101, 105"):
            self.export(input_dir, time_zone="America/New_York")

    def test_closed_deal_without_a_won_flag_takes_the_stage(self):
        input_dir = self.crawl_copy()

        def unflag(d):
            del d["payload"]["results"][1]["properties"]["hs_is_closed_won"]  # deal 402, in closedwon

        self.edit(input_dir / "crawl" / "deals" / "page-0001.json", unflag)
        out, _ = self.export(input_dir)
        self.assertEqual(self.records(out, "deals.jsonl")["402"]["data"]["status"], "won")

    def test_bad_values_refuse_instead_of_crashing(self):
        self.assertIsNone(parse_decimal("1,5"))
        self.assertIsNone(parse_decimal("1.200,50"))
        self.assertEqual(parse_decimal("12,000"), "12000")
        input_dir = self.crawl_copy()
        self.edit(input_dir / "crawl" / "pipelines-deals.json",
                  lambda d: d["payload"]["results"][0]["stages"][0].update(displayOrder="first"))
        with self.assertRaisesMessage(AdapterError, "is not a whole number"):
            self.export(input_dir)

    def test_stray_files_in_the_output_are_refused(self):
        out = self.tmp()
        (out / "notes.txt").write_text("x", encoding="utf-8")
        with self.assertRaisesMessage(AdapterError, "not canonical output"):
            HubSpotExportAdapter().export(FIXTURES / "crawl-small", out, mapping=None, time_zone=None)
