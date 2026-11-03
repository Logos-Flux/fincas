"""Party merges retarget communications and participants (phase3-engagement.md 1.3, 2.2)."""

from __future__ import annotations

from communications.ingest import record
from communications.models import Communication, CommunicationParticipant
from communications.participants import Participant
from crm.tests.helpers import person
from parties.merge import merge, unmerge
from parties.models import PartyEmail

from .helpers import NOW, CommTestCase, email


class MergeTests(CommTestCase):
    def test_merge_moves_party_links_and_twin_addresses_and_unmerge_restores(self):
        loser = person(self.company, "Ada", "L.")
        twin = email(loser, "ada@example.com")  # duplicate of the survivor's address
        own = email(loser, "ada.l@example.com")
        # Participants built before the twin existed resolve to the survivor; link the loser's by hand.
        comm = record(self.company, "email", direction="inbound", occurred_at=NOW, party=loser,
                      participants=[Participant("from", "ada.l@example.com"),
                                    Participant("cc", "ada@example.com", party=loser)]).communication
        from_row = comm.participants.get(role="from")
        cc_row = comm.participants.get(role="cc")
        self.assertEqual((from_row.party, from_row.party_email), (loser, own))
        self.assertEqual((cc_row.party, cc_row.party_email), (loser, twin))

        (log,) = merge(self.ada, [loser], actor="tester")
        comm.refresh_from_db()
        from_row.refresh_from_db()
        cc_row.refresh_from_db()
        self.assertEqual(comm.party, self.ada)
        self.assertEqual((from_row.party, from_row.party_email_id), (self.ada, own.pk))
        # The loser's duplicate address was deleted; the row names the survivor's copy.
        self.assertFalse(PartyEmail.objects.filter(pk=twin.pk).exists())
        self.assertEqual((cc_row.party, cc_row.party_email), (self.ada, self.ada_email))
        self.assertIn("communications.CommunicationParticipant", log.moved)
        self.assertIn("communications.Communication", log.moved)

        unmerge(log)
        comm.refresh_from_db()
        cc_row.refresh_from_db()
        from_row.refresh_from_db()
        self.assertEqual(comm.party, loser)
        self.assertEqual((cc_row.party_id, cc_row.party_email_id), (loser.pk, twin.pk))
        self.assertEqual((from_row.party_id, from_row.party_email_id), (loser.pk, own.pk))
        self.assertEqual(Communication.objects.count(), 1)
        self.assertEqual(CommunicationParticipant.objects.count(), 2)
