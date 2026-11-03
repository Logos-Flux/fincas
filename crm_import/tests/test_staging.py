"""Staging and matching (phase5-importer.md 3.1-3.4, 4.3, 4.10): every 3.1
path, every 3.4 case, tenant isolation, erasure, auto-approval and the
constant per-chunk query count."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core.models import Company, ExternalRef, UserProfile
from crm_import import match
from crm_import import stage as staging
from crm_import.models import ImportJob
from parties.erasure import erase_party
from parties.match import find
from unify.models import StagedRow

from .staging_helpers import (
    SOURCE,
    ExportDir,
    company,
    contact,
    deal,
    fake_apply_contacts,
    link_ref,
    make_org,
    make_person,
    owner,
    pipeline,
)


def rows(job, object_type="contact"):
    return {r.raw.get("source_id", f"row{r.row_number}"): r for r in StagedRow.objects.filter(
        staging.job_rows_filter([job.pk], object_type)).order_by("row_number")}


def by_row(job, object_type="contact"):
    return list(StagedRow.objects.filter(staging.job_rows_filter([job.pk], object_type)).order_by("row_number"))


class Base(TestCase):
    def setUp(self):
        self.co = Company.objects.create(name="Tenant A")
        self.other = Company.objects.create(name="Tenant B")

    def run_stage(self, records, *, co=None, **kw):
        export = ExportDir(self, records)
        return staging.stage(co or self.co, export.path, **kw)

    def one(self, record, **kw):
        job = self.run_stage({record["object_type"]: [record]}, **kw)
        return by_row(job, record["object_type"])[0]


class JobLifecycleTests(Base):
    def test_valid_export_stages_every_type_in_step_order(self):
        job = self.run_stage({"contact": [contact(1, emails=["a@example.com"])],
                              "company": [company(10, name="Acme")]})
        self.assertEqual(job.status, ImportJob.Status.REVIEWING)
        self.assertEqual(job.coverage, "complete")
        steps = list(job.job_batches.order_by("step").values_list("object_type", "step"))
        self.assertEqual([t for t, _ in steps], list(staging.STEP_ORDER))
        batch = job.job_batches.get(object_type="contact").batch
        self.assertEqual((batch.source_kind, batch.entity_kind, batch.source_name), ("crm", "crm_contact", SOURCE))
        self.assertEqual(job.report["staging"]["contact"]["rows"], 1)
        self.assertEqual(job.report["staging"]["company"]["by_decision"], {"create": 1})

    def test_invalid_export_stages_nothing(self):
        export = ExportDir(self, {"contact": [contact(1)]})
        (export.path / "contacts.jsonl").write_text("{not json}\n", encoding="utf-8")
        job = staging.stage(self.co, export.path)
        self.assertEqual(job.status, ImportJob.Status.INVALID)
        self.assertFalse(job.report["validation"]["ok"])
        self.assertFalse(job.job_batches.exists())

    def test_staged_row_carries_record_and_hash_without_raw_ref(self):
        rec = contact(1)
        row = self.one(rec)
        self.assertEqual(row.raw, rec)
        moved = dict(rec, raw_ref="crawl/contacts/page-0009.json#/results/1")
        self.assertEqual(row.content_hash, match.staged_hash(moved))

    def test_property_labels_kept_on_the_job(self):
        from crm_import.canonical import make_record

        pdef = make_record("property_def", source=SOURCE, source_id="contact/lead_type", data={
            "object_type": "contact", "name": "lead_type", "label": "Lead type", "type": "enumeration",
            "field_type": "select", "options": [{"value": "pa", "label": "Patent Attorney"}], "group": None,
            "provider_defined": False,
        })
        job = self.run_stage({"property_def": [pdef]})
        self.assertEqual(job.report["property_labels"]["contact"]["lead_type"]["options"], {"pa": "Patent Attorney"})

    def lead_type_def(self):
        from crm_import.canonical import make_record

        return make_record("property_def", source=SOURCE, source_id="contact/lead_type", data={
            "object_type": "contact", "name": "lead_type", "label": "Lead type", "type": "string",
            "field_type": "text", "options": [], "group": None, "provider_defined": False,
        })

    def test_mapping_is_frozen_onto_the_job(self):
        mapping = {"source": SOURCE, "contact": {"properties": {"lead_type": "sidecar"}}}
        job = self.run_stage({"contact": [contact(1)], "property_def": [self.lead_type_def()]}, mapping=mapping)
        self.assertEqual((job.status, job.mapping), (ImportJob.Status.REVIEWING, mapping))

    def test_mapping_with_provider_tables_is_frozen_as_is(self):
        # [ticket.properties] is the provider's table: the adapter checked it.
        mapping = {"ticket": {"properties": {"hs_pipeline": "sidecar"}}, "tickets": {"relationship_kind": "partnership"}}
        job = self.run_stage({"contact": [contact(1)]}, mapping=mapping)
        self.assertEqual((job.status, job.mapping), (ImportJob.Status.REVIEWING, mapping))

    def test_mapping_for_another_source_is_refused(self):
        job = self.run_stage({"contact": [contact(1)]}, mapping={"source": "examplecrm:1"})
        self.assertEqual(job.status, ImportJob.Status.INVALID)

    def test_command_stages_and_prints_json(self):
        import io
        import json

        from django.core.management import call_command

        export = ExportDir(self, {"contact": [contact(1, emails=["a@example.com"])]})
        out = io.StringIO()
        call_command("crm_import", "stage", "--company", str(self.co.pk), "--dir", str(export.path), "--json",
                     stdout=out)
        report = json.loads(out.getvalue())
        self.assertEqual(report["status"], "reviewing")
        self.assertEqual(report["report"]["staging"]["contact"]["by_state"], {"approved": 1})

    def test_command_refuses_an_invalid_export(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        export = ExportDir(self, {"contact": [contact(1)]})
        (export.path / "manifest.json").unlink()
        with self.assertRaises(CommandError):
            call_command("crm_import", "stage", "--company", str(self.co.pk), "--dir", str(export.path))

    def test_second_staging_of_a_busy_source_refuses(self):
        ImportJob.objects.create(company=self.co, source=SOURCE, provider="examplecrm",
                                 status=ImportJob.Status.APPLYING)
        with self.assertRaises(staging.StagingError):
            self.run_stage({"contact": [contact(1)]})

    def test_another_tenant_busy_does_not_block(self):
        ImportJob.objects.create(company=self.other, source=SOURCE, provider="examplecrm",
                                 status=ImportJob.Status.STAGING)
        self.assertEqual(self.run_stage({"contact": [contact(1)]}).status, ImportJob.Status.REVIEWING)

    def test_previous_job_of_another_tenant_or_source_refused(self):
        theirs = ImportJob.objects.create(company=self.other, source=SOURCE, provider="examplecrm",
                                          status=ImportJob.Status.APPLIED)
        with self.assertRaises(staging.StagingError):
            self.run_stage({"contact": [contact(1)]}, previous_job=theirs)
        other_source = ImportJob.objects.create(company=self.co, source="examplecrm:9", provider="examplecrm",
                                                status=ImportJob.Status.APPLIED)
        with self.assertRaises(staging.StagingError):
            self.run_stage({"contact": [contact(1)]}, previous_job=other_source)

    def test_resume_continues_after_last_committed_row(self):
        export = ExportDir(self, {"contact": [contact(i, given=f"P{i}") for i in range(1, 6)]})
        job = staging.stage(self.co, export.path)
        batch = job.job_batches.get(object_type="contact").batch
        batch.rows.filter(row_number__gte=4).delete()  # an interrupted run committed rows 1-3
        batch.rows.filter(row_number=3).update(mapping_state="unmapped", proposed_mapping={})
        job.status = ImportJob.Status.STAGING
        job.save()
        staging.stage_job(job, export.path)
        got = list(batch.rows.order_by("row_number").values_list("row_number", "mapping_state"))
        self.assertEqual([n for n, _ in got], [1, 2, 3, 4, 5])
        self.assertTrue(all(state == "approved" for _, state in got))


class ResumeTests(Base):
    def interrupted(self):
        export = ExportDir(self, {"contact": [contact(i) for i in range(1, 4)]})
        job = staging.stage(self.co, export.path)
        job.status = ImportJob.Status.STAGING
        job.save()
        return job, export

    def test_resume_of_a_finished_or_invalid_job_refuses(self):
        job, export = self.interrupted()
        for status in (ImportJob.Status.INVALID, ImportJob.Status.APPLYING, ImportJob.Status.APPLIED):
            job.status = status
            job.save()
            with self.assertRaises(staging.StagingError):
                staging.stage_job(job, export.path, resume=True)

    def test_resume_from_another_directory_refuses(self):
        job, _export = self.interrupted()
        other = ExportDir(self, {"contact": [contact(9)]})
        with self.assertRaises(staging.StagingError):
            staging.stage_job(job, other.path, resume=True)

    def test_resume_refuses_files_edited_after_staging_started(self):
        job, export = self.interrupted()
        path = export.path / "contacts.jsonl"
        path.write_text(path.read_text(encoding="utf-8").replace('"Ada"', '"Eve"'), encoding="utf-8")
        with self.assertRaises(staging.StagingError):
            staging.stage_job(job, export.path, resume=True)

    def test_resume_from_the_same_directory(self):
        job, export = self.interrupted()
        self.assertEqual(staging.stage_job(job, export.path, resume=True).status, "reviewing")

    def test_losing_the_source_lock_cancels_the_new_job(self):
        ImportJob.objects.create(company=self.co, source=SOURCE, provider="examplecrm",
                                 status=ImportJob.Status.STAGING)
        with self.assertRaises(staging.StagingError):
            self.run_stage({"contact": [contact(1)]})
        self.assertTrue(ImportJob.objects.filter(company=self.co, status=ImportJob.Status.CANCELLED).exists())


class ExternalRefPathTests(Base):
    """3.1 step 1."""

    def test_ref_resolves_by_id_and_by_alias(self):
        party = make_person(self.co, emails=["a@example.com"])
        link_ref(self.co, party, "18")
        row = self.one(contact(51, aliases=["18"], emails=["a@example.com"]))
        self.assertEqual(row.proposed_mapping["target"], {"model": "parties.party", "id": str(party.pk)})
        self.assertEqual(row.proposed_mapping["rule"], "external_ref")

    def test_ref_to_a_merged_party_follows_the_survivor(self):
        survivor = make_person(self.co, given="Ada")
        loser = make_person(self.co, given="Ada")
        loser.merged_into = survivor
        loser.save(update_fields=["merged_into"])
        link_ref(self.co, loser, "51")
        row = self.one(contact(51))
        self.assertEqual(row.proposed_mapping["target"]["id"], str(survivor.pk))

    def test_ref_of_another_tenant_is_not_resolved(self):
        theirs = make_person(self.other, emails=["a@example.com"])
        link_ref(self.other, theirs, "51")
        row = self.one(contact(51, emails=["a@example.com"]))
        self.assertEqual(row.proposed_mapping["decision"], "create")

    def test_ref_whose_target_is_gone_is_a_review_item(self):
        party = make_person(self.co)
        ExternalRef.objects.create(company=self.co, source=SOURCE, object_type="contact", source_id="51",
                                   content_type=link_ref(self.co, party, "x").content_type,
                                   object_id="00000000-0000-0000-0000-000000000001")
        row = self.one(contact(51))
        self.assertIsNone(row.proposed_mapping["decision"])
        self.assertIn(match.FLAG_TARGET_MISSING, row.proposed_mapping["flags"])
        self.assertEqual(row.mapping_state, "proposed")

    def test_deals_are_create_or_externalref_only(self):
        job = self.run_stage({"pipeline": [pipeline("p1")], "deal": [deal("d1")]})
        self.assertEqual(by_row(job, "deal")[0].proposed_mapping["decision"], "create")


class ErasureTests(Base):
    """3.1 steps 1 and 5: an erased person is never matched or re-created."""

    def assertErased(self, row, source_id=None):
        from core.provenance import erased_source_id

        self.assertEqual(row.mapping_state, "na")
        self.assertEqual(row.proposed_mapping["reason"], "erased")
        self.assertTrue(row.raw["erased"])
        self.assertLessEqual(set(row.raw), {"erased", "source_key"})  # nothing about the person
        self.assertEqual(row.mapped_values, {})
        if source_id:
            self.assertEqual(row.raw["source_key"], erased_source_id(source_id))

    def test_opted_out_then_erased_address_still_counts_as_erased(self):
        from parties.erasure import erased_address
        from parties.models import Suppression

        party = make_person(self.co, emails=["gone@example.com"])
        Suppression.objects.create(company=self.co, channel="email", reason="opt_out", source="erp",
                                   address_hash=__import__("parties.consent", fromlist=["x"]).address_hash(
                                       "gone@example.com"))
        erase_party(party, reason="request")
        self.assertTrue(erased_address(self.co.pk, "gone@example.com"))
        sup = Suppression.objects.get(company=self.co, channel="email")
        self.assertEqual((sup.reason, sup.source_detail), ("opt_out", "erasure"))  # reason kept, erasure marked
        self.assertErased(self.one(contact(77, emails=["gone@example.com"])))

    def test_communication_with_an_erased_participant(self):
        from crm_import.canonical import make_record

        party = make_person(self.co, emails=["gone@example.com"])
        erase_party(party, reason="request")
        note = make_record("communication", source=SOURCE, source_id="n1", data={
            "kind": "email", "direction": "inbound", "status": None, "subject": "Hi", "body_text": "hello",
            "body_html": None, "summary": None, "occurred_at": "2026-01-01T00:00:00Z", "ended_at": None,
            "duration_seconds": None, "owner_ref": None,
            "participants": [{"role": "from", "address": "gone@example.com", "name": "Gone"}],
            "target_refs": [], "raw": {"schema_version": 1, "provider": "examplecrm", "payload": {}},
        })
        self.assertErased(self.one(note))

    def test_company_loses_an_erased_phone_and_is_flagged(self):
        party = make_person(self.co, phones=["+12025550143"])
        erase_party(party, reason="request")
        rec = company(10, name="Sole Trader")
        rec["data"]["phones"] = [{"raw": "(202) 555-0143", "kind": "phone", "primary": True},
                                 {"raw": "(202) 555-0199", "kind": "phone", "primary": False}]
        row = self.one(rec)
        self.assertEqual(row.proposed_mapping["decision"], "create")
        self.assertIn(match.FLAG_ERASED_POINT_REMOVED, row.proposed_mapping["flags"])
        self.assertEqual([p["raw"] for p in row.raw["data"]["phones"]], ["(202) 555-0199"])
        self.assertEqual(row.mapping_state, "proposed")

    def test_erasure_after_staging_scrubs_and_closes_the_row(self):
        party = make_person(self.co, given="Ada", family="Example")
        link_ref(self.co, party, "51")
        row = self.one(contact(51, family="Other", phones=["(202) 555-0177"]))  # a conflict, waiting for review
        self.assertEqual(row.mapping_state, "proposed")
        erase_party(party, reason="request")  # the staged phone is formatted unlike anything golden holds
        row.refresh_from_db()
        self.assertEqual((row.mapping_state, row.raw, row.proposed_mapping["reason"]), ("na", {"erased": True}, "erased"))
        with self.assertRaises(staging.DecisionError):
            row.mapping_state = "proposed"
            staging.approve_row(row, "reviewer", keep=["family_name"])

    def test_an_already_scrubbed_unmapped_row_is_closed_not_crashed(self):
        job = self.run_stage({"contact": [contact(1), contact(2)]})
        batch = job.job_batches.get(object_type="contact").batch
        batch.rows.filter(row_number=2).update(raw={"erased": True}, mapping_state="unmapped", proposed_mapping={})
        staging.match_batch(job, batch, "contact")
        row = batch.rows.get(row_number=2)
        self.assertEqual((row.mapping_state, row.raw), ("na", {"erased": True}))

    def test_erased_ref_makes_the_row_na_and_drops_raw(self):
        party = make_person(self.co, emails=["gone@example.com"])
        link_ref(self.co, party, "51")
        erase_party(party, reason="request")
        self.assertTrue(ExternalRef._base_manager.filter(company=self.co, metadata__erased=True).exists())
        row = self.one(contact(51, emails=["new@example.com"], given="Ada"))
        self.assertEqual(row.mapping_state, "na")
        self.assertErased(row, "51")
        self.assertEqual(row.proposed_mapping, {"decision": "na", "reason": "erased"})
        self.assertEqual(row.mapped_values, {})

    def test_erased_ref_via_alias(self):
        party = make_person(self.co)
        link_ref(self.co, party, "18")
        erase_party(party, reason="request")
        row = self.one(contact(51, aliases=["18"]))
        self.assertErased(row)

    def test_erased_address_without_a_ref(self):
        party = make_person(self.co, emails=["gone@example.com"])
        erase_party(party, reason="request")
        row = self.one(contact(77, emails=["Gone@Example.com"]))
        self.assertErased(row)

    def test_erased_phone_without_a_ref(self):
        party = make_person(self.co, phones=["+12025550143"])
        erase_party(party, reason="request")
        row = self.one(contact(77, phones=["(202) 555-0143"]))
        self.assertErased(row)

    def test_consent_fact_about_an_erased_address(self):
        from crm_import.canonical import make_record

        party = make_person(self.co, emails=["gone@example.com"])
        erase_party(party, reason="request")
        fact = make_record("consent", source=SOURCE, source_id="c1", data={
            "address": "gone@example.com", "channel": "email", "purpose": "marketing", "state": "withdrawn",
            "suppress": None, "occurred_at": "2026-01-01T00:00:00Z", "subscription": None, "evidence": None,
        })
        row = self.one(fact)
        self.assertErased(row)

    def test_erased_party_is_never_a_match_candidate(self):
        party = make_person(self.co, emails=["gone@example.com"], given="Ada", family="Example")
        erase_party(party, reason="request")
        # A different address, same name: nothing points at the erased party.
        row = self.one(contact(77, emails=["fresh@example.com"]))
        self.assertEqual(row.proposed_mapping["decision"], "create")
        self.assertNotIn(str(party.pk), str(row.proposed_mapping))

    def test_erasure_in_another_tenant_does_not_apply(self):
        party = make_person(self.other, emails=["gone@example.com"])
        erase_party(party, reason="request")
        row = self.one(contact(77, emails=["gone@example.com"]))
        self.assertEqual(row.proposed_mapping["decision"], "create")


class ExactMatchTests(Base):
    """3.1 step 2: Phase 2's exact matcher."""

    def test_email_links_at_r1_and_auto_approves(self):
        party = make_person(self.co, emails=["ada@example.com"])
        row = self.one(contact(1, emails=["ADA@example.com"]))
        p = row.proposed_mapping
        self.assertEqual((p["decision"], p["rule"], row.mapping_confidence), ("link", "R1", Decimal("0.950")))
        self.assertEqual(p["evidence"], [{"rule": "R1", "value": "a…@example.com"}])
        self.assertEqual(row.mapping_state, "approved")
        self.assertEqual(row.mapped_values["target"]["id"], str(party.pk))
        self.assertEqual(row.mapped_values["external_ref"],
                         {"source": SOURCE, "object_type": "contact", "source_id": "1"})

    def test_email_with_a_different_family_name_is_flagged_not_approved(self):
        make_person(self.co, given="Ada", family="Example", emails=["info@example.com"])
        row = self.one(contact(1, given="Bob", family="Other", emails=["info@example.com"]))
        self.assertEqual(row.proposed_mapping["decision"], "link")
        self.assertIn(match.FLAG_NAME_MISMATCH, row.proposed_mapping["flags"])
        self.assertEqual(row.mapping_state, "proposed")

    def test_same_family_other_given_name_on_a_family_mailbox_is_flagged(self):
        make_person(self.co, given="John", family="Smith", emails=["smiths@example.com"])
        row = self.one(contact(1, given="Mary", family="Smith", emails=["smiths@example.com"]))
        self.assertIn(match.FLAG_NAME_MISMATCH, row.proposed_mapping["flags"])
        self.assertEqual(row.mapping_state, "proposed")

    def test_an_initial_is_not_a_mismatch(self):
        make_person(self.co, given="John", family="Smith", emails=["john@example.com"])
        row = self.one(contact(1, given="J.", family="Smith", emails=["john@example.com"]))
        self.assertEqual((row.proposed_mapping["flags"], row.mapping_state), ([], "approved"))

    def test_role_address_with_a_tag_or_suffix(self):
        for email in ("info+web@acme.example", "sales.uk@acme.example", "finance@acme.example"):
            self.assertTrue(match.is_role_address(email), email)
        self.assertFalse(match.is_role_address("salesforce.fan@acme.example"))

    def test_given_name_only_record_on_another_persons_email_is_flagged(self):
        make_person(self.co, given="Jane", family="Doe", emails=["desk@acme.example"])
        row = self.one(contact(1, given="John", family=None, emails=["desk@acme.example"]))
        self.assertIn(match.FLAG_NAME_MISMATCH, row.proposed_mapping["flags"])
        self.assertEqual(row.mapping_state, "proposed")

    def test_role_address_link_is_flagged(self):
        make_person(self.co, given="Jane", family="Doe", emails=["info@acme.example"])
        row = self.one(contact(1, given=None, family=None, emails=["info@acme.example"]))
        self.assertIn(match.FLAG_ROLE_ADDRESS, row.proposed_mapping["flags"])
        self.assertEqual(row.mapping_state, "proposed")

    def test_phone_and_family_name_is_r2_phone_only(self):
        make_person(self.co, family="Example", phones=["+12025550143"])
        row = self.one(contact(1, family="Example", phones=["202-555-0143"]))
        p = row.proposed_mapping
        self.assertEqual((p["rule"], row.mapping_confidence), ("R2", Decimal("0.850")))
        self.assertIn(match.FLAG_PHONE_ONLY, p["flags"])
        self.assertEqual(row.mapping_state, "proposed")

    def test_phone_alone_is_r3(self):
        make_person(self.co, family="Someone", phones=["+12025550143"])
        row = self.one(contact(1, family="Example", phones=["202-555-0143"]))
        self.assertEqual((row.proposed_mapping["rule"], row.mapping_confidence), ("R3", Decimal("0.600")))

    def test_two_parties_on_one_email_propose_nothing(self):
        a = make_person(self.co, emails=["shared@example.com"])
        b = make_person(self.co, emails=["shared@example.com"])
        row = self.one(contact(1, emails=["shared@example.com"]))
        p = row.proposed_mapping
        self.assertIsNone(p["decision"])
        self.assertEqual(row.mapping_confidence, Decimal("0.000"))
        self.assertIn(match.FLAG_AMBIGUOUS, p["flags"])
        self.assertEqual({c["id"] for c in p["candidates"]}, {str(a.pk), str(b.pk)})

    def test_emails_matching_two_parties_propose_nothing(self):
        make_person(self.co, emails=["one@example.com"])
        make_person(self.co, emails=["two@example.com"])
        row = self.one(contact(1, emails=["one@example.com", "two@example.com"]))
        self.assertIsNone(row.proposed_mapping["decision"])

    def test_email_beats_phone(self):
        by_email = make_person(self.co, emails=["ada@example.com"])
        make_person(self.co, phones=["+12025550143"])
        row = self.one(contact(1, emails=["ada@example.com"], phones=["202-555-0143"]))
        self.assertEqual(row.proposed_mapping["target"]["id"], str(by_email.pk))
        self.assertEqual(len(row.proposed_mapping["candidates"]), 2)
        # The phone points at someone else: competing evidence is never auto-approved.
        self.assertIn(match.FLAG_COMPETING, row.proposed_mapping["flags"])
        self.assertEqual(row.mapping_state, "proposed")

    def test_matches_parties_match_find(self):
        """The bulk matcher gives what parties.match.find gives, record by record."""
        make_person(self.co, emails=["ada@example.com"])
        make_person(self.co, emails=["x@example.com", "y@example.com"])
        make_person(self.co, phones=["+12025550143"])
        make_person(self.co, phones=["+12025550144"])
        make_person(self.co, phones=["+12025550144"])
        cases = [(["ada@example.com"], []), (["y@example.com"], []), ([], ["202-555-0143"]),
                 ([], ["202-555-0144"]), (["nobody@example.com"], [])]
        job = self.run_stage({"contact": [contact(i, family=f"F{i}", emails=e, phones=p)
                                          for i, (e, p) in enumerate(cases, 1)]})
        for row, (emails, phones) in zip(by_row(job), cases):
            found = find(self.co, email=emails[0]) if emails else find(self.co, phone=phones[0])
            p = row.proposed_mapping
            if found.state == "match-1":
                self.assertEqual(p["target"]["id"], str(found.party.pk))
            elif found.state == "match-N":
                self.assertIsNone(p["decision"])
            else:
                self.assertEqual(p["decision"], "create")

    def test_other_tenant_party_is_never_matched(self):
        make_person(self.other, emails=["ada@example.com"])
        self.assertEqual(self.one(contact(1, emails=["ada@example.com"])).proposed_mapping["decision"], "create")

    def test_party_with_a_login_needs_individual_review(self):
        user = User.objects.create_user("ada", "ada@example.com")
        make_person(self.co, emails=["ada@example.com"], user=user)
        row = self.one(contact(1, emails=["ada@example.com"]))
        self.assertIn(match.FLAG_NEEDS_INDIVIDUAL_REVIEW, row.proposed_mapping["flags"])
        self.assertEqual(row.mapping_state, "proposed")

    def test_party_already_holding_another_record_of_the_source(self):
        party = make_person(self.co, emails=["ada@example.com"])
        link_ref(self.co, party, "999")
        row = self.one(contact(1, emails=["ada@example.com"]))
        self.assertIn(match.FLAG_ALREADY_LINKED, row.proposed_mapping["flags"])
        self.assertEqual(row.mapping_state, "proposed")

    def test_two_records_linking_one_party_are_both_flagged(self):
        make_person(self.co, emails=["ada@example.com"])
        job = self.run_stage({"contact": [contact(1, emails=["ada@example.com"]),
                                          contact(2, emails=["ada@example.com"])]})
        for row in by_row(job):
            self.assertEqual(row.proposed_mapping["decision"], "link")
            self.assertIn(match.FLAG_SHARED_TARGET, row.proposed_mapping["flags"])
            self.assertEqual(row.mapping_state, "proposed")


