"""The people appliers, the write log and undo (phase5-importer.md 1.5, 2.3-2.7, 4.5; step 4).

Done-when: apply then undo leaves the database equal to its snapshot (every
table the appliers write dumped, ordered, all columns, before and after;
``AuditLog`` is left out on purpose: ``core.sidecar.set_fields`` appends an
audit entry per sidecar write and the audit trail is append-only); an edited row
refuses undo; an erased ref is skipped. Runs on SQLite and on Postgres (the
"Test Suite (Postgres 17)" CI job runs the whole suite).
"""

from __future__ import annotations

from django.apps import apps
from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Company, ExternalRef, Team, UserProfile
from core.plugins import dispatch
from crm.models import Pipeline, Stage
from crm_import import stage as staging
from crm_import import writelog
from crm_import.canonical import make_record
from crm_import.models import ImportWriteLog
from parties.erasure import erase_party
from parties.models import Party, PartyAddress, PartyEmail, PartyPhone, PartyRelationship, PartyRole
from unify.apply import apply_chunk, start_apply_run, undo_run
from unify.models import ApprovalRow, StagedRow

from .staging_helpers import SOURCE, ExportDir, company, contact, deal, link_ref, make_org, make_person, pipeline

PROVIDER = SOURCE.split(":", 1)[0]
ACTOR = "test:reviewer"
STEP_ORDER = ("owner", "pipeline", "company", "contact", "association")

#: Every table the people appliers can write (plus auth.User: they must never).
SNAPSHOT_MODELS = (
    User, UserProfile, Team, ExternalRef, Party, PartyEmail, PartyPhone, PartyAddress, PartyRole,
    PartyRelationship, Pipeline, Stage,
)


def dump() -> dict:
    """Every affected table: all rows, ordered by pk, all columns."""
    out = {}
    for model in SNAPSHOT_MODELS:
        names = [f.attname for f in model._meta.concrete_fields]
        out[model._meta.label] = list(model._base_manager.order_by("pk").values_list(*names))
    return out


def owner(sid, *, email, active=True, given="Olive", family="Owner", teams=()):
    return make_record("owner", source=SOURCE, source_id=str(sid), archived=not active, data={
        "email": email, "given_name": given, "family_name": family, "display_name": f"{given} {family}",
        "active": active, "teams": [dict(t) for t in teams],
    })


def association(sid, from_ref, to_ref, *, primary=False, label=None):
    return make_record("association", source=SOURCE, source_id=str(sid), data={
        "from_ref": from_ref, "to_ref": to_ref, "label": label, "primary": primary,
    })


def ref(object_type, sid):
    return {"object_type": object_type, "source_id": str(sid)}


def person(sid, **kw):
    owner_ref = kw.pop("owner_ref", None)
    marketable = kw.pop("marketable", None)
    record = contact(sid, **kw)
    record["data"]["owner_ref"] = owner_ref
    record["data"]["marketable"] = marketable
    return record


def org(sid, *, phones=(), addresses=(), properties=None, owner_ref=None, **kw):
    record = company(sid, **kw)
    record["data"]["phones"] = [{"raw": p, "kind": "phone", "primary": i == 0} for i, p in enumerate(phones)]
    record["data"]["addresses"] = [dict({"purpose": "billing", "line_1": None, "line_2": None, "city": None,
                                         "region": None, "postal_code": None, "country": None,
                                         "primary": i == 0}, **a) for i, a in enumerate(addresses)]
    record["data"]["owner_ref"] = owner_ref
    record["properties"] = dict(properties or {})
    return record


def rows_of(job, object_type):
    return list(StagedRow.objects.filter(staging.job_rows_filter([job.pk], object_type)).order_by("row_number"))


def approve_all(job, object_type):
    """The reviewer approves every proposal; ambiguous rows become new records."""
    for row in rows_of(job, object_type):
        if row.mapping_state != StagedRow.MappingState.PROPOSED:
            continue
        decision = (row.proposed_mapping or {}).get("decision")
        staging.approve_row(row, ACTOR, create=decision is None)


