"""``custom_fields`` and the ``metadata`` compat write on the staff API
(phase4-fields.md 3.3, section 9): parties, engagements and tasks."""

from __future__ import annotations

from django.test import override_settings

from core.models import AuditLog
from crm import engagements
from crm.models import Engagement, Task
from crm.tests.test_api import CRM, _Api, staff_client
from parties.models import Party, PartyRelationship
from parties.tests.test_api import BASE, _Base, org, person

PARTIES = f"{BASE}parties/"

DEFINITIONS = {
    "party": [
        {"key": "custom.package", "label": "Package", "type": "choice",
         "choices": [["accelerator", "Accelerator"], ["other", "Other"]]},
        {"key": "custom.budget", "label": "Budget", "type": "decimal"},
        {"key": "custom.job_title", "label": "Job title", "type": "string", "pii": True},
    ],
    "engagement": [
        {"key": "custom.ip_status", "label": "IP status", "type": "choice",
         "choices": [["filed", "Filed"], ["none", "None"]]},
    ],
    "task": [{"key": "custom.effort", "label": "Effort", "type": "integer"}],
}


def _codes(response, field="metadata"):
    return [getattr(e, "code", None) for e in response.data.get(field, [])]


@override_settings(GOLDEN_FIELD_DEFINITIONS=DEFINITIONS)
class PartyCustomFieldsTests(_Base):
    def setUp(self):
        super().setUp()
        self.party = person(self.a, metadata={
            "name_inferred": True, "_import_run": "r1", "hubspot.lifecyclestage": "lead",
            "legacy_note": "x", "custom.orphan": "kept",
        })
        self.url = f"{PARTIES}{self.party.pk}/"

    def patch(self, body):
        return self.client.patch(self.url, body, format="json")

    def test_get_shows_defined_custom_keys_only(self):
        Party.objects.filter(pk=self.party.pk).update(metadata={**self.party.metadata, "custom.package": "other"})
        body = self.client.get(self.url).json()
        self.assertEqual(body["custom_fields"], {"custom.package": "other"})
        self.assertIn("hubspot.lifecyclestage", body["metadata"])  # metadata stays readable

    def test_patch_custom_fields_validates_coerces_and_merges(self):
        r = self.patch({"custom_fields": {"custom.package": "accelerator", "custom.budget": 1250}})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["custom_fields"], {"custom.package": "accelerator", "custom.budget": "1250"})
        self.party.refresh_from_db()
        self.assertEqual(self.party.metadata["_import_run"], "r1")
        self.assertEqual(self.party.metadata["hubspot.lifecyclestage"], "lead")
        r = self.patch({"custom_fields": {"custom.budget": None}})
        self.assertEqual(r.json()["custom_fields"], {"custom.package": "accelerator"})

    def test_custom_fields_refusals(self):
        for body, code in (
            ({"custom.nope": 1}, "metadata_key_undefined"),
            ({"custom.package": "gold"}, "metadata_value_invalid"),
            ({"hubspot.x": 1}, "metadata_key_invalid"),
            ({"lead": "x"}, "metadata_key_invalid"),
        ):
            with self.subTest(body=body):
                r = self.patch({"custom_fields": body})
                self.assertEqual(r.status_code, 400, r.content)
                self.assertIn(code, _codes(r, "custom_fields"), r.content)
        r = self.patch({"custom_fields": ["custom.package"]})
        self.assertEqual(r.status_code, 400)

    def test_metadata_custom_keys_go_through_the_same_validator(self):
        r = self.patch({"metadata": {"custom.nope": 1}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("metadata_key_undefined", _codes(r))
        r = self.patch({"metadata": {"custom.package": "other"}})
        self.assertEqual(r.status_code, 200, r.content)
        self.party.refresh_from_db()
        self.assertEqual(self.party.metadata["custom.package"], "other")

    def test_metadata_provider_and_unregistered_keys_are_ignored_with_a_warning(self):
        r = self.patch({"metadata": {"hubspot.lifecyclestage": "customer", "note": "new",
                                     "Bad Key": 1, "custom.package": "other"}})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("299", r["Warning"])
        for key in ("hubspot.lifecyclestage", "note", "Bad Key"):
            self.assertIn(key, r["Warning"])
        self.party.refresh_from_db()
        self.assertEqual(self.party.metadata["hubspot.lifecyclestage"], "lead")
        self.assertNotIn("note", self.party.metadata)
        self.assertEqual(self.party.metadata["custom.package"], "other")

    def test_round_trip_of_the_whole_object_is_accepted_without_a_warning(self):
        current = self.client.get(self.url).json()["metadata"]
        current["custom.package"] = "accelerator"
        r = self.patch({"metadata": current})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.has_header("Warning"))
        self.party.refresh_from_db()
        self.assertEqual(self.party.metadata["custom.orphan"], "kept")  # undefined but unchanged
        self.assertEqual(self.party.metadata["custom.package"], "accelerator")

    def test_a_patch_does_not_write_back_a_stale_sidecar(self):
        from unittest import mock

        from parties.views import PartyViewSet

        stale = Party.objects.get(pk=self.party.pk)
        # A sync drops name_inferred after the request loaded the row.
        Party.objects.filter(pk=self.party.pk).update(
            metadata={k: v for k, v in self.party.metadata.items() if k != "name_inferred"})
        with mock.patch.object(PartyViewSet, "get_object", return_value=stale):
            r = self.patch({"tags": ["renamed"]})
        self.assertEqual(r.status_code, 200, r.content)
        self.party.refresh_from_db()
        self.assertEqual(self.party.tags, ["renamed"])
        self.assertNotIn("name_inferred", self.party.metadata)

    def test_registered_system_keys_still_refuse(self):
        for metadata in ({"name_inferred": False}, {"_import_run": "evil"}, {"lead": "x"}):
            r = self.patch({"metadata": metadata})
            self.assertEqual(r.status_code, 400, metadata)
            self.assertIn("metadata_key_protected", _codes(r))

    def test_a_refused_field_rolls_the_create_back(self):
        before = Party.objects.count()
        r = self.client.post(PARTIES, {"kind": "organisation", "display_name": "Nope",
                                       "custom_fields": {"custom.package": "gold"}}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(Party.objects.count(), before)

    def test_create_with_custom_fields_audits(self):
        r = self.client.post(PARTIES, {"kind": "person", "given_name": "Grace",
                                       "custom_fields": {"custom.job_title": "Admiral"}}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["custom_fields"], {"custom.job_title": "Admiral"})
        entry = AuditLog.objects.get(object_id=r.json()["id"], action="update_fields")
        self.assertEqual(entry.changes["set_pii"], ["custom.job_title"])
        self.assertEqual(entry.changes["source"], "api")
        self.assertEqual(entry.user.user, self.user)

    def test_another_tenants_definition_is_invisible(self):
        b_only = {"party": [{"key": "custom.b_only", "label": "B", "type": "string",
                             "companies": [str(self.b.pk)]}]}
        with override_settings(GOLDEN_FIELD_DEFINITIONS=b_only):
            Party.objects.filter(pk=self.party.pk).update(metadata={"custom.b_only": "leak?"})
            self.assertEqual(self.client.get(self.url).json()["custom_fields"], {})
            r = self.patch({"custom_fields": {"custom.b_only": "x"}})
            self.assertEqual(r.status_code, 400)
            self.assertIn("metadata_key_undefined", _codes(r, "custom_fields"))


class RelationshipCompatTests(_Base):
    def test_relationship_metadata_ignores_client_keys_and_keeps_system_ones(self):
        rel = PartyRelationship.objects.create(
            company=self.a, from_party=person(self.a), to_party=org(self.a), kind="employment",
            source="api", metadata={"_import_run": "r1", "contact": "c1"},
        )
        r = self.client.patch(f"{BASE}relationships/{rel.pk}/", {"metadata": {"title_src": "csv"}},
                              format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("title_src", r["Warning"])
        rel.refresh_from_db()
        self.assertEqual(rel.metadata, {"_import_run": "r1", "contact": "c1"})
        r = self.client.patch(f"{BASE}relationships/{rel.pk}/", {"metadata": {"custom.x": 1}}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("metadata_key_undefined", _codes(r))


@override_settings(GOLDEN_FIELD_DEFINITIONS=DEFINITIONS)
class CrmCustomFieldsTests(_Api):
    def test_engagement_create_and_patch(self):
        r = self.client.post(f"{CRM}engagements/", {
            "title": "Deal", "party": str(self.org.pk), "custom_fields": {"custom.ip_status": "filed"},
        }, format="json")
        body = self.ok(r, 201)
        self.assertEqual(body["custom_fields"], {"custom.ip_status": "filed"})
        url = f"{CRM}engagements/{body['id']}/"
        r = self.client.patch(url, {"custom_fields": {"custom.ip_status": "bogus"}}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.patch(url, {"metadata": {"backfill": {"step": 9}}}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("metadata_key_protected", _codes(r))
        self.ok(self.client.patch(url, {"custom_fields": {"custom.ip_status": "none"}}, format="json"))
        self.assertEqual(Engagement.objects.get(pk=body["id"]).metadata, {"custom.ip_status": "none"})

    def test_engagement_patch_keeps_system_keys(self):
        eng = engagements.create(company=self.company, party=self.org, title="Old")
        Engagement.objects.filter(pk=eng.pk).update(metadata={"lead": "l1", "backfill": {"step": 1}})
        self.ok(self.client.patch(f"{CRM}engagements/{eng.pk}/", {"metadata": {}}, format="json"))
        self.ok(self.client.patch(f"{CRM}engagements/{eng.pk}/",
                                  {"custom_fields": {"custom.ip_status": "filed"}}, format="json"))
        eng.refresh_from_db()
        self.assertEqual(eng.metadata, {"lead": "l1", "backfill": {"step": 1}, "custom.ip_status": "filed"})

    def test_task_create_and_patch(self):
        body = self.ok(self.client.post(f"{CRM}tasks/", {
            "title": "Call", "custom_fields": {"custom.effort": "3"},
        }, format="json"), 201)
        self.assertEqual(body["custom_fields"], {"custom.effort": 3})
        url = f"{CRM}tasks/{body['id']}/"
        r = self.client.patch(url, {"custom_fields": {"custom.effort": "lots"}}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Task.objects.get(pk=body["id"]).metadata, {"custom.effort": 3})

    def test_owner_team_via_custom_fields_needs_the_reassign_permission(self):
        from core.models import Team

        team = Team.objects.create(company=self.company, key="east", name="East")
        eng = engagements.create(company=self.company, party=self.org, title="Ours")
        url = f"{CRM}engagements/{eng.pk}/"
        r = self.client.patch(url, {"custom_fields": {"custom.owner_team": str(team.pk)}}, format="json")
        self.assertEqual(r.status_code, 403, r.content)
        eng.refresh_from_db()
        self.assertIsNone(eng.owner_team_id)
        boss, _ = staff_client(self.company, "boss", "crm.reassign_engagement")
        r = boss.patch(url, {"custom_fields": {"custom.owner_team": str(team.pk)}}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        eng.refresh_from_db()
        self.assertEqual(eng.owner_team_id, team.pk)

    def test_another_tenant_cannot_write_through_its_own_definition(self):
        scoped = {"engagement": [{"key": "custom.ip_status", "label": "IP", "type": "string",
                                  "companies": [str(self.other_company.pk)]}]}
        with override_settings(GOLDEN_FIELD_DEFINITIONS=scoped):
            eng = engagements.create(company=self.company, party=self.org, title="Ours")
            r = self.client.patch(f"{CRM}engagements/{eng.pk}/",
                                  {"custom_fields": {"custom.ip_status": "x"}}, format="json")
            self.assertEqual(r.status_code, 400)
            self.assertIn("metadata_key_undefined", _codes(r, "custom_fields"))