class DedupeEvidenceTests(Base):
    """3.1 steps 3 and 4."""

    def test_name_and_postcode_is_r4(self):
        party = make_person(self.co, given="Ada", family="Example", postcode="20001", country="US")
        row = self.one(contact(1, addresses=[{"postal_code": "20001", "country": "US"}]))
        p = row.proposed_mapping
        self.assertEqual((p["decision"], p["rule"], row.mapping_confidence), ("link", "R4", Decimal("0.500")))
        self.assertEqual(p["target"]["id"], str(party.pk))
        self.assertEqual(p["evidence"], [{"rule": "R4", "value": "same name and postal code"}])

    def test_organisation_name_is_r5(self):
        org = make_org(self.co, "Acme Widgets, Inc.")
        row = self.one(company(10, name="ACME WIDGETS LLC"))
        p = row.proposed_mapping
        self.assertEqual((p["rule"], row.mapping_confidence, p["target"]["id"]), ("R5", Decimal("0.700"), str(org.pk)))

    def test_domain_alone_is_r6_never_bulk_approvable(self):
        make_org(self.co, "Something Else", domain="acme.example")
        row = self.one(company(10, name="Acme", domains=["ACME.example"]))
        self.assertEqual((row.proposed_mapping["rule"], row.mapping_confidence), ("R6", Decimal("0.300")))
        self.assertFalse(match.bulk_approvable(dict(row.proposed_mapping), Decimal("0.99")))

    def test_people_never_match_organisations(self):
        make_org(self.co, "Ada Example")
        self.assertEqual(self.one(contact(1)).proposed_mapping["decision"], "create")

    def test_nothing_creates_at_one_and_auto_approves(self):
        row = self.one(contact(1, emails=["new@example.com"]))
        self.assertEqual((row.proposed_mapping["decision"], row.mapping_confidence), ("create", Decimal("1.000")))
        self.assertEqual(row.mapping_state, "approved")
        self.assertEqual(row.mapped_values["values"]["emails"], ["new@example.com"])

    def test_within_export_duplicates_are_both_created_with_a_hint(self):
        job = self.run_stage({"contact": [contact(1, emails=["twin@example.com"]),
                                          contact(2, emails=["twin@example.com"])]})
        first, second = by_row(job)
        self.assertEqual([first.proposed_mapping["decision"], second.proposed_mapping["decision"]],
                         ["create", "create"])
        self.assertEqual(second.proposed_mapping["possible_duplicate_of"], ["1"])

    def test_archived_record_is_na_unless_included(self):
        self.assertEqual(self.one(contact(1, archived=True)).proposed_mapping["reason"], "archived_in_source")
        row = self.one(contact(2, archived=True), options={"include_archived": True})
        self.assertEqual(row.proposed_mapping["decision"], "create")


