"""The deal and ticket appliers (phase5-importer.md 2.5, 2.7, 2.8; hand-off sections 2-4, 6, 7; step 5).

Done-when: every 2.5 rule has a test (the ``Rule*`` classes, one per rule);
``check_engagements`` is clean after the import; no Quote or Order is ever
created. Also: apply then undo leaves every table the appliers write equal
to its snapshot, nothing emits a signal, the history is strictly increasing
and ends at the engagement's own state, and another tenant is never touched.

The snapshot leaves out, on purpose, ``AuditLog`` (``core.sidecar.set_fields``
appends one per sidecar write; the audit trail is append-only) and
``DocumentSequence`` (an undone engagement's number is burned, never reused).
"""

from __future__ import annotations

import datetime as dt
import io
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.models import Company, ExternalRef
from core.plugins import dispatch
from crm import engagements
from crm.models import (
    Engagement,
    EngagementContact,
    EngagementStageChange,
    Pipeline,
    Stage,
    Task,
    TaskEvent,
)
from crm_import import stage as staging
from crm_import import writelog
from crm_import.appliers import deals as deal_applier
from crm_import.canonical import OBJECT_TYPES, make_record, write_manifest, write_records
from crm_import.models import ImportJob, ImportWriteLog
from parties.models import Party, PartyRelationship
from unify.models import ApprovalRow, StagedRow

from .staging_helpers import SOURCE, contact, pipeline
from .test_appliers import ACTOR, apply_type, association, owner, ref, rows_of
from .test_appliers import dump as people_dump

PROVIDER = SOURCE.split(":", 1)[0]
STEPS = ("owner", "pipeline", "company", "contact", "deal", "association", "relationship")
UTC = dt.UTC

#: Every table the step 5 appliers can write, on top of the people tables.
DEAL_MODELS = (Engagement, EngagementContact, EngagementStageChange, Task, TaskEvent, ImportWriteLog)


def dump() -> dict:
    out = people_dump()
    for model in DEAL_MODELS:
        if model is ImportWriteLog:
            continue  # the log is append-only: undo marks it, never deletes
        names = [f.attname for f in model._meta.concrete_fields]
        out[model._meta.label] = list(model._base_manager.order_by("pk").values_list(*names))
    return out


