"""Tasks, communications and consent (phase5-importer.md 2.9-2.11; hand-off 4-6; step 6).

Done-when, one class each: re-ingest is a no-op (``ReingestTests``); overdue
tasks emit no ``task_due`` (``TaskDueTests``); an opt-out is a withdrawal and a
suppression and never a grant (``ConsentTests``); the 2.11 consequence is in
the report (``ConsentReportTests``). Also: apply then undo leaves every table
the appliers write equal to its snapshot, nothing is announced, an erased
address gets nothing, and another tenant is never touched.

The snapshot leaves out ``AuditLog`` and ``DocumentSequence``, as steps 4-5 do.
"""

from __future__ import annotations

import datetime as dt
import io

from django.core.management import call_command
from django.utils import timezone

from communications.models import Communication, CommunicationParticipant, CommunicationRaw
from core.models import ExternalRef
from core.plugins import dispatch
from crm.due import emit_due
from crm.models import Engagement, Task, TaskEvent
from crm_import import writelog
from crm_import.appliers import consent as consent_applier
from crm_import.canonical import make_record
from crm_import.models import ImportWriteLog
from parties.consent import address_hash, can_contact
from parties.models import ConsentRecord, Party, PartyEmail, Suppression
from unify.models import StagedRow

from .staging_helpers import SOURCE
from .test_appliers import ACTOR, ref, rows_of
from .test_appliers_deals import DealScenario, apply_job, deal, engagement
from .test_appliers_deals import dump as deal_dump

PROVIDER = SOURCE.split(":", 1)[0]
STEPS = ("owner", "pipeline", "company", "contact", "deal", "association", "relationship", "task",
         "communication", "consent")
ACTIVITY_MODELS = (Communication, CommunicationParticipant, CommunicationRaw, ConsentRecord, Suppression)
UTC = dt.UTC


def dump() -> dict:
    out = deal_dump()
    for model in ACTIVITY_MODELS:
        names = [f.attname for f in model._meta.concrete_fields]
        out[model._meta.label] = list(model._base_manager.order_by("pk").values_list(*names))
    return out


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def task(sid, *, title="Call back", status="open", due_at=None, due_is_date=False, completed_at=None, kind="todo",
         targets=(), owner_ref=None, priority=None, created_at="2024-02-01T09:00:00Z", body=None, properties=None):
    return make_record("task", source=SOURCE, source_id=str(sid), data={
        "title": title, "body_text": body, "kind": kind, "status": status, "priority": priority,
        "due_at": due_at, "due_is_date": due_is_date, "completed_at": completed_at, "owner_ref": owner_ref,
        "target_refs": [ref(t, s) for t, s in targets],
    }, created_at=created_at, updated_at=created_at, properties=properties or {})


def comm(sid, *, kind="note", direction="internal", status="completed", occurred_at="2024-03-05T10:00:00Z",
         participants=(), targets=(), payload=None, subject=None, body="Spoke about the retrofit.",
         source_object_type=None, owner_ref=None, properties=None, duration=None, ended_at=None):
    return make_record("communication", source=SOURCE, source_id=str(sid),
                       source_object_type=source_object_type or kind, data={
        "kind": kind, "direction": direction, "status": status, "subject": subject, "body_text": body,
        "body_html": None, "summary": None, "occurred_at": occurred_at, "ended_at": ended_at,
        "duration_seconds": duration, "owner_ref": owner_ref,
        "participants": [{"role": r, "address": a, "name": n} for r, a, n in participants],
        "target_refs": [ref(t, s) for t, s in targets],
        "raw": {"schema_version": 1, "provider": PROVIDER,
                "payload": payload if payload is not None else {"id": str(sid), "body": body}},
    }, properties=properties or {})


def fact(sid, address, *, state=None, suppress=None, purpose="marketing", occurred_at=None, evidence=None,
         subscription=None, source_object_type="contact", channel="email"):
    return make_record("consent", source=SOURCE, source_id=str(sid), source_object_type=source_object_type, data={
        "address": address, "channel": channel, "purpose": purpose if state else None, "state": state,
        "suppress": suppress, "occurred_at": occurred_at, "subscription": subscription, "evidence": evidence,
    })