def apply_type(job, object_type):
    batch = job.job_batches.get(object_type=object_type).batch
    run = start_apply_run(batch, actor=ACTOR)
    while apply_chunk(str(run.pk), [])["results"]:
        pass
    return run


def apply_job(job, types=STEP_ORDER):
    runs = {}
    for otype in types:
        approve_all(job, otype)
        runs[otype] = apply_type(job, otype)
    return runs


class Scenario(TestCase):
    """A tenant with history: a staff login, a person and an organisation
    entered by hand, the seeded default pipeline. The export creates some
    records, links others to what golden already holds, and links one
    pipeline by key."""

    def setUp(self):
        self.co = Company.objects.create(name="Tenant A")
        self.other = Company.objects.create(name="Tenant B")
        self.staff = User.objects.create_user("sam", email="sam@example.com", is_staff=True, last_name="Staff")
        UserProfile.objects.create(user=self.staff, company=self.co, display_name="Sam Staff")
        self.ada = make_person(self.co, given="Ada", family="Lovelace", emails=["ada@example.com"])
        self.globex = make_org(self.co, "Globex", domain="globex.example")
        sales = [s.key for s in Pipeline.objects.get(company=self.co, key="sales").stages.order_by("sort_order")]
        self.records = {
            "owner": [
                owner(1, email="sam@example.com", given="Sam", family="Staff"),  # link to the staff login
                owner(2, email="olive@example.com", teams=[{"source_id": "t1", "name": "East", "primary": True}]),
                owner(3, email="gone@example.com", given="Gil", family="Gone", active=False,
                      teams=[{"source_id": "t1", "name": "East", "primary": True}]),
            ],
            "pipeline": [
                pipeline("p1", key="sales", stages=(*sales, "closedwon"), outcomes={"closedwon": "won"}),
                pipeline("p2", key="partners", stages=("intro", "active", "ended"), outcomes={"ended": "lost"}),
                pipeline("p3", key="onboarding", stages=("open", "done"), applies_to="ticket"),
            ],
            "company": [
                org(10, name="Initech", domains=["initech.example", "initech-alt.example"],
                    phones=["+1 202 555 0100"], properties={"industry": "Software", "employees": "12"},
                    addresses=[{"line_1": "1 Main St", "city": "Springfield", "region": "IL",
                                "postal_code": "62701", "country": "us"}], owner_ref=ref("owner", 2)),
                org(11, name="Globex", domains=["globex.example"]),  # links to the hand-entered org
            ],
            "contact": [
                person(20, given="Grace", family="Hopper", emails=["grace@example.com", "not an address"],
                       phones=["+1 202 555 0143"], properties={"lead_type": "Patent Attorney",
                                                                "client_status": "Active Client"},
                       owner_ref=ref("owner", 2), marketable=True, aliases=["19"]),
                person(21, given="Ada", family="Lovelace", emails=["ada@example.com"], phones=["+1 202 555 0177"],
                       properties={"lead_type": "patent attorney"}),  # links to the hand-entered Ada
            ],
            "deal": [deal(99, pipeline_sid="p1", stage_sid="p1-new")],  # applied by step 5
            "association": [
                association(30, ref("contact", 20), ref("company", 10), primary=True),
                association(31, ref("company", 11), ref("contact", 21)),
                association(32, ref("deal", 99), ref("contact", 20)),  # deals are step 5
            ],
        }
        self.mapping = {"roles": {"contact": {"client_status": {"Active Client": "client"}}}}
        self.before = dump()

    def stage(self, records=None, **kw):
        export = ExportDir(self, self.records if records is None else records)
        job = staging.stage(self.co, export.path, mapping=kw.pop("mapping", self.mapping), **kw)
        self.assertEqual(job.status, "reviewing", job.report)
        return job