def at(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def hist(*entries) -> list[dict]:
    """``("dealstage", "p1-qualified", "2024-01-02T09:00:00Z")`` → canonical history entries."""
    return [{"property": p, "value": v, "at": a, "source": "CRM_UI"} for p, v, a in entries]


def deal(sid, *, stage="p1-appointment", pipeline_sid="p1", status="open", closed_at=None, amount=None, currency="USD",
         company_refs=(), contact_refs=(), owner_ref=None, loss_reason_raw=None, follow_up=(), history=None,
         created_at="2024-01-01T09:00:00Z", updated_at="2024-06-01T09:00:00Z", properties=None, title=None,
         expected_close_date=None):
    return make_record("deal", source=SOURCE, source_id=str(sid), data={
        "title": title if title is not None else f"Deal {sid}",
        "pipeline_ref": ref("pipeline", pipeline_sid), "stage_ref": ref("stage", stage),
        "status": status, "closed_at": closed_at, "amount": amount, "currency": currency,
        "expected_close_date": expected_close_date, "owner_ref": owner_ref,
        "company_refs": [ref("company", c) for c in company_refs],
        "contact_refs": [ref("contact", c) for c in contact_refs],
        "loss_reason_raw": loss_reason_raw,
        "follow_up": [{"field": f, "value": v} for f, v in follow_up],
    }, history=history, created_at=created_at, updated_at=updated_at, properties=properties or {})


def org(sid, name):
    return make_record("company", source=SOURCE, source_id=str(sid), data={
        "name": name, "legal_name": None, "domains": [], "phones": [], "addresses": [], "owner_ref": None,
        "registration_number": None, "tax_id": None,
    })


def ticket(sid, *, stage="t1-new", state="open", contact_refs=(), company_refs=(), opened_at="2023-01-10T09:00:00Z",
           closed_at=None, subject="Referral partner", history=None, properties=None):
    return make_record("relationship", source=SOURCE, source_id=str(sid), source_object_type="ticket", data={
        "pipeline_ref": ref("pipeline", "t1"), "stage_ref": ref("stage", stage), "state": state,
        "subject": subject, "description": "Refers patent work to us.", "opened_at": opened_at,
        "closed_at": closed_at, "owner_ref": None,
        "contact_refs": [ref("contact", c) for c in contact_refs],
        "company_refs": [ref("company", c) for c in company_refs],
    }, history=history, created_at=opened_at, properties=properties or {})


def with_deals(records: dict, deals: list) -> dict:
    """``records`` with only ``deals``, and only the associations whose deal is among them."""
    keep = {str(d["source_id"]) for d in deals}

    def wanted(a):
        ends = [a["data"]["from_ref"], a["data"]["to_ref"]]
        return all(e["object_type"] != "deal" or str(e["source_id"]) in keep for e in ends)

    return records | {"deal": deals, "association": [a for a in records["association"] if wanted(a)]}


class Export:
    """A canonical export in a temp directory; ``coverage`` overrides per type."""

    def __init__(self, testcase, records: dict, *, coverage=None):
        self.path = Path(tempfile.mkdtemp(prefix="crm-deals-"))
        testcase.addCleanup(shutil.rmtree, self.path, True)
        for otype in OBJECT_TYPES:
            write_records(self.path, otype, records.get(otype, []))
        objects = {t: {"coverage": "complete"} for t in OBJECT_TYPES}
        for otype, value in (coverage or {}).items():
            objects[otype] = {"coverage": value, "reasons": [{"code": "count_mismatch", "detail": "test"}]}
        write_manifest(self.path, provider=PROVIDER, source=SOURCE,
                       adapter={"id": "test", "version": "0.0.1", "schema_version": 1},
                       exported_at="2026-10-01T09:00:00Z", input={"kind": "crawl", "time_zone": None},
                       objects=objects)


STAGES = ("appointment", "qualified", "negotiation", "closedwon", "closedlost")
OUTCOMES = {"closedwon": "won", "closedlost": "lost"}


def approve_all(job, object_type, **picks):
    """The reviewer approves every proposal as proposed (``picks``: ``{source id: outcome}``)."""
    for row in rows_of(job, object_type):
        if row.mapping_state != StagedRow.MappingState.PROPOSED:
            continue
        decision = (row.proposed_mapping or {}).get("decision")
        staging.approve_row(row, ACTOR, create=decision is None, outcome=picks.get(str(row.raw.get("source_id"))))


def apply_job(job, types=STEPS, **picks):
    runs = {}
    for otype in types:
        approve_all(job, otype, **picks)
        runs[otype] = apply_type(job, otype)
    return runs


def engagement(source_id, *, object_type="deal"):
    ref_ = ExternalRef.objects.get(source=SOURCE, object_type=object_type, source_id=str(source_id))
    return Engagement._base_manager.get(pk=ref_.object_id)


def history_of(eng) -> list[tuple]:
    return [(r.from_stage.key if r.from_stage_id else None, r.to_stage.key if r.to_stage_id else None,
             r.from_status, r.to_status, r.changed_at, r.source)
            for r in eng.stage_changes.order_by("changed_at", "created_at").select_related("from_stage", "to_stage")]


class DealScenario(TestCase):
    """A tenant with a staff login and its self party; an export of owners,
    one deal pipeline, two companies, three contacts, deals for every 2.5
    rule, their associations and two partner tickets."""

    mapping = {
        "deal": {"loss_reasons": {"Too expensive": "price"}},
        "associations": {"deal_contact": {"Decision maker": "decision_maker"}},
        "tickets": {"relationship_kind": "partnership", "ended_stages": ["ended"]},
        "default_currency": "EUR",
    }

    def setUp(self):
        self.co = Company.objects.create(name="Tenant A")
        self.other = Company.objects.create(name="Tenant B")
        self.me = Party.objects.create(company=self.co, kind="organisation", display_name="Tenant A",
                                       is_self=True, origin="system")
        self.records = self.build_records()
        self.before = dump()

    def build_records(self) -> dict:
        future = (timezone.now() + dt.timedelta(days=30)).date().isoformat()
        return {
            "owner": [owner(2, email="olive@example.com")],
            "pipeline": [
                pipeline("p1", key="deals", stages=STAGES, outcomes=OUTCOMES),
                pipeline("t1", key="partners", stages=("new", "active", "ended"), applies_to="ticket",
                         outcomes={"ended": "closed"}),
            ],
            "company": [org(10, "Initech"), org(11, "Globex")],
            "contact": [
                contact(20, given="Grace", family="Hopper", emails=["grace@example.com"]),
                contact(21, given="Ada", family="Lovelace", emails=["ada@example.com"]),
                contact(22, given="Bob", family="Builder", emails=["bob@example.com"]),
                contact(23, given="Old", family="Archived", emails=["old@example.com"], archived=True),
            ],
            "deal": [
                # open, full history, follow-up with an overdue instant
                deal("d1", stage="p1-negotiation", amount="1200.50", company_refs=[10, 11], contact_refs=[20, 21],
                     owner_ref=ref("owner", 2), title="Widget retrofit",
                     follow_up=[("hs_next_step", "Send revised quote"), ("notes_next_activity_date",
                                                                         "2024-05-01T14:00:00Z")],
                     history=hist(("pipeline", "p1", "2024-01-01T09:00:00Z"),
                                  ("dealstage", "p1-appointment", "2024-01-01T09:00:00Z"),
                                  ("dealstage", "p1-qualified", "2024-02-01T09:00:00Z"),
                                  ("dealstage", "p1-negotiation", "2024-03-01T09:00:00Z"))),
                # lost with history: keeps the last open stage; mapped loss reason
                deal("d2", stage="p1-closedlost", status="lost", closed_at="2024-04-15T12:00:00Z",
                     amount="300", company_refs=[11], loss_reason_raw="Too expensive",
                     follow_up=[("hs_next_step", "Never mind")],
                     history=hist(("dealstage", "p1-appointment", "2024-01-01T09:00:00Z"),
                                  ("dealstage", "p1-negotiation", "2024-02-01T09:00:00Z"),
                                  ("dealstage", "p1-closedlost", "2024-04-10T09:00:00Z"))),
                # won, no history, in the won stage: stage NULL; one contact is the party; no amount
                deal("d3", stage="p1-closedwon", status="won", closed_at="2024-05-01T12:00:00Z",
                     contact_refs=[22], currency=None),
                # lost in a working stage with no history or close date: keeps that stage, closed_at inferred
                deal("d4", stage="p1-qualified", status="lost", loss_reason_raw="Gremlins", company_refs=[10]),
                # no status in the won stage: won by the hint; closed_at from the last stage change
                deal("d5", stage="p1-closedwon", status=None, company_refs=[10],
                     history=hist(("dealstage", "p1-qualified", "2024-01-05T09:00:00Z"),
                                  ("dealstage", "p1-closedwon", "2024-03-03T09:00:00Z"))),
                # two contacts, no company, no primary: no party, flagged
                deal("d6", contact_refs=[20, 21]),
                # two contacts, the primary association decides
                deal("d7", contact_refs=[20, 21]),
                # nothing associated; a date-only follow-up in the future
                deal("d8", follow_up=[("notes_next_activity_date", future)]),
            ],
            "association": [
                association("a1", ref("deal", "d1"), ref("contact", 20), label="Decision maker"),
                association("a2", ref("deal", "d1"), ref("contact", 21), label="champion", primary=True),
                association("a3", ref("deal", "d1"), ref("company", 10), primary=True),
                association("a4", ref("deal", "d1"), ref("company", 11)),
                association("a5", ref("deal", "d7"), ref("contact", 21), primary=True),
                association("a6", ref("deal", "d7"), ref("contact", 20), label="Influencer"),
                association("a10", ref("deal", "d1"), ref("contact", 23)),  # an archived contact: N/A
                association("a7", ref("deal", "d3"), ref("contact", 22)),  # the hub person
                association("a8", ref("relationship", "50"), ref("contact", 20), primary=True),
                association("a9", ref("relationship", "52"), ref("contact", 21)),
            ],
            "relationship": [
                ticket(50, contact_refs=[20], company_refs=[10]),
                ticket(51, stage="t1-ended", state="closed", company_refs=[11], closed_at="2024-02-01T09:00:00Z"),
                # no primary contact: the company is the partner (2.8)
                ticket(52, stage="t1-active", contact_refs=[21], company_refs=[10]),
            ],
        }

    def stage(self, records=None, *, coverage=None, mapping=None, options=None, previous_job=None):
        export = Export(self, self.records if records is None else records, coverage=coverage)
        job = staging.stage(self.co, export.path, mapping=self.mapping if mapping is None else mapping,
                            options=options, previous_job=previous_job)
        self.assertEqual(job.status, ImportJob.Status.REVIEWING, job.report)
        return job

    def imported(self, **picks):
        job = self.stage()
        apply_job(job, **picks)
        return job

    def row(self, job, object_type, source_id):
        return next(r for r in rows_of(job, object_type) if str(r.raw.get("source_id")) == str(source_id))


# --- 2.5, rule by rule ---------------------------------------------------------------


class RuleOutcomeTests(DealScenario):
    """2.5 rule 1: the outcome is a tri-state independent of the stage."""

    def test_flags_win_and_a_null_status_takes_the_stage_hint(self):
        self.imported()
        self.assertEqual(engagement("d1").status, "open")
        self.assertEqual(engagement("d2").status, "lost")
        self.assertEqual(engagement("d3").status, "won")
        self.assertEqual(engagement("d5").status, "won")  # null + won hint

    def test_null_status_in_an_unhinted_stage_is_open(self):
        records = with_deals(self.records, [deal("d9", stage="p1-qualified", status=None)])
        job = self.stage(records)
        apply_job(job)
        self.assertEqual(engagement("d9").status, "open")

    def test_a_conflict_is_flagged_and_never_resolved_silently(self):
        records = with_deals(self.records, [deal("d9", stage="p1-closedlost", status="won",
                                                closed_at="2024-02-02T09:00:00Z")])
        job = self.stage(records)
        row = self.row(job, "deal", "d9")
        self.assertIn("outcome_conflict", row.proposed_mapping["flags"])
        self.assertEqual(row.mapping_state, StagedRow.MappingState.PROPOSED)  # never auto-approved
        apply_job(job)  # approved as proposed, no pick
        row.refresh_from_db()
        self.assertEqual(row.mapping_state, StagedRow.MappingState.FAILED)
        self.assertTrue(row.error.startswith("invalid_value: outcome_conflict"), row.error)
        self.assertFalse(ExternalRef.objects.filter(object_type="deal", source_id="d9").exists())

    def test_a_reviewer_pick_settles_the_conflict(self):
        records = with_deals(self.records, [deal("d9", stage="p1-closedlost", status="won",
                                                closed_at="2024-02-02T09:00:00Z")])
        job = self.stage(records)
        apply_job(job, d9="lost")
        eng = engagement("d9")
        self.assertEqual((eng.status, eng.loss_reason), ("lost", "unknown"))

    def test_open_in_a_won_stage_is_a_conflict_and_open_cannot_be_picked(self):
        records = with_deals(self.records, [deal("d9", stage="p1-closedwon", status="open")])
        job = self.stage(records)
        self.assertIn("outcome_conflict", self.row(job, "deal", "d9").proposed_mapping["flags"])
        with self.assertRaises(staging.DecisionError):
            staging.approve_row(self.row(job, "deal", "d9"), ACTOR, outcome="maybe")
        apply_job(job, d9="open")
        row = self.row(job, "deal", "d9")
        self.assertTrue(row.error.startswith("invalid_value: "), row.error)

    def test_null_status_in_a_closed_unhinted_stage_is_a_review_item(self):
        records = dict(self.records)
        records["pipeline"] = [pipeline("p1", key="deals", stages=(*STAGES, "referred"),
                                        outcomes={**OUTCOMES, "referred": "closed"}), records["pipeline"][1]]
        records = with_deals(records, [deal("d9", stage="p1-referred", status=None)])
        job = self.stage(records)
        self.assertIn("outcome_conflict", self.row(job, "deal", "d9").proposed_mapping["flags"])
        # The pipeline reviewer leaves the stage unhinted; the deal waits for its own pick.
        prow = self.row(job, "pipeline", "p1")
        staging.approve_row(prow, ACTOR)
        StagedRow.objects.filter(pk=prow.pk).update(
            mapped_values={**prow.mapped_values, "stage_outcomes": {"referred": ""}})
        apply_job(job, ("owner", "pipeline", "company", "contact"))
        apply_job(job, ("deal",))
        self.assertTrue(self.row(job, "deal", "d9").error.startswith("invalid_value: outcome_conflict"))
        StagedRow.objects.filter(pk=self.row(job, "deal", "d9").pk).update(
            mapping_state=StagedRow.MappingState.PROPOSED, error="")
        apply_job(job, ("deal",), d9="lost")
        eng = engagement("d9")
        self.assertEqual((eng.status, eng.stage.key), ("lost", "referred"))  # closed, unhinted: keeps it


class RuleClosedStageTests(DealScenario):
    """2.5 rule 2 (hand-off 2): closed deals keep the stage they closed from."""

    def test_with_history_the_last_open_stage(self):
        self.imported()
        eng = engagement("d2")
        self.assertEqual(eng.stage.key, "negotiation")  # lost at Negotiation, not "in Closed lost"

    def test_without_history_in_a_won_stage_null_and_the_key_kept(self):
        self.imported()
        eng = engagement("d3")
        self.assertIsNone(eng.stage_id)
        self.assertIsNone(eng.stage_entered_at)
        self.assertEqual(eng.metadata[f"{PROVIDER}.import_closed_stage"], "closedwon")

    def test_without_history_in_a_working_stage_that_stage(self):
        self.imported()
        self.assertEqual(engagement("d4").stage.key, "qualified")


class RuleClosedAtTests(DealScenario):
    """2.5 rule 3: closed_at from the close date, else inferred and flagged; never now."""

    def test_close_date_is_used(self):
        self.imported()
        self.assertEqual(engagement("d2").closed_at, at("2024-04-15T12:00:00Z"))

    def test_inferred_from_the_last_stage_change_else_the_last_update(self):
        job = self.imported()
        self.assertEqual(engagement("d5").closed_at, at("2024-03-03T09:00:00Z"))
        self.assertEqual(engagement("d4").closed_at, at("2024-06-01T09:00:00Z"))  # updated_at
        for sid in ("d4", "d5"):
            self.assertIn("closed_at_inferred", self.row(job, "deal", sid).proposed_mapping["flags"])
            self.assertIn("closed_at_inferred", engagement(sid).metadata[f"{PROVIDER}.import_flags"])
        self.assertNotIn("closed_at_inferred", engagement("d2").metadata.get(f"{PROVIDER}.import_flags", []))

    def test_open_engagements_have_no_closed_at(self):
        self.imported()
        self.assertIsNone(engagement("d1").closed_at)


class RuleAmountCurrencyTests(DealScenario):
    """2.5 rule 4 (hand-off 7): missing amount is not zero; the currency rule."""

    def test_missing_amount_is_null_never_zero(self):
        self.imported()
        self.assertIsNone(engagement("d3").expected_value)
        self.assertEqual(engagement("d1").expected_value, Decimal("1200.50"))

    def test_blank_currency_takes_the_mapping_default(self):
        self.imported()
        self.assertEqual(engagement("d3").currency, "EUR")
        self.assertEqual(engagement("d1").currency, "USD")

    def test_no_currency_and_no_default_refuses_the_job_at_validation(self):
        export = Export(self, self.records)
        job = staging.stage(self.co, export.path, mapping={k: v for k, v in self.mapping.items()
                                                           if k != "default_currency"})
        self.assertEqual(job.status, ImportJob.Status.INVALID)
        self.assertIn("default_currency", " ".join(job.report["mapping"]["errors"]))
        self.assertFalse(Engagement.objects.exists())

    def test_more_than_two_decimals_is_rounded_and_flagged_negative_refused(self):
        records = with_deals(self.records, [deal("d9", amount="10.005"), deal("d10", amount="-1")])
        job = self.stage(records)
        apply_job(job)
        eng = engagement("d9")
        self.assertEqual(eng.expected_value, Decimal("10.01"))
        self.assertIn("amount_rounded", eng.metadata[f"{PROVIDER}.import_flags"])
        self.assertTrue(self.row(job, "deal", "d10").error.startswith("invalid_value: "))


class RuleLossReasonTests(DealScenario):
    """2.5 rule 5: mapped loss reasons; the rest is ``unknown`` with the text kept."""

    def test_mapped_and_unmapped(self):
        self.imported()
        d2, d4 = engagement("d2"), engagement("d4")
        self.assertEqual((d2.loss_reason, d2.loss_notes), ("price", ""))
        self.assertEqual((d4.loss_reason, d4.loss_notes), ("unknown", "Gremlins"))
        self.assertEqual(engagement("d3").loss_reason, "")


class RuleHistoryTests(DealScenario):
    """2.5 rule 6 (hand-off 3): history as ``EngagementStageChange(source="import")``."""

    def test_open_deal_history_rows_and_stage_entered_at(self):
        self.imported()
        eng = engagement("d1")
        self.assertEqual(history_of(eng), [
            (None, "appointment", "", "open", at("2024-01-01T09:00:00Z"), "import"),
            ("appointment", "qualified", "open", "open", at("2024-02-01T09:00:00Z"), "import"),
            ("qualified", "negotiation", "open", "open", at("2024-03-01T09:00:00Z"), "import"),
        ])
        self.assertEqual(eng.stage_entered_at, at("2024-03-01T09:00:00Z"))
        self.assertTrue(all(r.changed_by_id is None for r in eng.stage_changes.all()))

    def test_closing_keeps_the_stage_at_closed_at(self):
        self.imported()
        self.assertEqual(history_of(engagement("d2")), [
            (None, "appointment", "", "open", at("2024-01-01T09:00:00Z"), "import"),
            ("appointment", "negotiation", "open", "open", at("2024-02-01T09:00:00Z"), "import"),
            ("negotiation", "negotiation", "open", "lost", at("2024-04-15T12:00:00Z"), "import"),
        ])

    def test_no_history_shapes(self):
        self.imported()
        self.assertEqual(history_of(engagement("d3")), [  # rule 7
            (None, None, "", "won", at("2024-05-01T12:00:00Z"), "import")])
        self.assertEqual(history_of(engagement("d4")), [  # creation at the stage, then the close
            (None, "qualified", "", "open", at("2024-01-01T09:00:00Z"), "import"),
            ("qualified", "qualified", "open", "lost", at("2024-06-01T09:00:00Z"), "import")])

    def test_every_history_is_one_strictly_increasing_chain_ending_at_the_engagement(self):
        self.imported()
        for eng in Engagement.objects.filter(company=self.co):
            rows = list(eng.stage_changes.order_by("changed_at", "created_at"))
            self.assertTrue(rows, eng.number)
            self.assertEqual(rows[0].from_status, "")
            for a, b in zip(rows, rows[1:]):
                self.assertLess(a.changed_at, b.changed_at)
                self.assertEqual((a.to_pipeline_id, a.to_stage_id, a.to_status),
                                 (b.from_pipeline_id, b.from_stage_id, b.from_status))
            last = rows[-1]
            self.assertEqual((last.to_pipeline_id, last.to_stage_id, last.to_status),
                             (eng.pipeline_id, eng.stage_id, eng.status))

    def test_an_unmapped_history_stage_is_skipped_and_kept(self):
        records = with_deals(self.records, [deal("d9", stage="p1-qualified", history=hist(
            ("dealstage", "p1-appointment", "2024-01-01T09:00:00Z"),
            ("dealstage", "deleted-stage", "2024-01-15T09:00:00Z"),
            ("dealstage", "p1-qualified", "2024-02-01T09:00:00Z")))])
        job = self.stage(records)
        apply_job(job)
        eng = engagement("d9")
        self.assertEqual([r[:2] for r in history_of(eng)], [(None, "appointment"), ("appointment", "qualified")])
        self.assertEqual(eng.metadata[f"{PROVIDER}.import_unmapped_history"],
                         ["dealstage=deleted-stage@2024-01-15T09:00:00+00:00"])
        self.assertFalse(Stage.objects.filter(key="deleted-stage").exists())  # never a placeholder

    def test_created_at_is_the_sources(self):
        self.imported()
        self.assertEqual(engagement("d1").created_at, at("2024-01-01T09:00:00Z"))

    def test_the_stage_report_times_nothing_and_counts_the_footnote(self):
        from reporting.crm_reports import stage_duration

        self.imported()
        pipeline_ = Pipeline.objects.get(company=self.co, key="deals")
        report = stage_duration(pipeline_, Engagement.objects.filter(company=self.co),
                                dt.date(2023, 1, 1), dt.date(2025, 12, 31))
        self.assertTrue(all(not r.durations for r in report["rows"]))
        self.assertGreater(report["excluded"], 0)


class RuleFollowUpTests(DealScenario):
    """2.5 rule 7 (hand-off 4): follow-up fields become one task, not many."""

    def test_one_task_due_at_the_latest_value_titled_by_the_next_step(self):
        self.imported()
        eng = engagement("d1")
        task = Task.objects.get(engagement=eng)
        self.assertEqual((task.title, task.kind, task.status), ("Send revised quote", "follow_up", "open"))
        self.assertEqual(task.source, f"import:{PROVIDER}:follow_up")
        self.assertEqual(task.due_at, at("2024-05-01T14:00:00Z"))
        self.assertFalse(task.all_day)
        self.assertEqual(task.assignee_id, eng.owner_id)
        self.assertEqual(eng.next_task_id, task.pk)  # recompute_next_action ran
        self.assertEqual(eng.next_action_due_at, task.due_at)

    def test_already_due_is_pre_notified_future_is_not(self):
        from crm.due import due_candidates

        self.imported()
        overdue = Task.objects.get(engagement=engagement("d1"))
        future = Task.objects.get(engagement=engagement("d8"))
        self.assertIsNotNone(overdue.due_notified_at)
        self.assertIsNone(future.due_notified_at)
        self.assertTrue(future.all_day)
        self.assertEqual(future.title, "Follow up")
        self.assertNotIn(overdue.pk, [t.pk for t in due_candidates(timezone.now())])

    def test_closed_engagements_and_engagements_without_a_value_get_none(self):
        self.imported()
        self.assertFalse(Task.objects.filter(engagement=engagement("d2")).exists())  # closed, had a value
        self.assertFalse(Task.objects.filter(engagement=engagement("d6")).exists())  # open, no value
        self.assertEqual(Task.objects.filter(company=self.co).count(), 2)

    def test_the_original_values_stay_in_the_sidecar(self):
        records = with_deals(self.records, [deal("d9", follow_up=[("hs_next_step", "Call")],
                                                properties={"hs_next_step": "Call"})])
        job = self.stage(records)
        apply_job(job)
        self.assertEqual(engagement("d9").metadata[f"{PROVIDER}.hs_next_step"], "Call")


class RulePartyTests(DealScenario):
    """2.5 rule 8: ``Engagement.party``, never guessed."""

    def test_the_primary_company_extra_companies_listed(self):
        self.imported()
        eng = engagement("d1")
        self.assertEqual(eng.party.display_name, "Initech")
        self.assertEqual(eng.metadata[f"{PROVIDER}.import_other_companies"], ["11"])

    def test_no_company_one_contact(self):
        self.imported()
        self.assertEqual(engagement("d3").party.given_name, "Bob")

    def test_several_contacts_the_primary_association_else_null_flagged(self):
        job = self.imported()
        self.assertEqual(engagement("d7").party.given_name, "Ada")
        d6 = engagement("d6")
        self.assertIsNone(d6.party_id)
        self.assertIn("party_ambiguous", self.row(job, "deal", "d6").proposed_mapping["flags"])
        self.assertIn("party_ambiguous", d6.metadata[f"{PROVIDER}.import_flags"])

    def test_no_association_null(self):
        self.imported()
        self.assertIsNone(engagement("d8").party_id)

    def test_a_contact_still_in_review_is_dependency_missing(self):
        job = self.stage()
        apply_job(job, ("owner", "pipeline", "company"))
        apply_job(job, ("deal",))
        row = self.row(job, "deal", "d3")
        self.assertTrue(row.error.startswith("dependency_missing: contact 22"), row.error)


class RuleOwnerOriginTests(DealScenario):
    """2.5 rule 9: origin import, number minted, owner from owner_ref (unresolved: NULL, flagged)."""

    def test_origin_number_owner(self):
        self.imported()
        eng = engagement("d1")
        self.assertEqual(eng.origin, "import")
        self.assertTrue(eng.number.startswith("EN-"))
        self.assertEqual(eng.owner.display_name, "Olive Owner")
        self.assertEqual(eng.title, "Widget retrofit")
        self.assertTrue(engagement("d6").title)  # minted by save() when the source has none

    def test_an_owner_missing_from_the_export_is_null_and_flagged(self):
        records = with_deals(self.records, [deal("d9", owner_ref=ref("owner", 999))])
        job = self.stage(records, coverage={"owner": "partial"})
        self.assertIn("owner_unresolved", self.row(job, "deal", "d9").proposed_mapping["flags"])
        apply_job(job)
        eng = engagement("d9")
        self.assertIsNone(eng.owner_id)
        self.assertIn("owner_unresolved", eng.metadata[f"{PROVIDER}.import_flags"])


class RuleNoSaleDocumentTests(DealScenario):
    """2.5 rule 10 (6.2): an imported deal never creates a Quote or an Order."""

    def test_no_quote_no_order(self):
        from orders.models import Order, Quote

        self.imported()
        self.assertTrue(Engagement.objects.filter(company=self.co, status="won").exists())
        self.assertFalse(Quote._base_manager.exists())
        self.assertFalse(Order._base_manager.exists())


# --- associations, tickets --------------------------------------------------------


class AssociationTests(DealScenario):
    def test_deal_contacts_with_roles_and_the_hub_person_skipped(self):
        job = self.imported()
        failed = [(r.raw["source_id"], r.error) for r in rows_of(job, "association") if r.error]
        self.assertEqual(failed, [])
        d1 = engagement("d1")
        contacts = {c.party.given_name: (c.role, c.is_primary) for c in d1.contacts.select_related("party")}
        self.assertEqual(contacts, {"Grace": ("decision_maker", False), "Ada": ("champion", True)})
        self.assertFalse(engagement("d3").contacts.exists())  # Bob is the party
        self.assertEqual(list(engagement("d7").contacts.values_list("party__given_name", "role")),
                         [("Grace", "influencer")])  # the label names a role, any case
        for sid in ("a3", "a4", "a7"):  # link-only rows still record their ref
            self.assertTrue(ExternalRef.objects.filter(source=SOURCE, source_id=sid).exists(), sid)
        a10 = self.row(job, "association", "a10")  # its contact is archived (N/A): a no-op, not a retry loop
        self.assertEqual((a10.mapping_state, a10.applied_ref),
                         (StagedRow.MappingState.APPLIED, f"crm_import.importjob:{job.pk}"))


class TicketTests(DealScenario):
    def test_tickets_are_relationships_and_no_engagement(self):
        job = self.imported()
        self.assertEqual([r.error for r in rows_of(job, "relationship") if r.error], [])
        rels = PartyRelationship.objects.filter(company=self.co, kind="partnership")
        self.assertEqual(rels.count(), 3)
        initech = Party.objects.get(company=self.co, display_name="Initech")
        self.assertTrue(rels.filter(from_party__in=[initech, self.me], to_party__in=[initech, self.me]).exists())
        grace = Party.objects.get(company=self.co, given_name="Grace")
        rel = rels.get(from_party__in=[grace, self.me], to_party__in=[grace, self.me])
        self.assertLess(str(rel.from_party_id), str(rel.to_party_id))  # symmetric: canonical order
        self.assertEqual((rel.valid_from, rel.valid_to), (dt.date(2023, 1, 10), None))
        self.assertEqual(rel.source, f"import:{PROVIDER}")
        self.assertEqual(rel.metadata[f"{PROVIDER}.import_ticket_stage"], "new")
        ended = rels.get(valid_to__isnull=False)
        self.assertEqual(ended.valid_to, dt.date(2024, 2, 1))  # stage in ended_stages
        ticket_ref = ExternalRef.objects.get(source=SOURCE, object_type="ticket", source_id="50")
        self.assertEqual(ticket_ref.metadata["source_ticket"]["description"], "Refers patent work to us.")
        self.assertFalse(ExternalRef.objects.filter(object_type="ticket_engagement").exists())
        self.assertFalse(Pipeline.objects.filter(company=self.co, key="partners").exists())
        self.assertEqual(Engagement.objects.filter(company=self.co).count(), 8)

    def test_tickets_as_engagements(self):
        job = self.stage(options={"tickets_as_engagements": True})
        prow = self.row(job, "pipeline", "t1")
        staging.approve_row(prow, ACTOR)
        StagedRow.objects.filter(pk=prow.pk).update(
            mapped_values={**prow.mapped_values, "stage_outcomes": {"ended": "lost"}})
        apply_job(job)
        open_t = engagement("50", object_type="ticket_engagement")
        closed_t = engagement("51", object_type="ticket_engagement")
        self.assertEqual((open_t.pipeline.key, open_t.status, open_t.party.given_name), ("partners", "open", "Grace"))
        self.assertEqual((closed_t.status, closed_t.loss_reason, closed_t.stage_id), ("lost", "unknown", None))
        self.assertEqual(PartyRelationship.objects.filter(company=self.co, kind="partnership").count(), 3)

    def test_a_closed_ticket_in_an_unhinted_stage_is_never_stored_open(self):
        job = self.stage(options={"tickets_as_engagements": True})
        prow = self.row(job, "pipeline", "t1")
        staging.approve_row(prow, ACTOR)
        StagedRow.objects.filter(pk=prow.pk).update(
            mapped_values={**prow.mapped_values, "stage_outcomes": {"ended": ""}})  # the reviewer leaves it
        apply_job(job)
        row = self.row(job, "relationship", "51")
        self.assertTrue(row.error.startswith("invalid_value: outcome_conflict"), row.error)
        staging.approve_row(row, ACTOR, outcome="won")  # a failed row is re-decided with an outcome
        apply_job(job, ("relationship",))
        eng = engagement("51", object_type="ticket_engagement")
        self.assertEqual((eng.status, eng.stage.key), ("won", "ended"))

    def test_tickets_as_engagements_need_a_default_currency(self):
        export = Export(self, self.records)
        mapping = {k: v for k, v in self.mapping.items() if k != "default_currency"}
        records = with_deals(self.records, [d for d in self.records["deal"] if d["data"]["currency"]])
        export = Export(self, records)
        job = staging.stage(self.co, export.path, mapping=mapping, options={"tickets_as_engagements": True})
        self.assertEqual(job.status, ImportJob.Status.INVALID)
        self.assertIn("ticket", " ".join(job.report["mapping"]["errors"]))


# --- the done-when checks and the safety net --------------------------------------------


class InvariantTests(DealScenario):
    def test_check_engagements_is_clean_after_import(self):
        self.imported()
        out = io.StringIO()
        call_command("check_engagements", stdout=out)
        self.assertIn("0 violation(s)", out.getvalue())

    def test_reconcile_engagements_finds_no_stale_next_action(self):
        self.imported()
        out = io.StringIO()
        call_command("reconcile_engagements", "--check", stdout=out)  # exit 0: no drift of any kind
        self.assertIn("drift: 0 engagement(s)", out.getvalue())

    def test_nothing_is_announced(self):
        from core.plugins import signals

        received = []

        def receiver(sender, **kwargs):
            received.append(sender)

        names = ("engagement_created", "engagement_won", "engagement_lost", "engagement_stage_changed",
                 "task_created", "party_created", "party_updated")
        for name in names:
            sig = getattr(signals, name)
            sig.connect(receiver, weak=False, dispatch_uid=f"deals-{name}")
            self.addCleanup(sig.disconnect, dispatch_uid=f"deals-{name}")
        with self.captureOnCommitCallbacks(execute=True):
            job = self.imported()
            writelog.undo_job(job, actor=ACTOR)
        self.assertEqual(received, [])
        self.assertFalse(dispatch.is_suppressed())

    def test_tenant_isolation(self):
        job = self.imported()
        self.assertFalse(Engagement._base_manager.filter(company=self.other).exists())
        self.assertFalse(EngagementStageChange._base_manager.exclude(company=self.co).exists())
        self.assertFalse(ImportWriteLog.objects.exclude(company=self.co).exists())
        # A decision naming another tenant's engagement is refused.
        foreign = engagements.create(company=self.other, title="Theirs")
        row = self.row(job, "deal", "d8")
        StagedRow.objects.filter(pk=row.pk).update(
            mapped_values={**row.mapped_values, "decision": "update",
                           "target": {"model": "crm.engagement", "id": str(foreign.pk)}},
            mapping_state=StagedRow.MappingState.APPROVED)
        ApprovalRow.objects.filter(staged_row=row).delete()
        apply_type(job, "deal")
        row.refresh_from_db()
        self.assertTrue(row.error.startswith("conflict_stale: "), row.error)
        foreign.refresh_from_db()
        self.assertEqual(foreign.title, "Theirs")


class UndoTests(DealScenario):
    def test_apply_then_undo_job_restores_the_snapshot(self):
        job = self.imported()
        self.assertNotEqual(dump(), self.before)
        report = writelog.undo_job(job, actor=ACTOR)
        self.assertEqual(report.refused, [])
        self.assertEqual(dump(), self.before)

    def test_apply_then_undo_runs_newest_batch_first(self):
        from unify.apply import undo_run

        job = self.stage()
        runs = apply_job(job)
        for otype in reversed(STEPS):
            out = undo_run(str(runs[otype].pk), runs[otype].undo_token)
            self.assertEqual(out["refused"], [], otype)
        self.assertEqual(dump(), self.before)

    def test_an_engagement_staff_moved_refuses_undo_and_the_rest_is_undone(self):
        job = self.imported()
        eng = engagement("d8")
        engagements.move(eng, eng.pipeline.stages.get(key="qualified"))
        report = writelog.undo_job(job, actor=ACTOR)
        self.assertTrue(report.refused)
        self.assertTrue(Engagement.objects.filter(pk=eng.pk).exists())
        self.assertFalse(Engagement.objects.filter(pk=engagement_pk_or_none("d1")).exists())


def engagement_pk_or_none(source_id):
    ref_ = ExternalRef.objects.filter(source=SOURCE, object_type="deal", source_id=source_id).first()
    return ref_.object_id if ref_ else None


class ReimportTests(DealScenario):
    def test_a_changed_stage_and_outcome_go_through_the_services_and_undo_exactly(self):
        first = self.imported()
        snapshot = dump()
        records = dict(self.records)
        records["deal"] = [d if d["source_id"] != "d1" else deal(
            "d1", stage="p1-closedwon", status="won", closed_at="2024-07-01T09:00:00Z", amount="1500",
            company_refs=[10, 11], contact_refs=[20, 21], owner_ref=ref("owner", 2), title="Widget retrofit",
            follow_up=[("hs_next_step", "Send revised quote"), ("notes_next_activity_date", "2024-05-01T14:00:00Z")],
            history=self.records["deal"][0]["history"]) for d in self.records["deal"]]
        second = self.stage(records, previous_job=first)
        row = self.row(second, "deal", "d1")
        self.assertEqual(row.proposed_mapping["decision"], "update", row.proposed_mapping)
        apply_job(second)
        eng = engagement("d1")
        self.assertEqual((eng.status, eng.stage.key, eng.expected_value), ("won", "negotiation", Decimal("1500")))
        last = eng.stage_changes.order_by("changed_at").last()
        self.assertEqual((last.source, last.to_status), ("import", "won"))
        self.assertEqual(writelog.undo_job(second, actor=ACTOR).refused, [])
        self.assertEqual(dump(), snapshot)


class ErasureTests(DealScenario):
    def test_an_erased_contact_is_never_a_counterparty_or_a_contact(self):
        from parties.erasure import erase_party

        job = self.stage()
        apply_job(job, ("owner", "pipeline", "company", "contact"))
        bob = Party.objects.get(company=self.co, given_name="Bob")
        erase_party(bob, actor="test", reason="request")
        apply_job(job, ("deal", "association"))
        self.assertIsNone(engagement("d3").party_id)
        a7 = self.row(job, "association", "a7")
        self.assertTrue(a7.error.startswith("erased: "), a7.error)

    def test_a_staff_edit_after_the_decision_fails_conflict_stale(self):
        first = self.imported()
        records = with_deals(self.records, [d if d["source_id"] != "d8" else deal("d8", title="New title",
                             follow_up=d["data"]["follow_up"] and [(i["field"], i["value"])
                                                                   for i in d["data"]["follow_up"]])
                                            for d in self.records["deal"]])
        second = self.stage(records, previous_job=first)
        self.assertEqual(self.row(second, "deal", "d8").proposed_mapping["decision"], "update")
        Engagement._base_manager.filter(pk=engagement("d8").pk).update(title="Staff title")
        apply_job(second, ("deal",))
        row = self.row(second, "deal", "d8")
        self.assertTrue(row.error.startswith("conflict_stale: "), row.error)
        self.assertEqual(engagement("d8").title, "Staff title")


# --- record_imported_history, unit --------------------------------------------------------


class RecordImportedHistoryTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(name="Tenant A")
        self.pipeline = Pipeline.objects.create(company=self.co, key="deals", name="Deals", sort_order=1)
        self.a, self.b = (Stage.objects.create(pipeline=self.pipeline, company=self.co, key=k, name=k, sort_order=i)
                          for i, k in enumerate(("a", "b")))
        self.won = Stage.objects.create(pipeline=self.pipeline, company=self.co, key="won", name="Won",
                                        sort_order=9, outcome_hint="won")

    def insert(self, stage, status="open", closed_at=None):
        with dispatch.suppressed():
            eng = Engagement(company=self.co, pipeline=self.pipeline, stage=stage, status=status,
                             closed_at=closed_at, origin="import", currency="USD")
            eng.save()
        return eng

    def row(self, from_stage, to_stage, from_status, to_status, when, *, from_pipeline=True):
        return {"from_pipeline": self.pipeline if from_pipeline and from_status else None,
                "to_pipeline": self.pipeline, "from_stage": from_stage, "to_stage": to_stage,
                "from_status": from_status, "to_status": to_status, "changed_at": at(when)}

    def test_writes_import_rows_and_stage_entered_at(self):
        eng = self.insert(self.b)
        rows = engagements.record_imported_history(eng, [
            self.row(None, self.a, "", "open", "2024-01-01T00:00:00Z"),
            self.row(self.a, self.b, "open", "open", "2024-02-01T00:00:00Z"),
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.source for r in eng.stage_changes.all()}, {"import"})
        eng.refresh_from_db()
        self.assertEqual(eng.stage_entered_at, at("2024-02-01T00:00:00Z"))

    def test_refusals(self):
        eng = self.insert(self.b)
        good = [self.row(None, self.a, "", "open", "2024-01-01T00:00:00Z"),
                self.row(self.a, self.b, "open", "open", "2024-02-01T00:00:00Z")]
        cases = {
            "empty": [],
            "not increasing": [good[0], {**good[1], "changed_at": good[0]["changed_at"]}],
            "no creation row": [good[1]],
            "broken chain": [good[0], {**good[1], "from_stage": self.b}],
            "wrong end": [good[0]],
            "source passed": [{**good[0], "source": "erp"}, good[1]],
            "naive": [{**good[0], "changed_at": dt.datetime(2024, 1, 1)}, good[1]],
            "open with no stage": [good[0], {**good[1], "to_stage": None},
                                   {**good[1], "from_stage": None, "changed_at": at("2024-03-01T00:00:00Z")}],
        }
        other = Pipeline.objects.create(company=self.co, key="other", name="Other", sort_order=2)
        foreign_stage = Stage.objects.create(pipeline=other, company=self.co, key="x", name="x", sort_order=1)
        cases["stage of another pipeline"] = [{**good[0], "to_stage": foreign_stage}, good[1]]
        for name, changes in cases.items():
            with self.subTest(name), self.assertRaises(ValidationError):
                engagements.record_imported_history(eng, changes)
        self.assertFalse(eng.stage_changes.exists())  # nothing written by any refusal

    def test_refuses_an_engagement_with_history(self):
        eng = engagements.create(company=self.co, pipeline=self.pipeline, stage=self.a)
        with self.assertRaises(ValidationError):
            engagements.record_imported_history(eng, [self.row(None, self.a, "", "open", "2024-01-01T00:00:00Z")])

    def test_rule_7_closed_stage_unknown(self):
        eng = self.insert(None, status="won", closed_at=at("2024-03-01T00:00:00Z"))
        engagements.record_imported_history(eng, [self.row(None, None, "", "won", "2024-03-01T00:00:00Z")])
        eng.refresh_from_db()
        self.assertIsNone(eng.stage_entered_at)

    def test_sends_no_signal_outside_suppressed(self):
        from core.plugins import signals

        received = []
        signals.engagement_stage_changed.connect(lambda sender, **kw: received.append(1), weak=False,
                                                 dispatch_uid="rih-test")
        self.addCleanup(signals.engagement_stage_changed.disconnect, dispatch_uid="rih-test")
        eng = self.insert(self.b)
        with self.captureOnCommitCallbacks(execute=True):
            engagements.record_imported_history(eng, [
                self.row(None, self.a, "", "open", "2024-01-01T00:00:00Z"),
                self.row(self.a, self.b, "open", "open", "2024-02-01T00:00:00Z")])
        self.assertEqual(received, [])


class PlanHistoryTests(TestCase):
    """Hand-off section 3, rule by rule, through ``plan_history`` (pure planning)."""

    def setUp(self):
        self.co = Company.objects.create(name="Tenant A")
        self.p = Pipeline.objects.create(company=self.co, key="deals", name="Deals", sort_order=1)
        self.q = Pipeline.objects.create(company=self.co, key="other", name="Other", sort_order=2)
        self.stages = {}
        for pipe in (self.p, self.q):
            for i, (k, hint) in enumerate((("a", ""), ("b", ""), ("won", "won"), ("lost", "lost"))):
                self.stages[(pipe.key, k)] = Stage.objects.create(
                    pipeline=pipe, company=self.co, key=k, name=k, sort_order=i, outcome_hint=hint)

    def plan(self, entries, *, current="a", status="open", closed_at=None, pipeline=None):
        pipeline = pipeline or self.p
        pipes = {"P": self.p, "Q": self.q}
        return deal_applier.plan_history(
            entries=hist(*entries), stage_props=("dealstage",), pipeline_props=("pipeline",), pipeline=pipeline,
            current_stage=self.stages[(pipeline.key, current)], status=status,
            closed_at=at(closed_at) if closed_at else None, created_at=at("2023-12-01T00:00:00Z"),
            pipeline_of=pipes.get, stage_of=lambda pipe, sid: self.stages.get((pipe.key, sid)) if pipe else None,
        )

    def shape(self, plan):
        def key(s):
            return s.key if s is not None else None
        return [(key(c["from_stage"]), key(c["to_stage"]), c["from_status"], c["to_status"],
                 c["to_pipeline"].key) for c in plan.changes]

    def test_rules_1_2_creation_and_moves_noop_skipped(self):
        plan = self.plan([("dealstage", "a", "2024-01-01T00:00:00Z"), ("dealstage", "a", "2024-01-02T00:00:00Z"),
                          ("dealstage", "b", "2024-01-03T00:00:00Z")], current="b")
        self.assertEqual(self.shape(plan), [(None, "a", "", "open", "deals"), ("a", "b", "open", "open", "deals")])

    def test_rule_3_pipeline_change_is_one_row(self):
        plan = self.plan([("pipeline", "P", "2024-01-01T00:00:00Z"), ("dealstage", "a", "2024-01-01T00:00:00Z"),
                          ("pipeline", "Q", "2024-02-01T00:00:00Z"),
                          ("dealstage", "b", "2024-02-01T00:00:00Z")], current="b", pipeline=self.q)
        self.assertEqual(self.shape(plan), [(None, "a", "", "open", "deals"), ("a", "b", "open", "open", "other")])
        self.assertEqual(plan.changes[1]["from_pipeline"], self.p)

    def test_rule_4_closing_keeps_the_stage_at_closed_at_clamped(self):
        plan = self.plan([("dealstage", "a", "2024-01-01T00:00:00Z"), ("dealstage", "b", "2024-02-01T00:00:00Z"),
                          ("dealstage", "won", "2024-03-01T00:00:00Z")], current="won", status="won",
                         closed_at="2024-01-15T00:00:00Z")  # before the last move: clamped
        self.assertEqual(self.shape(plan)[-1], ("b", "b", "open", "won", "deals"))
        self.assertEqual(plan.stage.key, "b")
        self.assertEqual(plan.changes[-1]["changed_at"], at("2024-02-01T00:00:00Z") + dt.timedelta(microseconds=1))

    def test_rules_5_and_6_reopen_and_won_to_lost(self):
        plan = self.plan([("dealstage", "a", "2024-01-01T00:00:00Z"), ("dealstage", "won", "2024-02-01T00:00:00Z"),
                          ("dealstage", "b", "2024-03-01T00:00:00Z"), ("dealstage", "won", "2024-04-01T00:00:00Z"),
                          ("dealstage", "lost", "2024-05-01T00:00:00Z")], current="lost", status="lost",
                         closed_at="2024-05-01T00:00:00Z")
        self.assertEqual(self.shape(plan), [
            (None, "a", "", "open", "deals"), ("a", "a", "open", "won", "deals"),
            ("a", "b", "won", "open", "deals"), ("b", "b", "open", "won", "deals"),
            ("b", "b", "won", "lost", "deals")])

    def test_rule_7_and_the_no_history_shapes(self):
        self.assertEqual(self.shape(self.plan([], current="won", status="won", closed_at="2024-05-01T00:00:00Z")),
                         [(None, None, "", "won", "deals")])
        self.assertEqual(self.shape(self.plan([], current="b", status="lost", closed_at="2024-05-01T00:00:00Z")),
                         [(None, "b", "", "open", "deals"), ("b", "b", "open", "lost", "deals")])
        self.assertEqual(self.shape(self.plan([], current="b")), [(None, "b", "", "open", "deals")])

    def test_reopened_after_the_last_entry_is_one_reopen_row(self):
        plan = self.plan([("dealstage", "a", "2024-01-01T00:00:00Z"), ("dealstage", "won", "2024-02-01T00:00:00Z")],
                         current="b")
        self.assertEqual(self.shape(plan), [(None, "a", "", "open", "deals"), ("a", "a", "open", "won", "deals"),
                                            ("a", "b", "won", "open", "deals")])  # never a stage move while won

    def test_a_stage_of_another_pipeline_without_a_pipeline_entry(self):
        only = Stage.objects.create(pipeline=self.p, company=self.co, key="p_only", name="p", sort_order=5)
        self.stages[("other", "q_only")] = Stage.objects.create(pipeline=self.q, company=self.co, key="q_only",
                                                               name="q", sort_order=5)
        plan = deal_applier.plan_history(
            entries=hist(("dealstage", "p_only", "2024-01-01T00:00:00Z"),
                         ("dealstage", "q_only", "2024-03-01T00:00:00Z")),
            stage_props=("dealstage",), pipeline_props=("pipeline",), pipeline=self.q,
            current_stage=self.stages[("other", "q_only")], status="open", closed_at=None,
            created_at=at("2023-12-01T00:00:00Z"), pipeline_of={"P": self.p, "Q": self.q}.get,
            stage_of=lambda pipe, sid: self.stages.get((pipe.key, sid)) if pipe else None,
            stage_elsewhere=lambda sid: only if sid == "p_only" else self.stages.get(("other", sid)))
        self.assertEqual(self.shape(plan), [(None, "p_only", "", "open", "deals"),
                                            ("p_only", "q_only", "open", "open", "other")])
        self.assertEqual(plan.unmapped, [])

    def test_ties_are_broken_by_a_microsecond(self):
        plan = self.plan([("dealstage", "a", "2024-01-01T00:00:00Z"), ("dealstage", "won", "2024-01-01T00:00:00Z")],
                         current="won", status="won", closed_at="2023-01-01T00:00:00Z")
        times = [c["changed_at"] for c in plan.changes]
        self.assertEqual(times[1] - times[0], dt.timedelta(microseconds=1))