class ActivityScenario(DealScenario):
    """The deal scenario plus tasks, communications and consent facts for every rule."""

    mapping = {**DealScenario.mapping, "communication": {"raw_schema_version": 1}}

    def setUp(self):
        super().setUp()
        self.before = dump()

    def build_records(self) -> dict:
        records = super().build_records()
        now = timezone.now()
        self.overdue_at = iso(now - dt.timedelta(days=40))
        self.future_at = iso(now + dt.timedelta(days=3))
        records["deal"] = [*records["deal"], deal("d10", contact_refs=[22])]  # Bob's only open engagement
        records["task"] = [
            task("t1", due_at=self.overdue_at, targets=[("deal", "d1"), ("contact", 20)], owner_ref=ref("owner", 2)),
            task("t2", status="in_progress", due_at=self.future_at, targets=[("deal", "d1")], kind="call"),
            task("t3", status="waiting", due_at="2024-03-01", due_is_date=True, targets=[("contact", 21)]),
            task("t4", status="done", due_at="2024-02-10T09:00:00Z", completed_at="2024-02-09T16:00:00Z",
                 targets=[("deal", "d1")]),
            task("t5", kind="carrier_pigeon", targets=[("company", 10)], priority="high"),
            task("t6", due_at=self.overdue_at, targets=[("deal", "d8")]),
        ]
        records["communication"] = [
            comm("n1", targets=[("deal", "d1"), ("contact", 20)], occurred_at="2024-06-02T10:00:00Z"),
            comm("n2", targets=[("contact", 22)]),  # Bob has exactly one open engagement: never guessed
            comm("e1", kind="email", direction="outbound", subject="Quote", source_object_type="email",
                 participants=[("from", "olive@example.com", "Olive"), ("to", "grace@example.com", "Grace")],
                 targets=[("contact", 20)]),
            comm("c1", kind="call", direction="inbound", status=None, duration=300,
                 occurred_at="2024-03-07T15:00:00Z", participants=[("caller", "+1 202 555 0143", None)],
                 targets=[("deal", "d1")]),
        ]
        records["consent"] = [
            fact("optout:20", "grace@example.com", state="withdrawn", suppress="opt_out",
                 evidence="hs_email_optout=true"),
            fact("bounce:22", "bob@example.com", suppress="hard_bounce"),
            fact("subscription:9:ada", "ada@example.com", state="granted", occurred_at="2024-01-03T09:00:00Z",
                 evidence='{"legalBasis": "CONSENT_WITH_NOTICE"}', subscription={"id": "9", "name": "Newsletter"},
                 source_object_type="subscription_status"),
            fact("subscription:9:bob", "bob@example.com", state="granted", occurred_at="2024-01-03T09:00:00Z",
                 evidence='{"legalBasis": "CONSENT_WITH_NOTICE"}', source_object_type="subscription_status"),
            fact("subscription:8:ada", "ada@example.com", state="granted", occurred_at="2024-01-04T09:00:00Z",
                 source_object_type="subscription_status"),  # no basis: nothing
            fact("optout:99", "nobody@example.com", state="withdrawn", suppress="opt_out"),
        ]
        return records

    def task_of(self, sid) -> Task:
        r = ExternalRef.objects.get(source=SOURCE, object_type="task", source_id=sid)
        return Task._base_manager.get(pk=r.object_id)

    def comm_of(self, sid, otype="note") -> Communication:
        r = ExternalRef.objects.get(source=SOURCE, object_type=otype, source_id=sid)
        return Communication._base_manager.get(pk=r.object_id)

    def person(self, given) -> Party:
        return Party.objects.get(company=self.co, given_name=given)

    def imported(self, **picks):
        job = self.stage()
        apply_job(job, STEPS, **picks)
        return job

    def errors(self, job, otype):
        return {str(r.raw.get("source_id")): r.error for r in rows_of(job, otype) if r.error}


# --- tasks (2.9, hand-off 4) ----------------------------------------------------------


