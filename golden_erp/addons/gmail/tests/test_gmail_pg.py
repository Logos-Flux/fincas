"""Concurrent sync of two mailboxes holding one email (Postgres only: SQLite serialises writers).

The sender's copy carries the Bcc, the recipient's does not. Ingested at the
same moment from two connections, the per-key advisory lock makes the
second wait for the first to commit, then add only what it lacks: one
communication, the Bcc kept, whichever copy wins the race.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from django.db import connection, connections, transaction
from django.test import TransactionTestCase, override_settings

from communications.models import Communication
from core import outbox
from core.models import ExternalRef

from ..adapter import GmailAdapter
from .helpers import PLUGINS, GmailSeed, item, message


@unittest.skipUnless(connection.vendor == "postgresql", "concurrent writers need Postgres")
@override_settings(GOLDEN_PLUGINS=PLUGINS)
class ConcurrentCopiesTests(GmailSeed, TransactionTestCase):
    def setUp(self):
        self.seed_tenant()
        self.grant(self.account, self.boss, "boss@example.org")

    def test_two_mailboxes_at_once_make_one_communication_with_the_bcc(self):
        real = GmailAdapter._existing

        def slow_existing(adapter, key):
            found = real(adapter, key)
            # Between "not there yet" and the write, the recipient waits longest: unserialised,
            # the sender's copy would commit first and the recipient's rewrite would drop the Bcc.
            time.sleep(0.6 if threading.current_thread().name == "recipient" else 0.1)
            return found

        with mock.patch.object(GmailAdapter, "_existing", slow_existing):
            self._race()

    def _race(self):
        for round_ in range(2):
            mid = f"<race-{round_}@mail.example.org>"
            sender = message(gid=f"s{round_}", mid=mid, frm="rep@example.org", to=["ada@example.com"],
                             bcc=["boss@example.org"])
            recipient = message(gid=f"r{round_}", mid=mid, frm="rep@example.org", to=["ada@example.com"])
            barrier = threading.Barrier(2)
            errors = []

            def run(msg, mailbox):
                try:
                    adapter = self.adapter()
                    barrier.wait(timeout=10)
                    with outbox.origin("gmail"), transaction.atomic():
                        adapter.ingest(item(msg, mailbox))
                except Exception as exc:  # noqa: BLE001 — reported below
                    errors.append(exc)
                finally:
                    connections.close_all()

            order = [("sender", sender, "rep@example.org"), ("recipient", recipient, "boss@example.org")]
            if round_ % 2:
                order.reverse()
            threads = [threading.Thread(target=run, args=(msg, box), name=name) for name, msg, box in order]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertEqual(errors, [])
            refs = ExternalRef.objects.filter(source="gmail", object_type="message")
            self.assertEqual(refs.count(), round_ + 1)
            comm = Communication.objects.get(pk=refs.order_by("-created_at").first().object_id)
            self.assertEqual(sorted(comm.participants.values_list("role", "address_normalized")),
                             [("bcc", "boss@example.org"), ("from", "rep@example.org"),
                              ("to", "ada@example.com")])
        self.assertEqual(Communication.objects.filter(provider="gmail").count(), 2)