class ApplyTests(Scenario):
    def test_each_type_writes_what_the_spec_says(self):
        job = self.stage()
        apply_job(job)
        # Owners: a login-less profile with its team; the staff profile only gains refs.
        olive = UserProfile.objects.get(company=self.co, display_name="Olive Owner")
        self.assertIsNone(olive.user_id)
        self.assertEqual(olive.team.key, f"{PROVIDER}_t1")
        gone = UserProfile.objects.get(company=self.co, display_name="Gil Gone")
        self.assertEqual(gone.team_id, olive.team_id)  # one team, found again by its ref
        self.assertEqual(User.objects.count(), len(self.before["auth.User"]))  # no login is ever created
        sam_ref = ExternalRef.objects.get(company=self.co, source=SOURCE, object_type="owner", source_id="1")
        self.assertEqual(sam_ref.object_id, self.staff.userprofile.pk)
        # Pipelines: "sales" linked (one stage added, none edited), "partners" created, the ticket one kept.
        sales = Pipeline.objects.get(company=self.co, key="sales")
        self.assertTrue(sales.is_default)
        self.assertEqual(sales.stages.get(key="closedwon").outcome_hint, "won")
        partners = Pipeline.objects.get(company=self.co, key="partners")
        self.assertFalse(partners.is_default)
        self.assertEqual(list(partners.stages.order_by("sort_order").values_list("key", "outcome_hint")),
                         [("intro", ""), ("active", ""), ("ended", "lost")])
        self.assertFalse(Pipeline.objects.filter(company=self.co, key="onboarding").exists())
        self.assertTrue(ExternalRef.objects.filter(object_type="stage", source_id="p2/p2-ended").exists())
        # Companies.
        initech = Party.objects.get(company=self.co, display_name="Initech")
        self.assertEqual((initech.kind, initech.origin, initech.primary_domain),
                         ("organisation", "import", "initech.example"))
        self.assertEqual(initech.owner_id, olive.pk)
        self.assertEqual(initech.owner_team_id, olive.team_id)  # stamped by Party.save()
        self.assertEqual(initech.phones.get().number_e164, "+12025550100")
        self.assertEqual(initech.addresses.get().country, "US")
        self.assertEqual(initech.metadata[f"{PROVIDER}.industry"], "Software")
        self.assertEqual(initech.metadata[f"{PROVIDER}.import_domains"], ["initech.example", "initech-alt.example"])
        # Contacts: points, the mapped role, the byte-for-byte lead type, the unparseable address kept.
        grace = Party.objects.get(company=self.co, given_name="Grace")
        self.assertEqual(grace.primary_email_normalized, "grace@example.com")
        self.assertEqual(grace.primary_phone_e164, "+12025550143")
        self.assertTrue(grace.marketable)
        self.assertEqual(grace.marketable_source, f"import:{PROVIDER}")
        self.assertTrue(grace.roles.filter(role="client", valid_to__isnull=True).exists())
        self.assertEqual(grace.metadata[f"{PROVIDER}.lead_type"], "Patent Attorney")
        self.assertEqual(grace.metadata[f"{PROVIDER}.import_unparsed_emails"], ["not an address"])
        self.assertEqual(set(ExternalRef.objects.filter(object_id=grace.pk).values_list("source_id", flat=True)),
                         {"20", "19"})  # one ref per id and alias
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.display_name, "Ada Lovelace")
        self.assertEqual(self.ada.metadata[f"{PROVIDER}.lead_type"], "patent attorney")
        self.assertEqual(self.ada.phones.get().is_primary, True)  # she had none: the new one is primary
        # Associations: employment both ways round; the deal pair waits for step 5.
        self.assertTrue(PartyRelationship.objects.filter(from_party=grace, to_party=initech, kind="employment",
                                                         is_primary=True).exists())
        self.assertTrue(PartyRelationship.objects.filter(from_party=self.ada, to_party=self.globex).exists())
        deal_row = rows_of(job, "association")[2]
        self.assertEqual(deal_row.mapping_state, StagedRow.MappingState.FAILED)
        self.assertTrue(deal_row.error.startswith("dependency_missing: "), deal_row.error)
        # The write log: one numbered entry per written row, contiguous per approval row.
        seqs = list(ImportWriteLog.objects.filter(job=job).values_list("seq", flat=True))
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))

    def test_apply_and_undo_send_no_signal(self):
        from core.plugins import signals

        received = []

        def receiver(sender, **kwargs):
            received.append(sender)

        for sig in (signals.party_created, signals.party_updated):
            sig.connect(receiver, weak=False, dispatch_uid=f"test-{id(sig)}")
            self.addCleanup(sig.disconnect, dispatch_uid=f"test-{id(sig)}")
        with self.captureOnCommitCallbacks(execute=True):
            job = self.stage()
            apply_job(job)
            writelog.undo_job(job, actor=ACTOR)
        self.assertEqual(received, [])  # every write ran inside suppressed() (hand-off 6)
        self.assertFalse(dispatch.is_suppressed())

    def test_a_crashed_row_is_not_written_twice(self):
        job = self.stage()
        apply_job(job, ("owner", "pipeline", "company"))
        row = rows_of(job, "company")[0]
        approval = ApprovalRow.objects.get(staged_row=row)
        # The commit landed but the ApprovalRow/StagedRow update did not.
        ApprovalRow.objects.filter(pk=approval.pk).update(state=ApprovalRow.State.PENDING)
        StagedRow.objects.filter(pk=row.pk).update(mapping_state=StagedRow.MappingState.APPROVED, applied_ref="")
        count = Party.objects.filter(display_name="Initech").count()
        apply_chunk(str(approval.run_id), [])
        self.assertEqual(Party.objects.filter(display_name="Initech").count(), count)
        row.refresh_from_db()
        self.assertEqual(row.mapping_state, StagedRow.MappingState.APPLIED)

    def test_a_crashed_link_row_reports_the_linked_party(self):
        job = self.stage()
        apply_job(job, ("owner", "pipeline", "company", "contact"))
        row = next(r for r in rows_of(job, "contact") if r.raw["source_id"] == "21")
        approval = ApprovalRow.objects.get(staged_row=row)
        ApprovalRow.objects.filter(pk=approval.pk).update(state=ApprovalRow.State.PENDING)
        StagedRow.objects.filter(pk=row.pk).update(mapping_state=StagedRow.MappingState.APPROVED, applied_ref="")
        apply_chunk(str(approval.run_id), [])
        row.refresh_from_db()
        self.assertEqual(row.applied_ref, f"parties.party:{self.ada.pk}")

    def test_tenant_isolation(self):
        job = self.stage()
        apply_job(job, ("owner",))
        # A decision naming another tenant's party is refused, and nothing is written there.
        foreign = make_person(self.other, given="Ada", family="Lovelace", emails=["ada@example.com"])
        row = next(r for r in rows_of(job, "contact") if r.raw["source_id"] == "21")
        mapped = dict(row.mapped_values or staging.approval_values(row.raw, "contact", "link", None))
        mapped.update(decision="link", target={"model": "parties.party", "id": str(foreign.pk)})
        StagedRow.objects.filter(pk=row.pk).update(mapped_values=mapped, mapping_state=StagedRow.MappingState.APPROVED)
        apply_type(job, "contact")
        row.refresh_from_db()
        self.assertTrue(row.error.startswith("conflict_stale: "), row.error)
        self.assertFalse(ExternalRef.objects.filter(object_id=foreign.pk).exists())
        self.assertFalse(ImportWriteLog.objects.exclude(company=self.co).exists())

    def test_a_property_in_the_importers_own_namespace_fails_the_row(self):
        self.records["company"][0]["properties"]["import_date"] = "2020-01-01"
        job = self.stage()
        apply_job(job, ("owner", "company"))
        row = rows_of(job, "company")[0]
        self.assertTrue(row.error.startswith("invalid_value: "), row.error)

    def test_a_system_role_in_the_mapping_fails_the_row(self):
        job = self.stage(mapping={"roles": {"contact": {"client_status": {"Active Client": "customer"}}}})
        apply_job(job, ("owner", "company", "contact"))
        grace = next(r for r in rows_of(job, "contact") if r.raw["source_id"] == "20")
        self.assertTrue(grace.error.startswith("invalid_value: "), grace.error)
        self.assertFalse(Party.objects.filter(given_name="Grace").exists())  # the savepoint rolled it back