class TaskTests(ActivityScenario):
    def test_status_map_kind_priority_and_links(self):
        job = self.imported()
        self.assertEqual(self.errors(job, "task"), {})
        t1, t2, t3, t4, t5 = (self.task_of(s) for s in ("t1", "t2", "t3", "t4", "t5"))
        self.assertEqual([t.status for t in (t1, t2, t3, t4)], ["open", "in_progress", "waiting", "done"])
        self.assertEqual(t1.engagement, engagement("d1"))
        self.assertEqual(t1.party, self.person("Grace"))  # the explicit contact, not the engagement's party
        self.assertEqual(t1.assignee.display_name, "Olive Owner")
        self.assertEqual(t2.kind, "call")
        self.assertIsNone(t3.engagement)
        self.assertEqual(t3.party, self.person("Ada"))
        self.assertEqual((t5.kind, t5.priority), ("todo", "high"))
        self.assertEqual(t5.metadata[f"{PROVIDER}.import_kind"], "carrier_pigeon")
        self.assertEqual(t1.source, f"import:{PROVIDER}")

    def test_done_task_is_history_with_source_timestamps(self):
        self.imported()
        t4 = self.task_of("t4")
        self.assertEqual(t4.completed_at, dt.datetime(2024, 2, 9, 16, tzinfo=UTC))
        self.assertEqual(t4.created_at, dt.datetime(2024, 2, 1, 9, tzinfo=UTC))
        events = list(TaskEvent.objects.filter(task=t4).order_by("at").values_list("event", "at"))
        self.assertEqual(events, [("created", dt.datetime(2024, 2, 1, 9, tzinfo=UTC)),
                                  ("completed", dt.datetime(2024, 2, 9, 16, tzinfo=UTC))])
        self.assertFalse(TaskEvent.objects.filter(event="rescheduled").exists())

    def test_date_only_due_is_all_day_at_the_end_of_that_local_day(self):
        from crm.tasks import end_of_local_day, zone_for

        self.imported()
        t3 = self.task_of("t3")
        self.assertTrue(t3.all_day)
        self.assertEqual(t3.due_at, end_of_local_day(dt.date(2024, 3, 1), zone_for(None, self.co)))
        self.assertTrue(self.task_of("t1").due_tz)
        self.assertFalse(self.task_of("t1").all_day)

    def test_next_action_is_the_earliest_open_task(self):
        self.imported()
        eng = engagement("d1")
        open_tasks = Task.objects.filter(engagement=eng, status__in=("open", "in_progress", "waiting"))
        earliest = open_tasks.order_by("due_at").first()
        self.assertEqual(eng.next_task_id, earliest.pk)
        out = io.StringIO()
        call_command("reconcile_engagements", "--check", stdout=out)
        self.assertIn("drift: 0 engagement(s)", out.getvalue())

    def test_a_task_whose_deal_is_still_in_review_waits(self):
        job = self.stage()
        apply_job(job, ("owner", "pipeline", "company", "contact"))
        apply_job(job, ("task",))
        self.assertTrue(self.errors(job, "task")["t1"].startswith("dependency_missing: "))
        self.assertNotIn("t3", self.errors(job, "task"))  # a contact-only task does not wait


class TaskReimportTests(ActivityScenario):
    def test_a_completed_and_a_moved_task_update_in_place_and_undo_exactly(self):
        first = self.imported()
        snapshot = dump()
        past = iso(timezone.now() - dt.timedelta(days=1))
        records = dict(self.records)
        records["task"] = [
            task("t1", status="done", due_at=self.overdue_at, completed_at="2026-09-30T10:00:00Z",
                 targets=[("deal", "d1"), ("contact", 20)], owner_ref=ref("owner", 2))
            if t["source_id"] == "t1" else
            task("t2", status="in_progress", due_at=past, targets=[("deal", "d1")], kind="call")
            if t["source_id"] == "t2" else t
            for t in self.records["task"]]
        second = self.stage(records, previous_job=first)
        decisions = {str(r.raw["source_id"]): (r.proposed_mapping or {}).get("decision") for r in rows_of(second, "task")}
        self.assertEqual((decisions["t1"], decisions["t2"]), ("update", "update"), decisions)
        apply_job(second, ("task",))
        self.assertEqual(self.errors(second, "task"), {})
        t1, t2 = self.task_of("t1"), self.task_of("t2")
        self.assertEqual((t1.status, t1.completed_at), ("done", dt.datetime(2026, 9, 30, 10, tzinfo=UTC)))
        self.assertTrue(TaskEvent.objects.filter(task=t1, event="completed").exists())
        self.assertIsNotNone(t2.due_notified_at)  # moved into the past: pre-notified, no alarm
        self.assertEqual(emit_due(now=timezone.now()).sent, 0)
        self.assertEqual(writelog.undo_job(second, actor=ACTOR).refused, [])
        after = dump()
        self.assertEqual({k for k in after if after[k] != snapshot[k]}, set())


