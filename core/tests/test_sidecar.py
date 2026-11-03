"""The ``metadata`` sidecar contract: ``core.sidecar`` (phase4-fields.md 3.1-3.4).

Every refusal of 3.1 (keys and namespaces), 3.2 (values and limits) and 3.3
(writes) has a test here; the API surface is in ``test_sidecar_api``.
"""

from __future__ import annotations

import datetime
import uuid
import warnings
from decimal import Decimal

from django.test import TestCase, override_settings

from core import sidecar
from core.models import AuditLog, Company, Team
from core.sidecar import SidecarError, filter_q, read, set_fields, strip
from crm import engagements
from crm.models import Task
from parties.models import Party, PartyRole

COMPANY_A = "00000000-0000-4000-8000-00000000000a"

DEFINITIONS = {
    "party": [
        {"key": "custom.job_title", "label": "Job title", "type": "string", "group": "Profile",
         "pii": True},
        {"key": "custom.notes", "label": "Notes", "type": "text"},
        {"key": "custom.seats", "label": "Seats", "type": "integer", "filterable": True},
        {"key": "custom.budget", "label": "Budget", "type": "decimal", "group": "Qualification"},
        {"key": "custom.first_met", "label": "First met", "type": "date"},
        {"key": "custom.signed_at", "label": "Signed at", "type": "datetime"},
        {"key": "custom.vip", "label": "VIP", "type": "boolean", "filterable": True},
        {"key": "custom.package", "label": "Package", "type": "choice", "filterable": True,
         "group": "Qualification", "choices": [["accelerator", "Accelerator"], ["other", "Other"]]},
        {"key": "custom.channels", "label": "Channels", "type": "multi_choice", "filterable": True,
         "choices": [["email", "Email"], ["phone", "Phone"], ["post", "Post"]]},
        {"key": "custom.site", "label": "Site", "type": "url"},
        {"key": "custom.tenant_a_only", "label": "A only", "type": "string",
         "companies": [COMPANY_A]},
        {"key": "hubspot.hs_lead_status", "label": "Lead status", "type": "choice",
         "choices": [["new", "New"], ["open", "Open"]]},
    ],
    "engagement": [
        {"key": "custom.package", "label": "Package", "type": "choice",
         "choices": [["accelerator", "Accelerator"], ["other", "Other"]]},
    ],
    "task": [{"key": "custom.effort", "label": "Effort", "type": "integer"}],
}


def codes(exc) -> dict:
    return {key: code for key, (code, _) in exc.errors.items()}


@override_settings(GOLDEN_FIELD_DEFINITIONS=DEFINITIONS)
class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a = Company.objects.create(pk=uuid.UUID(COMPANY_A), name="Tenant A")
        cls.b = Company.objects.create(name="Tenant B")

    def setUp(self):
        self.party = Party.objects.create(company=self.a, kind="person", given_name="Ada",
                                          family_name="L", display_name="Ada L", origin="erp",
                                          metadata={"name_inferred": True})

    def refused(self, values, *, obj=None, source="api"):
        with self.assertRaises(SidecarError) as ctx:
            set_fields(obj or self.party, values, source=source)
        return codes(ctx.exception)