class UndoTests(Scenario):
    def test_apply_then_undo_job_restores_the_snapshot(self):
        job = self.stage()
        apply_job(job)
        self.assertNotEqual(dump(), self.before)
        report = writelog.undo_job(job, actor=ACTOR)
        self.assertEqual(report.refused, [])
        self.assertEqual(dump(), self.before)
        self.assertFalse(ImportWriteLog.objects.filter(job=job, undone_at__isnull=True).exists())
        # Undone rows wait, approved, for a re-apply.
        applied = StagedRow.objects.filter(staging.job_rows_filter([job.pk], "contact"))
        self.assertEqual(set(applied.values_list("mapping_state", flat=True)), {StagedRow.MappingState.APPROVED})

    def test_apply_then_undo_runs_newest_batch_first_restores_the_snapshot(self):
        job = self.stage()
        runs = apply_job(job)
        for otype in reversed(STEP_ORDER):
            out = undo_run(str(runs[otype].pk), runs[otype].undo_token)
            self.assertEqual(out["refused"], [], otype)
        self.assertEqual(dump(), self.before)

    def test_reapply_after_undo_then_undo_again(self):
        job = self.stage()
        apply_job(job)
        writelog.undo_job(job, actor=ACTOR)
        for otype in STEP_ORDER:
            apply_type(job, otype)
        self.assertTrue(Party.objects.filter(given_name="Grace").exists())
        self.assertEqual(writelog.undo_job(job, actor=ACTOR).refused, [])
        self.assertEqual(dump(), self.before)

    def test_an_edited_row_refuses_undo_and_the_rest_is_undone(self):
        job = self.stage()
        apply_job(job)
        grace = Party.objects.get(given_name="Grace")
        grace.given_name = "Gracie"
        grace.save()
        report = writelog.undo_job(job, actor=ACTOR)
        refused_rows = {r["row"] for r in report.refused}
        grace_row = next(r for r in rows_of(job, "contact") if r.raw["source_id"] == "20")
        self.assertIn(str(grace_row.pk), refused_rows)
        self.assertTrue(any("changed after the import" in r["error"] for r in report.refused))
        self.assertTrue(Party.objects.filter(pk=grace.pk).exists())
        grace_row.refresh_from_db()
        self.assertEqual(grace_row.mapping_state, StagedRow.MappingState.APPLIED)
        # Owner 2 is still Grace's owner, so it (and its team) refuse too; the rest is gone.
        self.assertTrue(UserProfile.objects.filter(display_name="Olive Owner").exists())
        self.assertFalse(Party.objects.filter(display_name="Initech").exists())
        self.assertFalse(PartyRelationship.objects.filter(from_party=self.ada).exists())
        self.ada.refresh_from_db()
        self.assertNotIn(f"{PROVIDER}.lead_type", self.ada.metadata)

    def test_an_edited_row_refuses_undo_run(self):
        job = self.stage()
        runs = apply_job(job, ("owner", "pipeline", "company"))
        initech = Party.objects.get(display_name="Initech")
        PartyPhone.objects.filter(party=initech).update(number_raw="+1 202 555 0199")
        out = undo_run(str(runs["company"].pk), runs["company"].undo_token)
        self.assertEqual(len(out["refused"]), 1)
        self.assertIn("changed after the import", out["refused"][0]["error"])
        self.assertTrue(Party.objects.filter(pk=initech.pk).exists())  # all or nothing per row

    def test_a_hidden_reference_keeps_the_row(self):
        # Party.owner_team points at Team with related_name="+": still a reference.
        job = self.stage()
        apply_job(job)
        team = Team.objects.get(company=self.co, key=f"{PROVIDER}_t1")
        Party.objects.filter(pk=self.globex.pk).update(owner_team=team)
        report = writelog.undo_job(job, actor=ACTOR)
        self.assertTrue(any("still referenced" in r["error"] for r in report.refused), report.refused)
        self.assertTrue(Team.objects.filter(pk=team.pk).exists())

    def test_a_record_referenced_outside_the_import_stays(self):
        job = self.stage()
        apply_job(job)
        grace = Party.objects.get(given_name="Grace")
        PartyRelationship.objects.create(from_party=grace, to_party=self.globex, kind="employment")
        report = writelog.undo_job(job, actor=ACTOR)
        self.assertTrue(any("still referenced" in r["error"] for r in report.refused))
        self.assertTrue(Party.objects.filter(pk=grace.pk).exists())