class TaskDueTests(ActivityScenario):
    """Done-when: overdue imported tasks emit no ``task_due``; a future one fires once, when due."""

    def test_overdue_tasks_are_pre_notified_and_crm_emit_due_sends_nothing(self):
        from core.plugins import signals

        sent = []
        signals.task_due.connect(lambda sender, **kw: sent.append(kw.get("task") or sender), weak=False,
                                 dispatch_uid="p5s6-due")
        self.addCleanup(signals.task_due.disconnect, dispatch_uid="p5s6-due")
        self.imported()
        before = timezone.now()
        for sid in ("t1", "t6", "t3"):  # t3: a waiting task due in 2024
            self.assertIsNotNone(self.task_of(sid).due_notified_at, sid)
        self.assertIsNone(self.task_of("t2").due_notified_at)  # due in three days
        self.assertIsNone(self.task_of("t4").due_notified_at)  # done
        out = io.StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("crm_emit_due", stdout=out)
        self.assertIn("task_due sent: 0", out.getvalue())
        self.assertEqual(sent, [])
        self.assertLess(self.task_of("t1").due_notified_at, timezone.now())
        self.assertLessEqual(self.task_of("t1").due_notified_at, before)
        with self.captureOnCommitCallbacks(execute=True):
            result = emit_due(now=timezone.now() + dt.timedelta(days=4))
        self.assertEqual(result.sent, 1)  # t2, once, when it fell due
        self.assertEqual(len(sent), 1)


# --- communications (2.10, hand-off 5) ------------------------------------------------


class CommunicationTests(ActivityScenario):
    def test_written_through_ingest_with_explicit_engagement_linkage(self):
        job = self.imported()
        self.assertEqual(self.errors(job, "communication"), {})
        n1, n2 = self.comm_of("n1"), self.comm_of("n2")
        self.assertEqual(n1.engagement, engagement("d1"))
        self.assertEqual(n1.party, self.person("Grace"))
        self.assertEqual(engagement("d10").party, self.person("Bob"))
        self.assertIsNone(n2.engagement)  # party-only: never the "one open engagement" guess
        self.assertEqual(n2.party, self.person("Bob"))
        for c in (n1, n2):
            self.assertFalse(c.is_editable)
            self.assertEqual(c.provider, PROVIDER)
        raw = CommunicationRaw.objects.get(communication=n1)
        self.assertEqual(raw.payload, {"id": "n1", "body": "Spoke about the retrofit."})
        # record()'s own ref (source=provider) and the job's (source=<provider>:<account>).
        self.assertTrue(ExternalRef.objects.filter(source=PROVIDER, object_type="message", source_id="n1").exists())
        self.assertTrue(ExternalRef.objects.filter(source=SOURCE, object_type="note", source_id="n1").exists())

    def test_participants_resolve_against_imported_contacts(self):
        self.imported()
        e1 = self.comm_of("e1", "email")
        parts = {p.role: p.party for p in e1.participants.all()}
        self.assertEqual(parts["to"], self.person("Grace"))
        c1 = self.comm_of("c1", "call")
        self.assertEqual((c1.direction, c1.status, c1.duration_seconds), ("inbound", "completed", 300))

    def test_last_activity_moves_forward_only(self):
        job = self.stage()
        apply_job(job, STEPS[:-3])
        eng = engagement("d1")
        before = eng.last_activity_at
        apply_job(job, ("task", "communication"))
        eng.refresh_from_db()
        self.assertEqual(eng.last_activity_at, max(before, dt.datetime(2024, 6, 2, 10, tzinfo=UTC)))

    def test_a_missing_direction_is_refused_not_guessed(self):
        records = dict(self.records)
        records["communication"] = [comm("x1", kind="email", direction=None, targets=[("contact", 20)],
                                         source_object_type="email")]
        job = self.stage(records)
        apply_job(job, STEPS)
        self.assertTrue(self.errors(job, "communication")["x1"].startswith("invalid_value: "))
        job2 = self.stage(records, mapping={**self.mapping, "communication": {"raw_schema_version": 1, "default_direction": "inbound"}})
        apply_job(job2, ("communication",))
        self.assertEqual(self.comm_of("x1", "email").direction, "inbound")