class KeyRefusalTests(_Base):
    """3.1: who may write which namespace."""

    def test_keys_that_break_the_rule(self):
        for key in ("Custom.x", "custom.", "a.b.c", "custom.X", "has space", "", "x" * 65):
            with self.subTest(key=key):
                self.assertEqual(self.refused({key: "v"}), {key: sidecar.KEY_INVALID})
        self.assertEqual(self.refused({7: "v"}), {"7": sidecar.KEY_INVALID})

    def test_personal_data_in_a_key_name(self):
        self.assertEqual(self.refused({"hubspot.x_5551234567": 1}, source="import:hubspot"),
                         {"hubspot.x_5551234567": sidecar.KEY_INVALID})

    def test_system_keys_are_refused(self):
        for key in ("name_inferred", "lead", "customer", "anything_undotted", "_private",
                    "provenance", "provenance.source"):
            with self.subTest(key=key):
                self.assertEqual(self.refused({key: "x"}), {key: sidecar.KEY_PROTECTED})
                self.assertEqual(self.refused({key: None}, source="import:hubspot"),
                                 {key: sidecar.KEY_PROTECTED})
        self.party.refresh_from_db()
        self.assertEqual(self.party.metadata, {"name_inferred": True})

    def test_provider_keys_need_an_import_or_plugin_source(self):
        for source in ("api", "staff", "erp", "", "manual"):
            with self.subTest(source=source):
                self.assertEqual(self.refused({"hubspot.hs_next_step": "call"}, source=source),
                                 {"hubspot.hs_next_step": sidecar.KEY_PROVIDER})
        set_fields(self.party, {"hubspot.hs_next_step": "call"}, source="import:hubspot")
        set_fields(self.party, {"clickup.x": 1}, source="plugin:clickup")
        self.party.refresh_from_db()
        self.assertEqual(self.party.metadata["hubspot.hs_next_step"], "call")
        self.assertEqual(self.party.metadata["clickup.x"], 1)

    def test_custom_keys_need_a_definition(self):
        self.assertEqual(self.refused({"custom.unknown": "x"}), {"custom.unknown": sidecar.KEY_UNDEFINED})

    def test_definitions_are_per_tenant(self):
        set_fields(self.party, {"custom.tenant_a_only": "yes"}, source="api")
        other = Party.objects.create(company=self.b, kind="organisation", display_name="B Ltd", origin="erp")
        self.assertEqual(self.refused({"custom.tenant_a_only": "yes"}, obj=other),
                         {"custom.tenant_a_only": sidecar.KEY_UNDEFINED})

    def test_objects_without_definitions_take_no_custom_keys(self):
        role = PartyRole.objects.create(party=self.party, role="partner", valid_from=datetime.date(2026, 1, 1))
        self.assertEqual(self.refused({"custom.package": "other"}, obj=role),
                         {"custom.package": sidecar.KEY_UNDEFINED})

    def test_a_definition_on_one_object_does_not_define_another(self):
        task = Task.objects.create(company=self.a, title="Call", kind="todo")
        self.assertEqual(self.refused({"custom.package": "other"}, obj=task),
                         {"custom.package": sidecar.KEY_UNDEFINED})

    def test_every_refused_key_is_reported_and_nothing_is_written(self):
        result = self.refused({"custom.vip": "maybe", "lead": "x", "custom.nope": 1, "custom.seats": 3})
        self.assertEqual(result, {"custom.vip": sidecar.VALUE_INVALID, "lead": sidecar.KEY_PROTECTED,
                                  "custom.nope": sidecar.KEY_UNDEFINED})
        self.party.refresh_from_db()
        self.assertNotIn("custom.seats", self.party.metadata)

    def test_non_sidecar_models_are_refused(self):
        with self.assertRaises(TypeError):
            set_fields(self.a, {"custom.x": 1}, source="api")