class ReimportTests(Scenario):
    """A second job over the same source (3.4): ``update`` decisions."""

    def reimport(self, family="Murray"):
        first = self.stage()
        apply_job(first)
        self.after_first = dump()
        records = dict(self.records)
        records["contact"] = [dict(r) for r in self.records["contact"]]
        grace = records["contact"][0] = person(
            20, given="Grace", family=family, emails=["grace@example.com", "grace@navy.example"],
            phones=["+1 202 555 0143"], properties={"lead_type": "Patent Attorney", "client_status": "Active Client"},
            owner_ref=ref("owner", 2), marketable=True, aliases=["19"])
        self.assertEqual(grace["data"]["family_name"], family)
        second = self.stage(records, previous_job=first)
        return first, second

    def test_update_writes_the_changed_fields_and_undo_restores_the_first_import(self):
        _first, second = self.reimport()
        row = next(r for r in rows_of(second, "contact") if r.raw["source_id"] == "20")
        self.assertEqual(row.proposed_mapping["decision"], "update", row.proposed_mapping)
        apply_type(second, "contact")
        row.refresh_from_db()
        self.assertEqual(row.mapping_state, StagedRow.MappingState.APPLIED, row.error)
        grace = Party.objects.get(given_name="Grace")
        self.assertEqual(grace.family_name, "Murray")
        self.assertEqual(grace.display_name, "Grace Murray")  # re-derived as Party.save() would
        self.assertEqual(set(grace.emails.values_list("email_normalized", flat=True)),
                         {"grace@example.com", "grace@navy.example"})
        self.assertEqual(writelog.undo_job(second, actor=ACTOR).refused, [])
        self.assertEqual(dump(), self.after_first)

    def test_a_staff_edit_after_the_decision_fails_conflict_stale(self):
        _first, second = self.reimport()
        Party.objects.filter(given_name="Grace").update(family_name="Edited")
        apply_type(second, "contact")
        row = next(r for r in rows_of(second, "contact") if r.raw["source_id"] == "20")
        self.assertTrue(row.error.startswith("conflict_stale: "), row.error)
        self.assertEqual(Party.objects.get(given_name="Grace").family_name, "Edited")

    def test_a_kept_property_keeps_golden_value(self):
        first = self.stage()
        apply_job(first)
        grace = Party.objects.get(given_name="Grace")
        Party.objects.filter(pk=grace.pk).update(
            metadata={**grace.metadata, f"{PROVIDER}.lead_type": "Edited by staff"})
        records = dict(self.records)
        records["contact"] = [dict(r) for r in self.records["contact"]]
        changed = dict(records["contact"][0])
        changed["properties"] = {**changed["properties"], "lead_type": "Inventor"}
        records["contact"][0] = changed
        second = self.stage(records, previous_job=first)
        row = next(r for r in rows_of(second, "contact") if r.raw["source_id"] == "20")
        self.assertEqual(row.proposed_mapping["decision"], "conflict", row.proposed_mapping)
        staging.approve_row(row, ACTOR, keep=["properties.lead_type"])
        apply_type(second, "contact")
        row.refresh_from_db()
        self.assertEqual(row.mapping_state, StagedRow.MappingState.APPLIED, row.error)
        grace.refresh_from_db()
        self.assertEqual(grace.metadata[f"{PROVIDER}.lead_type"], "Edited by staff")

    def test_the_first_job_cannot_be_undone_under_the_second(self):
        first, second = self.reimport()
        apply_type(second, "contact")
        report = writelog.undo_job(first, actor=ACTOR)
        self.assertTrue(any("written again by import job" in r["error"] or "changed after" in r["error"]
                            for r in report.refused), report.refused)
        self.assertEqual(Party.objects.get(given_name="Grace").family_name, "Murray")