class ReviewRegressionTests(ActivityScenario):
    """Found in review: each would have imported or kept something it must not."""

    def test_an_association_only_change_is_applied_although_the_payload_is_the_same(self):
        records = dict(self.records)
        records["communication"] = [comm("z1", targets=[("contact", 20)])]
        first = self.stage(records)
        apply_job(first, STEPS)
        self.assertIsNone(self.comm_of("z1").engagement_id)
        snapshot = dump()
        records2 = dict(records)
        records2["communication"] = [comm("z1", targets=[("deal", "d1"), ("contact", 20)])]
        second = self.stage(records2, previous_job=first)
        self.assertEqual(rows_of(second, "communication")[0].proposed_mapping["decision"], "update")
        apply_job(second, ("communication",))
        self.assertEqual(self.comm_of("z1").engagement_id, engagement("d1").pk)
        self.assertEqual(writelog.undo_job(second, actor=ACTOR).refused, [])
        after = dump()
        self.assertEqual({k for k in after if after[k] != snapshot[k]}, set())

    def test_a_note_about_an_erased_contact_is_not_imported(self):
        from parties.erasure import erase_party

        job = self.stage()
        apply_job(job, STEPS)
        erase_party(self.person("Grace"), actor="test", reason="request")
        records = dict(self.records)
        records["communication"] = [comm("z2", targets=[("contact", 20)], body="Grace said her mortgage...")]
        records["task"] = [task("z3", title="Ask Grace about the mortgage", targets=[("contact", 20)])]
        second = self.stage(records, previous_job=job)
        apply_job(second, ("task", "communication"))
        self.assertFalse(ExternalRef.objects.filter(source=SOURCE, source_id__in=("z2", "z3")).exists())
        self.assertFalse(Communication.objects.filter(body_text__contains="mortgage").exists())
        self.assertFalse(Task.objects.filter(title__contains="mortgage").exists())
        for otype in ("task", "communication"):
            row = rows_of(second, otype)[0]
            self.assertEqual(row.raw, {"erased": True})

    def test_a_withdrawal_binds_every_party_holding_the_address(self):
        job = self.stage()
        apply_job(job, STEPS)
        grace, ada = self.person("Grace"), self.person("Ada")
        Party._base_manager.filter(pk=grace.pk).update(marketable=True)
        grace.refresh_from_db()
        for party in (ada, grace):
            PartyEmail.objects.create(party=party, company=self.co, email="shared@example.com", purpose="work")
        ConsentRecord.objects.create(company=self.co, party=grace, channel="email", purpose="marketing",
                                     state="granted", address_normalized="shared@example.com",
                                     occurred_at=timezone.now() - dt.timedelta(days=10), source="portal")
        self.assertTrue(can_contact(grace, "email", "marketing", "shared@example.com"))
        records = dict(self.records)
        records["consent"] = [fact("unsub:shared", "shared@example.com", state="withdrawn", occurred_at=iso(timezone.now()))]
        second = self.stage(records, previous_job=job)
        apply_job(second, ("consent",))
        withdrawn = ConsentRecord.objects.filter(address_normalized="shared@example.com", state="withdrawn")
        self.assertEqual(set(withdrawn.values_list("party_id", flat=True)), {grace.pk, ada.pk})
        self.assertEqual(can_contact(grace, "email", "marketing", "shared@example.com").reason, "withdrawn")
        self.assertEqual(writelog.undo_job(second, actor=ACTOR).refused, [])
        self.assertFalse(withdrawn.exists())

    def test_erasure_removes_a_party_less_imported_withdrawal_of_the_persons_address(self):
        from parties.erasure import erase_party

        self.imported()
        self.assertTrue(ConsentRecord.objects.filter(address_normalized="nobody@example.com", party=None).exists())
        stranger = Party.objects.create(company=self.co, kind="person", given_name="No", family_name="Body",
                                        display_name="No Body")
        PartyEmail.objects.create(party=stranger, company=self.co, email="nobody@example.com", purpose="work")
        erase_party(stranger, actor="test", reason="request")
        self.assertFalse(ConsentRecord.objects.filter(address_normalized="nobody@example.com").exists())

    def test_a_postal_fact_is_not_recorded(self):
        records = dict(self.records)
        records["consent"] = [fact("post:1", "1 Main St, Springfield", channel="post", state="withdrawn",
                                   occurred_at="2024-01-01T00:00:00Z")]
        job = self.stage(records)
        apply_job(job, STEPS)
        self.assertEqual(self.errors(job, "consent"), {})
        self.assertFalse(ConsentRecord.objects.filter(channel="post").exists())


