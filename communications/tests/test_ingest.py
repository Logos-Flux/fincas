"""``communications.ingest.record`` and participant resolution (spec 2.2, 2.6)."""

from __future__ import annotations

import datetime
from io import StringIO

from django.core.management import call_command
from django.test import override_settings

from communications.ingest import IngestError, record
from communications.models import Communication, CommunicationParticipant, CommunicationRaw
from communications.participants import Participant, resolve_unlinked
from core.models import ExternalRef
from core.plugins.base import BasePlugin
from core.plugins.payroll import AdapterError
from crm import engagements
from crm.tests.helpers import person
from unify.models import content_hash

from .helpers import NOW, CommTestCase, email, phone


class CallsAdapter(BasePlugin):
    name = "calls_adapter"

    def get_communication_kinds(self):
        return [{"name": "voicemail", "label": "Voicemail", "directions": ["inbound"],
                 "has_duration": True, "raw_schema_version": 2}]


def envelope(payload, version=1, provider="aircall"):
    return {"schema_version": version, "provider": provider, "payload": payload}


class ManualRecordTests(CommTestCase):
    def test_known_participant_is_linked_with_its_contact_point(self):
        result = record(
            self.company, "email", direction="outbound", occurred_at=NOW, subject="Hello",
            participants=[Participant("to", " ADA@example.com ", "Ada")], performed_by=self.rep,
        )
        comm = result.communication
        self.assertTrue(result.created)
        self.assertEqual(comm.provider, "manual")
        self.assertTrue(comm.is_editable)
        part = comm.participants.get()
        self.assertEqual((part.party, part.party_email), (self.ada, self.ada_email))
        self.assertEqual(part.address_normalized, "ada@example.com")
        self.assertEqual(comm.party, self.ada)

    def test_unknown_participant_stays_unlinked_and_creates_no_party(self):
        from parties.models import Party

        before = Party.objects.count()
        comm = record(self.company, "email", direction="inbound", occurred_at=NOW,
                      participants=[{"role": "from", "address": "stranger@elsewhere.test"}]).communication
        part = comm.participants.get()
        self.assertIsNone(part.party_id)
        self.assertEqual(part.address_normalized, "stranger@elsewhere.test")
        self.assertIsNone(comm.party_id)
        self.assertEqual(Party.objects.count(), before)

    def test_ambiguous_address_stays_unlinked(self):
        twin = person(self.company, "Ada", "Twin")
        email(twin, "ada@example.com")
        part = record(self.company, "email", direction="inbound", occurred_at=NOW,
                      participants=[Participant("from", "ada@example.com")]).communication.participants.get()
        self.assertIsNone(part.party_id)

    def test_another_tenants_contact_point_never_matches(self):
        stranger = person(self.other_company, "Bob", "Other")
        email(stranger, "bob@example.com")
        part = record(self.company, "email", direction="inbound", occurred_at=NOW,
                      participants=[Participant("from", "bob@example.com")]).communication.participants.get()
        self.assertIsNone(part.party_id)

    def test_phone_participants_resolve_by_e164(self):
        comm = record(self.company, "call", direction="inbound", occurred_at=NOW, duration_seconds=90,
                      participants=[Participant("caller", "(415) 555-0100"),
                                    Participant("callee", "", "Rep", party=self.rep_party())]).communication
        caller = comm.participants.get(role="caller")
        self.assertEqual((caller.party, caller.party_phone), (self.ada, self.ada_phone))
        self.assertEqual(caller.address_normalized, "+14155550100")
        self.assertEqual(comm.party, self.ada)  # the staff party is never the counterparty

    def rep_party(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user("staffer", password="x", is_staff=True)
        return person(self.company, "Sam", "Staff", user=user)

    def test_refusals(self):
        cases = [
            (dict(kind="fax", direction="outbound"), "Unknown communication kind"),
            (dict(kind="note", direction="outbound"), "not 'outbound'"),
            (dict(kind="email", direction="outbound", provider=""), "provider"),
            (dict(kind="email", direction="outbound", external_id="m1"), "envelope"),
            (dict(kind="email", direction="outbound", party=person(self.other_company)), "another company"),
            (dict(kind="email", direction="outbound", participants=[{"role": "boss"}]), "role"),
        ]
        for kwargs, fragment in cases:
            with self.subTest(fragment=fragment), self.assertRaisesRegex(ValueError, fragment):
                kind = kwargs.pop("kind")
                record(self.company, kind, occurred_at=NOW, **kwargs)
        self.assertEqual(Communication.objects.count(), 0)

    def test_duration_on_a_kind_without_one_is_refused(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            record(self.company, "email", direction="outbound", occurred_at=NOW, duration_seconds=5)

    def test_engagement_is_linked_only_when_exactly_one_is_open(self):
        eng = engagements.create(company=self.company, party=self.ada)
        first = record(self.company, "email", direction="inbound", occurred_at=NOW,
                       participants=[Participant("from", "ada@example.com")]).communication
        self.assertEqual(first.engagement, eng)
        engagements.create(company=self.company, party=self.ada)
        second = record(self.company, "email", direction="inbound", occurred_at=NOW,
                        participants=[Participant("from", "ada@example.com")]).communication
        self.assertIsNone(second.engagement_id)
        self.assertEqual(second.party, self.ada)

    def test_engagement_through_a_contact(self):
        eng = engagements.create(company=self.company, party=self.org)
        from crm.models import EngagementContact

        EngagementContact.objects.create(engagement=eng, party=self.ada, )
        comm = record(self.company, "email", direction="inbound", occurred_at=NOW,
                      participants=[Participant("from", "ada@example.com")]).communication
        self.assertEqual(comm.engagement, eng)

    def test_explicit_engagement_wins_and_must_share_the_tenant(self):
        eng = engagements.create(company=self.company, party=self.org)
        comm = record(self.company, "note", direction="internal", occurred_at=NOW,
                      engagement=eng, body_text="Called reception").communication
        self.assertEqual(comm.engagement, eng)
        other = engagements.create(company=self.other_company, party=person(self.other_company))
        with self.assertRaisesRegex(IngestError, "another company"):
            record(self.company, "note", direction="internal", occurred_at=NOW, engagement=other)


@override_settings(GOLDEN_PLUGINS=[CallsAdapter])
class ProviderImportTests(CommTestCase):
    def _import(self, payload, **kwargs):
        kwargs.setdefault("participants", [Participant("caller", "+14155550100")])
        return record(self.company, "voicemail", direction="inbound", occurred_at=NOW,
                      provider="aircall", external_id="vm-1", envelope=envelope(payload), **kwargs)

    def test_raw_sidecar_round_trip(self):
        payload = {"id": "vm-1", "transcript": "Call me back", "nested": {"n": [1, 2.5, None]}}
        comm = self._import(payload).communication
        raw = CommunicationRaw.objects.get(communication=comm)
        raw.refresh_from_db()
        self.assertEqual(raw.payload, payload)
        self.assertEqual(raw.content_hash, content_hash(payload))
        self.assertEqual((raw.provider, raw.schema_version, raw.company_id),
                         ("aircall", 1, self.company.pk))
        self.assertEqual(comm.raw, raw)
        self.assertFalse(comm.is_editable)
        self.assertNotIn("transcript", str(Communication.objects.filter(pk=comm.pk).values().get()))
        ref = ExternalRef.objects.get(source="aircall", object_type="message", source_id="vm-1")
        self.assertEqual(ref.object_id, comm.pk)

    def test_reingest_with_the_same_hash_is_a_noop(self):
        first = self._import({"id": "vm-1", "transcript": "a"})
        stamp = Communication.objects.get(pk=first.communication.pk).updated_at
        again = self._import({"transcript": "a", "id": "vm-1"}, subject="ignored")
        self.assertEqual(again.communication.pk, first.communication.pk)
        self.assertEqual((again.created, again.changed), (False, False))
        self.assertEqual(Communication.objects.count(), 1)
        self.assertEqual(CommunicationParticipant.objects.count(), 1)
        self.assertEqual(Communication.objects.get().updated_at, stamp)
        self.assertEqual(Communication.objects.get().subject, "")

    def test_reingest_with_a_new_payload_reprocesses(self):
        first = self._import({"id": "vm-1", "transcript": "a"})
        again = self._import({"id": "vm-1", "transcript": "b"}, subject="Updated",
                             participants=[Participant("caller", "+14155550100"),
                                           Participant("callee", "+14155550199")])
        self.assertEqual(again.communication.pk, first.communication.pk)
        self.assertEqual((again.created, again.changed), (False, True))
        comm = Communication.objects.get()
        self.assertEqual(comm.subject, "Updated")
        self.assertEqual(comm.raw.payload["transcript"], "b")
        self.assertEqual(comm.participants.count(), 2)
        self.assertEqual(ExternalRef.objects.filter(source="aircall").count(), 1)

    def test_a_concurrent_first_import_resolves_to_the_winner(self):
        from unittest.mock import patch

        from communications import ingest

        winner = self._import({"id": "vm-1"}).communication
        real = ingest._existing
        calls = []

        def racing(*args):
            calls.append(1)
            return None if len(calls) == 1 else real(*args)

        with patch.object(ingest, "_existing", side_effect=racing):
            again = self._import({"id": "vm-1"})
        self.assertEqual(again.communication.pk, winner.pk)
        self.assertFalse(again.created)
        self.assertEqual(Communication.objects.count(), 1)

    def test_reimport_under_another_object_type_is_refused(self):
        self._import({"id": "vm-1"})
        with self.assertRaisesRegex(IngestError, "cannot be re-imported"):
            record(self.company, "call", direction="inbound", occurred_at=NOW, provider="aircall",
                   external_id="vm-1", envelope=envelope({"id": "vm-1"}), known_max_version=1)
        self.assertEqual(Communication.objects.count(), 1)

    def test_reimport_keeps_a_party_set_since(self):
        comm = self._import({"id": "vm-1", "v": 1}).communication
        Communication.objects.filter(pk=comm.pk).update(party=self.org)
        self._import({"id": "vm-1", "v": 2})
        self.assertEqual(Communication.objects.get().party, self.org)

    def test_envelope_contract(self):
        cases = [
            ({"schema_version": 1, "payload": {}}, AdapterError, "envelope"),
            ({**envelope({}), "extra": 1}, AdapterError, "envelope"),
            (envelope({}, provider="zoom"), AdapterError, "does not match"),
            (envelope({}, version=3), AdapterError, "newer than the reader"),
            (envelope({}, version=0), AdapterError, "positive"),
            (envelope({}, version="1"), AdapterError, "newer than the reader"),
        ]
        for env, exc, fragment in cases:
            with self.subTest(fragment=fragment), self.assertRaisesRegex(exc, fragment):
                record(self.company, "voicemail", direction="inbound", occurred_at=NOW,
                       provider="aircall", external_id="vm-x", envelope=env)
        self.assertEqual(Communication.objects.count(), 0)

    def test_builtin_kind_import_needs_the_readers_max_version(self):
        with self.assertRaisesRegex(IngestError, "known_max_version"):
            record(self.company, "call", direction="inbound", occurred_at=NOW,
                   provider="aircall", external_id="c-1", envelope=envelope({}))
        comm = record(self.company, "call", direction="inbound", occurred_at=NOW, provider="aircall",
                      external_id="c-1", envelope=envelope({}), known_max_version=1).communication
        self.assertTrue(ExternalRef.objects.filter(object_type="call", source_id="c-1",
                                                   object_id=comm.pk).exists())

    def test_deleting_the_communication_drops_its_ref(self):
        comm = self._import({"id": "vm-1"}).communication
        comm.delete()
        self.assertFalse(ExternalRef.objects.filter(source="aircall").exists())
        self.assertFalse(CommunicationRaw.objects.exists())


class ResolveParticipantsTests(CommTestCase):
    def _unknown(self, address="newcomer@example.com"):
        return record(self.company, "email", direction="inbound", occurred_at=NOW,
                      participants=[Participant("from", address)]).communication.participants.get()

    def test_a_new_party_email_links_waiting_participants(self):
        part = self._unknown()
        newcomer = person(self.company, "New", "Comer")
        point = email(newcomer, "Newcomer@Example.com")
        part.refresh_from_db()
        self.assertEqual((part.party, part.party_email), (newcomer, point))

    def test_a_new_party_phone_links_waiting_participants(self):
        part = self._unknown("+44 20 7946 0000")
        caller = person(self.company, "Phone", "Person")
        point = phone(caller, "+442079460000")
        part.refresh_from_db()
        self.assertEqual((part.party, part.party_phone), (caller, point))

    def test_command_links_rows_written_around_the_hook(self):
        part = self._unknown()
        newcomer = person(self.company, "New", "Comer")
        from parties.models import PartyEmail

        # bulk_create skips save() and its signals: the hook never runs.
        PartyEmail.objects.bulk_create([PartyEmail(
            party=newcomer, company=self.company, email="newcomer@example.com",
            email_normalized="newcomer@example.com", purpose="work")])
        part.refresh_from_db()
        self.assertIsNone(part.party_id)
        out = StringIO()
        call_command("resolve_participants", "--dry-run", stdout=out)
        self.assertIn("would link 1", out.getvalue())
        part.refresh_from_db()
        self.assertIsNone(part.party_id)
        call_command("resolve_participants", stdout=out)
        part.refresh_from_db()
        self.assertEqual(part.party, newcomer)
        self.assertEqual(part.party_email, PartyEmail.objects.get(party=newcomer))
        self.assertEqual(resolve_unlinked(), 0)

    def test_match_n_stays_unlinked(self):
        for family in ("One", "Two"):
            email(person(self.company, "Shared", family), "shared@example.com")
        part = self._unknown("shared@example.com")
        self.assertIsNone(part.party_id)
        self.assertEqual(resolve_unlinked(), 0)
        part.refresh_from_db()
        self.assertIsNone(part.party_id)

    def test_merged_and_erased_parties_never_match(self):
        gone = person(self.company, "Gone", "Away", erased_at=NOW - datetime.timedelta(days=1))
        email(gone, "gone@example.com")
        self.assertIsNone(self._unknown("gone@example.com").party_id)


class AddParticipantsTests(CommTestCase):
    """``add_participants``: a later copy adds who took part and changes nothing else (Gmail, phase6 3.3)."""

    def test_adds_only_new_participants_and_leaves_the_rest(self):
        from communications.ingest import add_participants

        comm = record(self.company, "email", direction="inbound", occurred_at=NOW, subject="Hi",
                      body_text="body", participants=[Participant("from", "ada@example.com")]).communication
        added = add_participants(comm, [Participant("from", "ADA@example.com"), Participant("bcc", "x@example.net")])
        self.assertEqual(added, 1)
        comm.refresh_from_db()
        self.assertEqual((comm.subject, comm.body_text), ("Hi", "body"))
        self.assertEqual(sorted(comm.participants.values_list("role", flat=True)), ["bcc", "from"])
        self.assertEqual(add_participants(comm, [Participant("bcc", "x@example.net")]), 0)

    def test_a_scrubbed_communication_is_untouched(self):
        from communications.ingest import add_participants

        comm = record(self.company, "email", direction="inbound", occurred_at=NOW, provider="aircall",
                      external_id="e-1", envelope=envelope({"a": 1}), known_max_version=1,
                      participants=[Participant("from", "ada@example.com")]).communication
        CommunicationRaw.objects.filter(communication=comm).update(payload={"erased": True})
        self.assertEqual(add_participants(comm, [Participant("cc", "y@example.net")]), 0)
        self.assertEqual(comm.participants.count(), 1)