class ValueTests(_Base):
    """3.2: values, encodings and limits."""

    def ok(self, values, source="api"):
        set_fields(self.party, values, source=source)
        self.party.refresh_from_db()
        return self.party.metadata

    def test_typed_encodings(self):
        meta = self.ok({
            "custom.seats": "12", "custom.budget": 1250.5, "custom.first_met": datetime.date(2026, 2, 3),
            "custom.signed_at": "2026-02-03T10:00:00+02:00", "custom.vip": True,
            "custom.package": "accelerator", "custom.channels": ["post", "email", "post"],
            "custom.site": "https://example.com/x", "custom.notes": "line one\nline two",
        })
        self.assertEqual(meta["custom.seats"], 12)
        self.assertEqual(meta["custom.budget"], "1250.5")
        self.assertEqual(meta["custom.first_met"], "2026-02-03")
        self.assertEqual(meta["custom.signed_at"], "2026-02-03T08:00:00Z")
        self.assertIs(meta["custom.vip"], True)
        self.assertEqual(meta["custom.channels"], ["email", "post"])  # definition order, no repeats
        self.assertEqual(self.ok({"custom.budget": Decimal("1250.00")})["custom.budget"], "1250.00")

    def test_values_a_definition_refuses(self):
        cases = {
            "custom.seats": [1.5, True, "twelve", 2 ** 60],
            "custom.budget": ["abc", "NaN", "Infinity", True, [1]],
            "custom.first_met": ["03/02/2026", "2026-02-30", datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)],
            "custom.signed_at": ["2026-02-03T10:00:00", "yesterday", datetime.datetime(2026, 1, 1)],
            "custom.vip": ["true", 1],
            "custom.package": ["Accelerator", "gold", ["other"]],
            "custom.channels": ["email", ["fax"]],
            "custom.site": ["not a url", "ftp://example.com"],
            "custom.job_title": [5, "two\nlines", "x" * 2001],
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.refused({key: value}), {key: sidecar.VALUE_INVALID})

    def test_null_and_blank_remove_the_key(self):
        self.ok({"custom.vip": True, "custom.job_title": "CTO", "custom.channels": ["email"]})
        meta = self.ok({"custom.vip": None, "custom.job_title": "", "custom.channels": []})
        for key in ("custom.vip", "custom.job_title", "custom.channels"):
            self.assertNotIn(key, meta)
        self.assertEqual(meta, {"name_inferred": True})

    def test_undefined_provider_values_follow_the_generic_rules(self):
        source = "import:hubspot"
        self.ok({"hubspot.a": "x", "hubspot.b": 3, "hubspot.c": [1, "two", False]}, source=source)
        for value, code in (
            ({"nested": 1}, sidecar.VALUE_INVALID),
            (1.5, sidecar.VALUE_INVALID),
            ([[1]], sidecar.VALUE_INVALID),
            ([{"a": 1}], sidecar.VALUE_INVALID),
            (list(range(101)), sidecar.TOO_LARGE),
            ("x" * 2001, sidecar.TOO_LARGE),
            (2 ** 53 + 1, sidecar.TOO_LARGE),
        ):
            with self.subTest(value=str(value)[:30]):
                self.assertEqual(self.refused({"hubspot.d": value}, source=source), {"hubspot.d": code})

    def test_a_provider_key_with_a_definition_is_coerced(self):
        self.assertEqual(self.refused({"hubspot.hs_lead_status": "closed"}, source="import:hubspot"),
                         {"hubspot.hs_lead_status": sidecar.VALUE_INVALID})
        self.assertEqual(self.ok({"hubspot.hs_lead_status": "open"}, source="import:hubspot")
                         ["hubspot.hs_lead_status"], "open")

    def test_at_most_200_keys(self):
        Party.objects.filter(pk=self.party.pk).update(
            metadata={f"hubspot.k{i}": i for i in range(200)})
        self.party.refresh_from_db()
        self.assertEqual(self.refused({"hubspot.one_more": 1}, source="import:hubspot"),
                         {"__all__": sidecar.TOO_LARGE})
        # Changing or removing a key of a full sidecar is still allowed.
        self.ok({"hubspot.k0": None, "hubspot.k1": 99}, source="import:hubspot")

    def test_at_most_32_kb(self):
        values = {f"hubspot.k{i}": "x" * 2000 for i in range(17)}
        self.assertEqual(self.refused(values, source="import:hubspot"), {"__all__": sidecar.TOO_LARGE})
        self.party.refresh_from_db()
        self.assertEqual(self.party.metadata, {"name_inferred": True})


