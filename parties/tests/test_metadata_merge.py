"""``metadata`` writes through the party API merge (CRM Phase 4, 3.3).

A PATCH used to replace the whole dict, so a client that sent only its own
key, or ``{}``, wiped the syncs' system keys (``{"lead": pk}`` and friends)
and any provider provenance. Writes are now merges, system keys belong to
golden code, and since step 2 only defined ``custom.*`` keys are written
(``core.sidecar_api``; the rest is ignored with a ``Warning`` header).
"""

from __future__ import annotations

from django.test import override_settings

from core.provenance import is_system_metadata_key, merge_metadata
from parties.models import PartyRelationship

from .test_api import BASE, _Base, org, person


def _codes(response):
    """The DRF error codes on ``metadata`` (the body carries messages only)."""
    return [getattr(e, "code", None) for e in response.data.get("metadata", [])]


class MergeHelperTests(_Base):
    def test_rfc7396(self):
        self.assertEqual(
            merge_metadata({"a": 1, "b": {"x": 1, "y": 2}, "c": 3},
                           {"a": None, "b": {"y": None, "z": 3}, "d": [1]}),
            {"b": {"x": 1, "z": 3}, "c": 3, "d": [1]},
        )
        self.assertEqual(merge_metadata(None, {"a": None}), {})

    def test_system_keys(self):
        for key in ("lead", "customer", "backfill", "erased", "_private", "provenance",
                    "provenance.source"):
            self.assertTrue(is_system_metadata_key(key), key)
        for key in ("hubspot.lifecyclestage", "custom.package", "notes"):
            self.assertFalse(is_system_metadata_key(key), key)


PACKAGE = {"party": [{"key": "custom.package", "label": "Package", "type": "string"}]}


@override_settings(GOLDEN_FIELD_DEFINITIONS=PACKAGE)
class PartyMetadataPatchTests(_Base):
    def setUp(self):
        super().setUp()
        self.party = person(self.a, metadata={
            "name_inferred": True, "_import_run": "r1", "hubspot.lifecyclestage": "lead", "note": "x",
        })
        self.url = f"{BASE}parties/{self.party.pk}/"

    def patch(self, metadata):
        return self.client.patch(self.url, {"metadata": metadata}, format="json")

    def test_patch_merges_and_ignores_client_keys(self):
        r = self.patch({"custom.package": "gold", "note": None})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("note", r["Warning"])
        self.party.refresh_from_db()
        self.assertEqual(self.party.metadata, {
            "name_inferred": True, "_import_run": "r1", "hubspot.lifecyclestage": "lead",
            "note": "x", "custom.package": "gold",
        })
        self.assertEqual(r.json()["metadata"], self.party.metadata)
        r = self.patch({"custom.package": None})
        self.party.refresh_from_db()
        self.assertNotIn("custom.package", self.party.metadata)

    def test_empty_patch_wipes_nothing(self):
        before = dict(self.party.metadata)
        self.assertEqual(self.patch({}).status_code, 200)
        self.party.refresh_from_db()
        self.assertEqual(self.party.metadata, before)

    def test_round_trip_of_the_whole_object_is_accepted(self):
        current = self.client.get(self.url).json()["metadata"]
        current["custom.package"] = "y"
        self.assertEqual(self.patch(current).status_code, 200)
        self.party.refresh_from_db()
        self.assertEqual(self.party.metadata["custom.package"], "y")
        self.assertEqual(self.party.metadata["_import_run"], "r1")
        self.assertEqual(self.party.metadata["note"], "x")

    def test_system_keys_cannot_be_set_changed_or_deleted(self):
        for metadata in ({"name_inferred": None}, {"name_inferred": False}, {"_import_run": "evil"},
                         {"lead": "00000000-0000-0000-0000-000000000000"}, {"provenance": "client"}):
            r = self.patch(metadata)
            self.assertEqual(r.status_code, 400, metadata)
            self.assertIn("metadata_key_protected", _codes(r), r.content)
        self.party.refresh_from_db()
        self.assertEqual(self.party.metadata["name_inferred"], True)
        self.assertEqual(self.party.metadata["_import_run"], "r1")
        self.assertNotIn("lead", self.party.metadata)

    def test_metadata_must_be_an_object(self):
        self.assertEqual(self.patch(["a"]).status_code, 400)

    def test_create_refuses_system_keys_and_drops_nulls(self):
        r = self.client.post(f"{BASE}parties/", {
            "kind": "organisation", "display_name": "Planted", "metadata": {"customer": "x"},
        }, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("metadata_key_protected", _codes(r))
        r = self.client.post(f"{BASE}parties/", {
            "kind": "organisation", "display_name": "Clean",
            "metadata": {"custom.package": "a", "b": None, "c": 1},
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["metadata"], {"custom.package": "a"})
        self.assertIn("c", r["Warning"])


class RelationshipMetadataPatchTests(_Base):
    def test_relationship_patch_keeps_every_key(self):
        rel = PartyRelationship.objects.create(
            company=self.a, from_party=person(self.a), to_party=org(self.a), kind="employment",
            source="api", metadata={"_import_run": "r1", "note": "x"},
        )
        r = self.client.patch(f"{BASE}relationships/{rel.pk}/", {"metadata": {"title_src": "csv"}},
                              format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("title_src", r["Warning"])
        rel.refresh_from_db()
        self.assertEqual(rel.metadata, {"_import_run": "r1", "note": "x"})