class OwnerAndPipelineTests(Base):
    def staff(self, email, company=None):
        user = User.objects.create_user(email.split("@")[0], email, is_staff=True)
        UserProfile.objects.update_or_create(user=user, defaults={"company": company or self.co})
        return user

    def test_owner_email_links_staff_profile(self):
        user = self.staff("olive@example.com")
        row = self.one(owner(101, email="Olive@example.com"))
        self.assertEqual(row.proposed_mapping["target"], {"model": "core.userprofile", "id": str(user.userprofile.pk)})
        self.assertEqual((row.mapping_confidence, row.mapping_state), (Decimal("0.950"), "approved"))

    def test_owner_with_another_family_name_is_flagged(self):
        user = self.staff("olive@example.com")
        user.last_name = "Different"
        user.save()
        row = self.one(owner(101, email="olive@example.com"))
        self.assertIn(match.FLAG_NAME_MISMATCH, row.proposed_mapping["flags"])
        self.assertEqual(row.mapping_state, "proposed")

    def test_archived_owner_is_capped_and_flagged(self):
        self.staff("olive@example.com")
        row = self.one(owner(101, email="olive@example.com", active=False))  # archived owners are kept (2.3)
        self.assertEqual(row.mapping_confidence, Decimal("0.600"))
        self.assertIn(match.FLAG_ARCHIVED_OWNER, row.proposed_mapping["flags"])

    def test_owner_without_match_creates_and_other_tenant_staff_never_match(self):
        self.staff("olive@example.com", company=self.other)
        row = self.one(owner(101, email="olive@example.com"))
        self.assertEqual(row.proposed_mapping["decision"], "create")

    def test_pipeline_key_links_when_stages_are_a_superset(self):
        from crm.models import Pipeline, Stage

        pipe = Pipeline.objects.create(company=self.co, key="referrals", name="Referrals", sort_order=9)
        Stage.objects.create(pipeline=pipe, company=self.co, key="new", name="New", sort_order=0)
        row = self.one(pipeline("p1", key="referrals", stages=("new", "won")))
        self.assertEqual((row.proposed_mapping["decision"], row.mapping_state), ("link", "approved"))
        Stage.objects.create(pipeline=pipe, company=self.co, key="legacy", name="Legacy", sort_order=1)
        row = self.one(pipeline("p1", key="referrals", stages=("new", "won")))
        self.assertIsNone(row.proposed_mapping["decision"])
        self.assertIn(match.FLAG_STAGE_KEYS_DIFFER, row.proposed_mapping["flags"])
        self.assertEqual(row.proposed_mapping["diff"], {"missing_stages": ["legacy"], "new_stages": ["won"]})

    def test_closed_stage_without_outcome_is_flagged(self):
        row = self.one(pipeline("p1", key="partners", stages=("new", "done"), outcomes={"done": "closed"}))
        self.assertIn(match.FLAG_STAGE_OUTCOME_CLOSED, row.proposed_mapping["flags"])
        self.assertEqual(row.mapping_state, "proposed")