class WriteTests(_Base):
    """3.3: merge, lock, audit, routing of promoted keys."""

    def test_merges_onto_the_stored_row_not_the_stale_instance(self):
        stale = Party.objects.get(pk=self.party.pk)
        # Another writer (a sync) adds a system key after ``stale`` was read.
        Party.objects.filter(pk=self.party.pk).update(metadata={"name_inferred": True, "seed": "s"})
        set_fields(stale, {"custom.vip": True}, source="api")
        self.party.refresh_from_db()
        self.assertEqual(self.party.metadata, {"name_inferred": True, "seed": "s", "custom.vip": True})
        self.assertEqual(stale.metadata, self.party.metadata)

    def test_one_audit_row_with_pii_values_omitted(self):
        profile_user = None
        set_fields(self.party, {"custom.job_title": "CTO", "custom.vip": True}, source="api", by=profile_user)
        entry = AuditLog.objects.get(object_id=self.party.pk, action="update_fields")
        self.assertEqual(entry.model_name, "parties.Party")
        self.assertEqual(entry.changes["set"], {"custom.vip": True})
        self.assertEqual(entry.changes["set_pii"], ["custom.job_title"])
        self.assertNotIn("CTO", str(entry.changes))
        set_fields(self.party, {"custom.vip": None}, source="api")
        self.assertEqual(AuditLog.objects.filter(object_id=self.party.pk).count(), 2)
        self.assertEqual(AuditLog.objects.filter(object_id=self.party.pk).order_by("-created_at")
                         .first().changes["removed"], ["custom.vip"])

    def test_provider_values_are_not_audited_without_a_non_pii_definition(self):
        set_fields(self.party, {"hubspot.mobilephone": "+12125550143", "hubspot.hs_lead_status": "new"},
                   source="import:hubspot")
        entry = AuditLog.objects.get(object_id=self.party.pk, action="update_fields")
        self.assertNotIn("+12125550143", str(entry.changes))
        self.assertEqual(entry.changes["set"], {"hubspot.hs_lead_status": "new"})
        self.assertEqual(entry.changes["set_pii"], ["hubspot.mobilephone"])

    def test_an_orphaned_custom_key_can_be_removed_not_set(self):
        Party.objects.filter(pk=self.party.pk).update(metadata={"custom.gone": "x"})
        self.party.refresh_from_db()
        self.assertEqual(self.refused({"custom.gone": "y"}), {"custom.gone": sidecar.KEY_UNDEFINED})
        set_fields(self.party, {"custom.gone": None}, source="api")
        self.party.refresh_from_db()
        self.assertEqual(self.party.metadata, {})

    def test_a_no_op_write_saves_and_audits_nothing(self):
        set_fields(self.party, {"custom.vip": True}, source="api")
        before = Party.objects.get(pk=self.party.pk).updated_at
        plan = set_fields(self.party, {"custom.vip": True, "custom.seats": None}, source="api")
        self.assertTrue(plan.empty)
        self.assertEqual(Party.objects.get(pk=self.party.pk).updated_at, before)
        self.assertEqual(AuditLog.objects.filter(object_id=self.party.pk).count(), 1)

    def test_unsaved_objects_are_refused(self):
        with self.assertRaises(ValueError):
            set_fields(Party(company=self.a, kind="person"), {"custom.vip": True}, source="api")

    def _routed(self, obj, values):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            plan = set_fields(obj, values, source="api")
        self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))
        return plan

    def test_step_4_promoted_keys_route_to_the_real_columns(self):
        team = Team.objects.create(company=self.a, key="east", name="East")
        self._routed(self.party, {"custom.lifecycle_stage": "customer", "custom.contact_type": None,
                                  "custom.owner_team": str(team.pk), "custom.vip": True})
        self.party.refresh_from_db()
        self.assertEqual((self.party.lifecycle_stage, self.party.owner_team_id), ("customer", team.pk))
        self.assertIsNotNone(self.party.lifecycle_stage_changed_at)
        self.assertEqual(self.party.contact_type, "")
        self.assertIs(self.party.metadata["custom.vip"], True)
        for key in ("custom.lifecycle_stage", "custom.contact_type", "custom.owner_team"):
            self.assertNotIn(key, self.party.metadata)
        entry = AuditLog.objects.filter(object_id=self.party.pk, action="update_fields").get()
        self.assertEqual(entry.changes["routed"], ["contact_type", "lifecycle_stage", "owner_team"])
        eng = engagements.create(company=self.a, title="Deal")
        self._routed(eng, {"custom.owner_team": str(team.pk)})
        eng.refresh_from_db()
        self.assertEqual(eng.owner_team_id, team.pk)

    def test_routed_values_go_through_the_models_validation(self):
        other_team = Team.objects.create(company=self.b, key="west", name="West")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for values in ({"custom.lifecycle_stage": "nonsense"}, {"custom.contact_type": "nope"},
                           {"custom.owner_team": str(other_team.pk)}, {"custom.owner_team": "not-a-uuid"},
                           {"custom.lifecycle_stage": ["customer"]}):
                with self.subTest(values=values):
                    key = next(iter(values))
                    self.assertEqual(self.refused({**values, "custom.vip": True}),
                                     {key: sidecar.VALUE_INVALID})
        self.party.refresh_from_db()
        self.assertEqual((self.party.lifecycle_stage, self.party.owner_team_id), ("", None))
        self.assertNotIn("custom.vip", self.party.metadata)  # nothing written

    def test_promoted_keys_route_to_the_column_then_are_refused(self):
        eng = engagements.create(company=self.a, title="Deal")
        sidecar.PROMOTED_KEYS["engagement"]["custom.legacy_title"] = "title"
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                plan = set_fields(eng, {"custom.legacy_title": "Renamed"}, source="api")
            self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))
            self.assertEqual(plan.deprecated, ["custom.legacy_title"])
            eng.refresh_from_db()
            self.assertEqual(eng.title, "Renamed")
            self.assertNotIn("custom.legacy_title", eng.metadata)
        finally:
            sidecar.PROMOTED_KEYS["engagement"].pop("custom.legacy_title")
        sidecar.RETIRED_KEYS["engagement"] = frozenset({"custom.legacy_title"})
        try:
            self.assertEqual(self.refused({"custom.legacy_title": "x"}, obj=eng),
                             {"custom.legacy_title": sidecar.KEY_PROMOTED})
        finally:
            sidecar.RETIRED_KEYS["engagement"] = frozenset()

    def test_strip_removes_any_namespace(self):
        set_fields(self.party, {"custom.vip": True, "hubspot.x": 1}, source="import:hubspot")
        self.assertEqual(sorted(strip(self.party, ["custom.vip", "hubspot.x", "name_inferred", "absent"])),
                         ["custom.vip", "hubspot.x", "name_inferred"])
        self.party.refresh_from_db()
        self.assertEqual(self.party.metadata, {})