class ReingestTests(ActivityScenario):
    """Done-when: re-ingest is a no-op, across two runs and across undo + re-apply."""

    def test_a_second_identical_import_writes_nothing(self):
        first = self.imported()
        snapshot = dump()
        logs = ImportWriteLog.objects.count()
        second = self.stage(previous_job=first)
        for otype in ("task", "communication"):
            states = {r.mapping_state for r in rows_of(second, otype)}
            self.assertEqual(states, {StagedRow.MappingState.NA}, otype)
        # A consent fact golden recorded nothing for (an unbacked grant) has no
        # ref to recognise it by: it is staged again, and applies to nothing again.
        again = {str(r.raw["source_id"]) for r in rows_of(second, "consent")
                 if r.mapping_state != StagedRow.MappingState.NA}
        self.assertEqual(again, {"subscription:8:ada", "subscription:9:bob"})
        apply_job(second, STEPS)
        self.assertEqual(dump(), snapshot)
        self.assertEqual(ImportWriteLog.objects.count(), logs)

    def test_record_is_a_no_op_when_the_payload_hash_matches(self):
        first = self.imported()
        n1 = self.comm_of("n1")
        parts = sorted(n1.participants.values_list("pk", flat=True))
        records = dict(self.records)
        records["communication"] = [comm("n1", targets=[("deal", "d1"), ("contact", 20)],
                                         occurred_at="2024-06-02T10:00:00Z", properties={"hs_note_flag": "x"})]
        second = self.stage(records, previous_job=first)
        self.assertEqual(rows_of(second, "communication")[0].proposed_mapping["decision"], "update")
        snapshot = dump()
        apply_job(second, ("communication",))
        n1.refresh_from_db()
        self.assertEqual(Communication.objects.filter(provider=PROVIDER).count(), 4)
        self.assertEqual(sorted(n1.participants.values_list("pk", flat=True)), parts)
        self.assertEqual(dump()["communications.CommunicationRaw"], snapshot["communications.CommunicationRaw"])

    def test_a_changed_payload_updates_in_place_and_undoes_exactly(self):
        first = self.imported()
        snapshot = dump()
        records = dict(self.records)
        records["communication"] = [
            c if c["source_id"] != "e1" else comm(
                "e1", kind="email", direction="outbound", subject="Quote v2", source_object_type="email",
                participants=[("from", "olive@example.com", "Olive"), ("to", "ada@example.com", "Ada")],
                targets=[("contact", 20)], payload={"id": "e1", "v": 2})
            for c in self.records["communication"]]
        second = self.stage(records, previous_job=first)
        apply_job(second, ("communication",))
        e1 = self.comm_of("e1", "email")
        self.assertEqual(e1.subject, "Quote v2")
        self.assertEqual(Communication.objects.filter(provider=PROVIDER).count(), 4)
        self.assertEqual({p.address_normalized for p in e1.participants.all()},
                         {"olive@example.com", "ada@example.com"})
        self.assertEqual(writelog.undo_job(second, actor=ACTOR).refused, [])
        after = dump()
        self.assertEqual({k for k in after if after[k] != snapshot[k]}, set())

    def test_undo_then_reapply_gives_the_same_activity(self):
        job = self.imported()

        def shape():
            return (sorted(Task.objects.filter(company=self.co).values_list("title", "status", "due_at"), key=str),
                    sorted(Communication.objects.filter(company=self.co).values_list("kind", "subject", "occurred_at"),
                           key=str),
                    sorted(ConsentRecord.objects.values_list("address_normalized", "state", "occurred_at"), key=str),
                    sorted(Suppression.objects.values_list("address_hash", "reason"), key=str))

        first = shape()
        self.assertEqual(writelog.undo_job(job, actor=ACTOR).refused, [])
        self.assertEqual(dump(), self.before)
        apply_job(job, STEPS)
        self.assertEqual(shape(), first)
        self.assertEqual(writelog.undo_job(job, actor=ACTOR).refused, [])
        self.assertEqual(dump(), self.before)


# --- consent (2.11) ---------------------------------------------------------------------