class ReimportTests(Base):
    """3.4, one case at a time, against a previous job the fake applier applied."""

    def first_import(self, records):
        job1 = self.run_stage({"contact": records})
        fake_apply_contacts(job1)
        return job1

    def test_unchanged_record_is_na_unchanged(self):
        job1 = self.first_import([contact(1, emails=["a@example.com"])])
        row = self.one(contact(1, emails=["a@example.com"]), previous_job=job1)
        self.assertEqual((row.mapping_state, row.proposed_mapping["reason"]), ("na", "unchanged"))

    def test_source_only_change_updates(self):
        job1 = self.first_import([contact(1, family="Example", properties={"lead_type": "Patent Attorney"})])
        row = self.one(contact(1, family="Exampleton", properties={"lead_type": "Inventor"}), previous_job=job1)
        p = row.proposed_mapping
        self.assertEqual((p["decision"], row.mapping_confidence, row.mapping_state), ("update", Decimal("1.000"),
                                                                                    "approved"))
        self.assertEqual(p["diff"]["fields"], {"family_name": "update", "properties.lead_type": "update"})
        self.assertTrue(p["diff"]["previous_import"])

    def test_staff_edit_since_the_import_is_a_conflict(self):
        from parties.models import Party

        job1 = self.first_import([contact(1, family="Example")])
        Party.objects.filter(company=self.co).update(family_name="Staff-Fixed")
        row = self.one(contact(1, family="Exampleton"), previous_job=job1)
        p = row.proposed_mapping
        self.assertEqual((p["decision"], row.mapping_confidence, row.mapping_state), ("conflict", Decimal("0.000"),
                                                                                    "proposed"))
        self.assertEqual(p["diff"]["fields"], {"family_name": "conflict"})
        self.assertNotIn("Staff-Fixed", str(p))  # field names and states only, never values

    def test_staff_edit_with_unchanged_source_field_is_left_alone(self):
        from parties.models import Party

        job1 = self.first_import([contact(1, family="Example", job_title="CEO")])
        Party.objects.filter(company=self.co).update(family_name="Staff-Fixed")
        row = self.one(contact(1, family="Example", job_title="CTO"), previous_job=job1)
        self.assertEqual(row.proposed_mapping["diff"]["fields"], {"job_title": "update"})

    def test_added_contact_points_added_removed_ones_reported_never_deleted(self):
        job1 = self.first_import([contact(1, emails=["a@example.com", "old@example.com"])])
        row = self.one(contact(1, emails=["a@example.com", "new@example.com"]), previous_job=job1)
        diff = row.proposed_mapping["diff"]
        self.assertEqual(diff["fields"], {"emails": "update"})
        self.assertEqual((diff["added"], diff["removed"]), ({"emails": 1}, {"emails": 1}))
        self.assertEqual(row.mapped_values["values"]["emails"], ["a@example.com", "new@example.com"])

    def test_only_a_removal_writes_nothing(self):
        job1 = self.first_import([contact(1, emails=["a@example.com", "old@example.com"])])
        row = self.one(contact(1, emails=["a@example.com"]), previous_job=job1)
        self.assertEqual((row.mapping_state, row.proposed_mapping["reason"]), ("na", "unchanged"))
        self.assertEqual(row.proposed_mapping["diff"]["removed"], {"emails": 1})

    def test_records_missing_from_the_new_export_are_reported(self):
        job1 = self.first_import([contact(1), contact(2), contact(3)])
        job2 = self.run_stage({"contact": [contact(1)]}, previous_job=job1)
        self.assertEqual(job2.report["staging"]["contact"]["missing_in_source"], 2)
        from parties.models import Party

        self.assertEqual(Party.objects.filter(company=self.co, kind="person").count(), 3)

    def test_previous_chain_skips_a_job_that_left_the_record_unchanged(self):
        from parties.models import Party

        job1 = self.first_import([contact(1, family="Example")])
        job2 = self.run_stage({"contact": [contact(1, family="Example")]}, previous_job=job1)  # unchanged
        Party.objects.filter(company=self.co).update(family_name="Staff-Fixed")
        row = self.one(contact(1, family="Exampleton"), previous_job=job2)
        self.assertEqual(row.proposed_mapping["decision"], "conflict")  # compared with job1's write

    def test_without_previous_job_differing_fields_conflict(self):
        party = make_person(self.co, given="Ada", family="Example", emails=["a@example.com"])
        link_ref(self.co, party, "1")
        row = self.one(contact(1, family="Other", emails=["a@example.com"]))
        p = row.proposed_mapping
        self.assertEqual(p["decision"], "conflict")
        self.assertEqual(p["diff"]["fields"], {"family_name": "conflict"})
        self.assertFalse(p["diff"]["previous_import"])

    def test_without_previous_job_an_unreadable_field_is_a_conflict(self):
        party = make_person(self.co, given="Ada", family="Example", emails=["a@example.com"])
        link_ref(self.co, party, "1")
        row = self.one(contact(1, emails=["a@example.com"], job_title="CTO"))
        self.assertEqual(row.proposed_mapping["diff"]["fields"], {"job_title": "conflict"})

    def test_previous_import_found_through_an_alias(self):
        job1 = self.first_import([contact(18, family="Example")])
        job2 = self.run_stage({"contact": [contact(51, aliases=["18"], family="Exampleton")]}, previous_job=job1)
        row = by_row(job2)[0]
        self.assertEqual(row.proposed_mapping["decision"], "update")
        self.assertTrue(row.proposed_mapping["diff"]["previous_import"])
        self.assertEqual(job2.report["staging"]["contact"]["missing_in_source"], 0)

    def test_two_refs_to_one_merged_survivor_are_flagged(self):
        survivor = make_person(self.co, family="Example")
        loser = make_person(self.co, family="Example")
        loser.merged_into = survivor
        loser.save(update_fields=["merged_into"])
        link_ref(self.co, survivor, "1")
        link_ref(self.co, loser, "2")
        job = self.run_stage({"contact": [contact(1, family="Other"), contact(2, family="Other")]})
        for row in by_row(job):
            self.assertEqual(row.proposed_mapping["target"]["id"], str(survivor.pk))
            self.assertIn(match.FLAG_SHARED_TARGET, row.proposed_mapping["flags"])

    def test_removals_are_counted_in_the_report(self):
        job1 = self.first_import([contact(1, emails=["a@example.com", "old@example.com"])])
        job2 = self.run_stage({"contact": [contact(1, emails=["a@example.com"])]}, previous_job=job1)
        self.assertEqual(job2.report["staging"]["contact"]["removed_in_source"], {"emails": 1})

    def test_without_previous_job_equal_record_is_unchanged(self):
        party = make_person(self.co, given="Ada", family="Example", emails=["a@example.com"])
        link_ref(self.co, party, "1")
        row = self.one(contact(1, emails=["a@example.com"]))
        self.assertEqual(row.proposed_mapping["reason"], "unchanged")