class ErasureTests(Scenario):
    def test_an_erased_ref_is_skipped(self):
        # Approved before the erasure, applied after: the row is skipped, its raw data dropped.
        job = self.stage({"contact": [person(40, given="Eve", family="Erased", emails=["eve@example.com"])]})
        eve = make_person(self.co, given="Eve", family="Erased", emails=["eve@example.com"])
        link_ref(self.co, eve, "40")
        erase_party(eve, reason="Request received by post")
        apply_type(job, "contact")
        row = rows_of(job, "contact")[0]
        self.assertEqual(row.mapping_state, StagedRow.MappingState.NA, row.error)
        self.assertEqual(row.raw, {"erased": True})
        self.assertFalse(Party.objects.filter(given_name="Eve").exclude(pk=eve.pk).exists())

    def test_an_erased_alias_is_skipped(self):
        # The record no longer carries the erased address, so erasure does not
        # scrub the staged row: only the alias says who it is.
        job = self.stage({"contact": [person(41, given="Eve", family="Erased", emails=["eve.new@example.com"],
                                             aliases=["42"])]})
        eve = make_person(self.co, given="Eve", family="Erased", emails=["eve2@example.com"])
        link_ref(self.co, eve, "42")
        erase_party(eve, reason="Request received by post")
        apply_type(job, "contact")
        row = rows_of(job, "contact")[0]
        self.assertEqual(row.mapping_state, StagedRow.MappingState.NA, row.error)
        self.assertEqual(row.raw, {"erased": True})
        self.assertEqual(Party.objects.filter(company=self.co, origin="import").count(), 0)

    def test_an_address_erased_after_staging_is_skipped(self):
        # No ExternalRef names the erased person and the staged row escaped
        # erasure's text scrub (written as the suppression erasure leaves, with
        # no staged row to find): only the erasure suppression on the number
        # says who it is.
        from parties.consent import address_hash
        from parties.models import Suppression

        job = self.stage({"contact": [person(43, given="Eve", family="Erased", phones=["(202) 555-0188"])]})
        Suppression.objects.create(company=self.co, channel="phone", address_hash=address_hash("+12025550188"),
                                   reason="erasure", source="erp")
        row = rows_of(job, "contact")[0]
        self.assertEqual(row.mapping_state, StagedRow.MappingState.APPROVED)
        apply_type(job, "contact")
        row.refresh_from_db()
        self.assertTrue(row.error.startswith("erased: "), row.error)
        self.assertEqual(row.raw, {"erased": True})
        self.assertFalse(Party.objects.filter(given_name="Eve").exists())

    def test_undo_never_resurrects_an_erased_party(self):
        job = self.stage()
        apply_job(job)
        grace = Party.objects.get(given_name="Grace")
        erase_party(grace, reason="Request received by post")
        report = writelog.undo_job(job, actor=ACTOR)
        self.assertTrue(any("erased" in r["error"] for r in report.refused))
        grace.refresh_from_db()
        self.assertIsNotNone(grace.erased_at)
        # Its refs stay erased, so a re-import still skips the person.
        refs = ExternalRef.objects.filter(object_id=grace.pk)
        self.assertTrue(refs.exists())
        self.assertTrue(all(r.metadata.get("erased") for r in refs))

    def test_erasure_scrubs_the_write_log_before_and_undo_refuses(self):
        job = self.stage()
        apply_job(job)
        updates = ImportWriteLog.objects.filter(job=job, action="update", object_id=str(self.ada.pk))
        self.assertTrue(updates.exists())
        self.assertIn("Lovelace", str(updates.first().before))
        erase_party(self.ada, reason="Request received by post")
        self.assertEqual(set(map(str, updates.values_list("before", flat=True))), {"{}"})
        report = writelog.undo_job(job, actor=ACTOR)
        self.assertTrue(any("scrubbed by an erasure" in r["error"] or "erased" in r["error"]
                            for r in report.refused))
        self.ada.refresh_from_db()
        self.assertIsNotNone(self.ada.erased_at)


class RegistryTests(TestCase):
    def test_entity_kinds_are_registered_with_the_permission(self):
        from unify.appliers import get_applier

        for otype in STEP_ORDER:
            applier = get_applier(f"crm_{otype}")
            self.assertEqual(applier.permission, "crm_import.apply_import")
            self.assertTrue(applier.undo_newest_first)
            self.assertEqual(applier.undo_state, "approved")
        self.assertTrue(apps.is_installed("crm_import"))