class ConsentTests(ActivityScenario):
    def test_opt_out_is_a_withdrawal_and_a_suppression_never_a_grant(self):
        self.imported()
        grace = self.person("Grace")
        records = ConsentRecord.objects.filter(address_normalized="grace@example.com")
        self.assertEqual(list(records.values_list("state", "party_id", "source")),
                         [("withdrawn", grace.pk, "import")])
        self.assertEqual(records.get().occurred_at, dt.datetime(2026, 10, 1, 9, tzinfo=UTC))  # the export time
        sup = Suppression.objects.get(address_hash=address_hash("grace@example.com"))
        self.assertEqual((sup.reason, sup.source, sup.lifted_at), ("opt_out", "import", None))
        self.assertEqual(can_contact(grace, "email", "marketing", "grace@example.com").reason, "suppressed:opt_out")
        self.assertFalse(ConsentRecord.objects.filter(state="granted", party=grace).exists())

    def test_hard_bounce_is_a_suppression_only(self):
        self.imported()
        self.assertEqual(Suppression.objects.get(address_hash=address_hash("bob@example.com")).reason, "hard_bounce")
        self.assertFalse(ConsentRecord.objects.filter(address_normalized="bob@example.com").exists())

    def test_a_grant_needs_time_basis_party_and_no_suppression(self):
        job = self.imported()
        ada = self.person("Ada")
        grants = ConsentRecord.objects.filter(state="granted")
        self.assertEqual(list(grants.values_list("party_id", "address_normalized")), [(ada.pk, "ada@example.com")])
        self.assertEqual(grants.get().evidence["evidence"], {"legalBasis": "CONSENT_WITH_NOTICE"})
        self.assertEqual(can_contact(ada, "email", "marketing", "ada@example.com").reason, "not_marketable")
        applied = {str(r.raw.get("source_id")): r.applied_ref for r in rows_of(job, "consent")}
        self.assertTrue(applied["subscription:9:bob"].startswith("crm_import.importjob:"))  # suppressed: nothing
        self.assertTrue(applied["subscription:8:ada"].startswith("crm_import.importjob:"))  # no basis: nothing

    def test_a_grant_is_blocked_by_a_suppression_later_in_the_file(self):
        records = dict(self.records)
        records["consent"] = [r for r in self.records["consent"] if r["source_id"] != "bounce:22"] + [
            fact("zz-bounce:22", "bob@example.com", suppress="hard_bounce")]
        job = self.stage(records)
        apply_job(job, STEPS)
        self.assertFalse(ConsentRecord.objects.filter(address_normalized="bob@example.com").exists())
        self.assertTrue(Suppression.objects.filter(address_hash=address_hash("bob@example.com")).exists())

    def test_an_unknown_address_is_still_suppressed(self):
        self.imported()
        self.assertTrue(Suppression.objects.filter(address_hash=address_hash("nobody@example.com")).exists())
        self.assertIsNone(ConsentRecord.objects.get(address_normalized="nobody@example.com").party_id)

    def test_an_existing_suppression_is_linked_never_duplicated_or_lifted(self):
        existing = Suppression.objects.create(company=self.co, channel="email",
                                              address_hash=address_hash("grace@example.com"), reason="complaint",
                                              source="erp")
        self.before = dump()
        self.imported()
        sup = Suppression.objects.get(address_hash=address_hash("grace@example.com"))
        self.assertEqual((sup.pk, sup.reason, sup.lifted_at), (existing.pk, "complaint", None))

    def test_an_erased_address_gets_no_consent_record_and_no_communication(self):
        from parties.erasure import erase_party

        job = self.stage()
        apply_job(job, ("owner", "pipeline", "company", "contact"))
        erase_party(self.person("Grace"), actor="test", reason="request")
        apply_job(job, STEPS[4:])
        self.assertFalse(ConsentRecord.objects.filter(address_normalized="grace@example.com").exists())
        self.assertFalse(ExternalRef.objects.filter(source=SOURCE, object_type="email", source_id="e1").exists())
        self.assertFalse(Communication.objects.filter(participants__address_normalized="grace@example.com").exists())
        # The erasure scrubbed the staged rows that name the address; none was applied.
        for otype in ("consent", "communication"):
            scrubbed = [r for r in rows_of(job, otype) if r.raw == {"erased": True}]
            self.assertTrue(scrubbed, otype)
            self.assertTrue(all(r.mapping_state != StagedRow.MappingState.APPLIED for r in scrubbed), otype)
        # The erasure's own suppression stands, unchanged by the import.
        self.assertEqual(Suppression.objects.get(address_hash=address_hash("grace@example.com")).reason, "erasure")

    def test_an_address_erased_after_staging_is_skipped_at_apply(self):
        """No erase_party (which also scrubs the staged rows): only the erasure
        suppression, as a hand-entered person erased mid-import leaves it."""
        from parties.erasure import ERASURE_DETAIL

        job = self.stage()
        Suppression.objects.create(company=self.co, channel="email", address_hash=address_hash("grace@example.com"),
                                   reason="erasure", source="erp", source_detail=ERASURE_DETAIL)
        apply_job(job, STEPS)
        errors = {**self.errors(job, "consent"), **self.errors(job, "communication")}
        self.assertTrue(errors, errors)
        for row in [*rows_of(job, "consent"), *rows_of(job, "communication")]:
            if row.raw == {"erased": True}:
                self.assertTrue(row.error.startswith("erased: "), row.error)
        self.assertFalse(ConsentRecord.objects.filter(address_normalized="grace@example.com").exists())
        self.assertFalse(ExternalRef.objects.filter(source=SOURCE, object_type="email", source_id="e1").exists())
        self.assertTrue(ExternalRef.objects.filter(source=SOURCE, object_type="note", source_id="n2").exists())

    def test_undo_refuses_a_consent_record_with_a_newer_one_for_the_address(self):
        job = self.imported()
        ConsentRecord.objects.create(company=self.co, party=self.person("Grace"), channel="email",
                                     purpose="marketing", state="granted", address_normalized="grace@example.com",
                                     occurred_at=timezone.now(), source="portal")
        report = writelog.undo_job(job, actor=ACTOR)
        self.assertTrue(any("newer consent record" in r["error"] for r in report.refused), report.refused)
        self.assertEqual(ConsentRecord.objects.filter(address_normalized="grace@example.com").count(), 2)