class DecisionServiceTests(Base):
    def test_pick_a_candidate(self):
        a = make_person(self.co, emails=["shared@example.com"])
        make_person(self.co, emails=["shared@example.com"])
        row = staging.approve_row(self.one(contact(1, emails=["shared@example.com"])), "reviewer", candidate=str(a.pk))
        self.assertEqual((row.mapping_state, row.mapped_values["target"]["id"]), ("approved", str(a.pk)))

    def test_undecided_row_needs_a_choice(self):
        make_person(self.co, emails=["shared@example.com"])
        make_person(self.co, emails=["shared@example.com"])
        row = self.one(contact(1, emails=["shared@example.com"]))
        with self.assertRaises(staging.DecisionError):
            staging.approve_row(row, "reviewer")
        self.assertEqual(staging.approve_row(row, "reviewer", create=True).mapped_values["decision"], "create")

    def test_conflict_needs_every_field_settled(self):
        party = make_person(self.co, family="Example")
        link_ref(self.co, party, "1")
        row = self.one(contact(1, family="Other"))
        with self.assertRaises(staging.DecisionError):
            staging.approve_row(row, "reviewer")
        row = staging.approve_row(row, "reviewer", keep=["family_name"])
        self.assertEqual(row.mapped_values["decision"], "update")
        self.assertNotIn("family_name", row.mapped_values["values"])
        self.assertEqual(row.mapped_values["keep"], ["family_name"])

    def test_skip_names_a_reason(self):
        row = self.one(contact(1))
        row = staging.skip_row(row, "reviewer")
        self.assertEqual((row.mapping_state, row.proposed_mapping["reason"], row.mapped_values), ("na", "rejected", {}))
        with self.assertRaises(staging.DecisionError):
            staging.skip_row(row, "")