class ReadTests(_Base):
    def test_read_types_values(self):
        set_fields(self.party, {"custom.budget": "10.50", "custom.first_met": "2026-01-02",
                                "custom.signed_at": "2026-01-02T03:04:05Z", "custom.seats": 3},
                   source="api")
        self.assertEqual(read(self.party, "custom.budget"), Decimal("10.50"))
        self.assertEqual(read(self.party, "custom.first_met"), datetime.date(2026, 1, 2))
        self.assertEqual(read(self.party, "custom.signed_at"),
                         datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC))
        self.assertEqual(read(self.party, "custom.seats"), 3)
        self.assertIsNone(read(self.party, "custom.vip"))
        self.assertIs(read(self.party, "name_inferred"), True)

    def test_an_invalid_stored_value_reads_raw_and_logs_once(self):
        Party.objects.filter(pk=self.party.pk).update(metadata={"custom.budget": "lots"})
        self.party.refresh_from_db()
        with self.assertLogs("core.sidecar", "WARNING") as logs:
            self.assertEqual(read(self.party, "custom.budget"), "lots")
            self.assertEqual(read(self.party, "custom.budget"), "lots")
        self.assertEqual(len(logs.records), 1)

    def test_fields_for_lists_every_definition_in_order(self):
        set_fields(self.party, {"custom.package": "other"}, source="api")
        pairs = sidecar.fields_for(self.party)
        self.assertEqual([d.key for d, _ in pairs][:3], ["custom.job_title", "custom.notes", "custom.seats"])
        self.assertIn("custom.tenant_a_only", [d.key for d, _ in pairs])
        self.assertEqual(dict((d.key, v) for d, v in pairs)["custom.package"], "other")
        self.assertEqual([d.key for d, _ in sidecar.fields_for(self.party, group="Qualification")],
                         ["custom.budget", "custom.package"])
        other = Party.objects.create(company=self.b, kind="organisation", display_name="B", origin="erp")
        self.assertNotIn("custom.tenant_a_only", [d.key for d, _ in sidecar.fields_for(other)])
        role = PartyRole.objects.create(party=self.party, role="partner", valid_from=datetime.date(2026, 1, 1))
        self.assertEqual(sidecar.fields_for(role), [])


class FilterTests(_Base):
    """3.4: equality only (the SQLite half; step 3 runs the same on Postgres)."""

    def setUp(self):
        super().setUp()
        self.other = Party.objects.create(company=self.a, kind="organisation", display_name="O", origin="erp")
        set_fields(self.party, {"custom.package": "accelerator", "custom.vip": True, "custom.seats": 5,
                                "custom.channels": ["email", "post"]}, source="api")
        set_fields(self.other, {"custom.package": "other", "custom.vip": False, "custom.seats": 50,
                                "custom.channels": ["phone"]}, source="api")

    def matches(self, key, value):
        return set(Party.objects.filter(filter_q(Party, key, value)).values_list("pk", flat=True))

    def test_equality(self):
        self.assertEqual(self.matches("custom.package", "accelerator"), {self.party.pk})
        self.assertEqual(self.matches("custom.vip", False), {self.other.pk})
        self.assertEqual(self.matches("custom.seats", 50), {self.other.pk})
        self.assertEqual(self.matches("custom.seats", "5"), {self.party.pk})  # coerced like a write

    def test_multi_choice_contains(self):
        self.assertEqual(self.matches("custom.channels", "post"), {self.party.pk})
        self.assertEqual(self.matches("custom.channels", "phone"), {self.other.pk})
        with self.assertRaises(ValueError):
            filter_q(Party, "custom.channels", "fax")

    def test_a_non_filterable_definition_refuses(self):
        with self.assertRaises(ValueError):
            filter_q(Party, "custom.budget", "10")

    def test_refusals(self):
        for key, value in (("name_inferred", True), ("lead", "x"), ("custom.package", None),
                           ("custom.package", ["accelerator"]), ("custom.package", {"a": 1}),
                           ("custom.seats", 1.5), ("bad key", 1), ("custom.package", "gold")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                filter_q(Party, key, value)