class ConsentReportTests(ActivityScenario):
    def test_the_consequence_and_the_counts_are_in_the_report(self):
        job = self.stage()
        report = job.report["consent"]
        self.assertEqual(report["consequence"], consent_applier.CONSEQUENCE)
        self.assertIn("cannot be sent marketing email", report["consequence"])
        self.assertEqual((report["withdrawals"], report["suppressions"], report["grants"],
                          report["grants_not_recorded"]), (2, 3, 2, 1))
        self.assertEqual(report["suppressions_by_reason"], {"opt_out": 2, "hard_bounce": 1})
        # Three contacts; only Ada (and Bob, on paper) has a grant with a basis.
        self.assertEqual((report["contacts"], report["contacts_no_consent_for_marketing"]), (3, 1))


# --- the whole step ---------------------------------------------------------------------


class StepTests(ActivityScenario):
    def test_apply_then_undo_job_restores_the_snapshot(self):
        job = self.imported()
        self.assertNotEqual(dump(), self.before)
        self.assertEqual(writelog.undo_job(job, actor=ACTOR).refused, [])
        self.assertEqual(dump(), self.before)

    def test_apply_then_undo_runs_newest_batch_first(self):
        from unify.apply import undo_run

        job = self.stage()
        runs = apply_job(job, STEPS)
        for otype in reversed(STEPS):
            out = undo_run(str(runs[otype].pk), runs[otype].undo_token)
            self.assertEqual(out["refused"], [], otype)
        self.assertEqual(dump(), self.before)

    def test_nothing_is_announced(self):
        from core.plugins import signals

        received = []
        names = ("task_created", "task_completed", "task_assigned", "communication_logged", "engagement_created",
                 "party_created", "party_updated")
        for name in names:
            sig = getattr(signals, name)
            sig.connect(lambda sender, **kw: received.append(sender), weak=False, dispatch_uid=f"p5s6-{name}")
            self.addCleanup(sig.disconnect, dispatch_uid=f"p5s6-{name}")
        from core.models import OutboxEvent

        outbox = OutboxEvent._base_manager.count()
        with self.captureOnCommitCallbacks(execute=True):
            job = self.imported()
            writelog.undo_job(job, actor=ACTOR)
        self.assertEqual(received, [])
        self.assertEqual(OutboxEvent._base_manager.count(), outbox)  # no durable consumer row either
        self.assertFalse(dispatch.is_suppressed())

    def test_tenant_isolation(self):
        from communications import ingest

        theirs = ingest.record(self.other, "note", direction="internal", occurred_at=timezone.now(),
                               provider=PROVIDER, external_id="n1", known_max_version=1,
                               envelope={"schema_version": 1, "provider": PROVIDER, "payload": {"x": 1}})
        snapshot = (Communication._base_manager.filter(company=self.other).count(), theirs.communication.subject)
        self.imported()
        self.assertEqual(Communication._base_manager.filter(company=self.other).count(), snapshot[0])
        self.assertNotEqual(self.comm_of("n1").pk, theirs.communication.pk)
        for model in (Task, TaskEvent, ConsentRecord, Suppression, CommunicationParticipant):
            self.assertFalse(model._base_manager.filter(company=self.other).exists(), model)
        self.assertFalse(ImportWriteLog.objects.exclude(company=self.co).exists())

    def test_engagement_bookkeeping_is_undone_before_the_task(self):
        job = self.imported()
        eng = Engagement._base_manager.get(pk=engagement("d1").pk)
        self.assertIsNotNone(eng.next_task_id)
        self.assertEqual(writelog.undo_job(job, actor=ACTOR).refused, [])
        self.assertFalse(Engagement._base_manager.filter(pk=eng.pk).exists())