class QueryCountTests(Base):
    """4.3: staging and matching cost a constant number of queries per 1,000 rows."""

    def export(self, n, co):
        """Per 1,000 rows the same mix: 100 email matches to existing parties,
        50 records golden already holds (ExternalRef, re-import), 333 with a phone."""
        records = []
        for i in range(n):
            sid = f"{i:06d}"
            emails = [f"known{i}@example.com"] if i % 10 == 0 else [f"p{i}@example.com"]
            records.append(contact(sid, given=f"G{i}", family=f"F{i}", emails=emails,
                                   phones=[f"+1202555{i % 10000:04d}"] if i % 3 == 0 else []))
            if i % 10 == 0:
                make_person(co, given=f"G{i}", family=f"F{i}", emails=[f"known{i}@example.com"])
            elif i % 20 == 5:
                link_ref(co, make_person(co, given=f"G{i}", family="Old"), sid)
        return ExportDir(self, {"contact": records})

    def queries(self, n):
        co = Company.objects.create(name=f"Perf {n}")
        export = self.export(n, co)
        previous = ImportJob.objects.create(company=co, source=SOURCE, provider="examplecrm",
                                            status=ImportJob.Status.APPLIED)
        ContentType.objects.clear_cache()  # every run pays the same cache misses
        with CaptureQueriesContext(connection) as ctx:
            job = staging.stage(co, export.path, previous_job=previous)
        counts = job.report["staging"]["contact"]
        self.assertEqual(counts["rows"], n)
        self.assertEqual(counts["by_decision"].get("conflict"), n // 20)  # the re-imported ones
        return len(ctx.captured_queries)

    def test_constant_queries_per_thousand_rows_up_to_ten_thousand(self):
        q1, q2, q10 = self.queries(1000), self.queries(2000), self.queries(10_000)
        per_chunk = q2 - q1
        self.assertGreater(per_chunk, 0)
        self.assertLess(per_chunk, 60, "a chunk of 1,000 rows must not cost a query per row")
        self.assertEqual(q10, q1 + 9 * per_chunk)
