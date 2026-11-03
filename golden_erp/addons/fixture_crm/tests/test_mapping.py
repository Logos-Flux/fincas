"""Pure mapping functions of the fixture adapter (no database)."""

from __future__ import annotations

from django.test import SimpleTestCase

from core.plugins.payroll import AdapterError

from ..mapping import message_fields, parse_instant


class MappingTests(SimpleTestCase):
    def test_a_message_maps_to_record_arguments(self):
        fields = message_fields({"id": "fx_msg_1", "type": "message.sent", "occurred_at": "2026-03-02T14:30:00Z",
                                 "from": "sales@example.org", "to": ["ada@example.com", ""], "subject": "Hi",
                                 "thread": "t1"})
        self.assertEqual(fields["direction"], "outbound")
        self.assertEqual(fields["participants"], [("from", "sales@example.org"), ("to", "ada@example.com")])
        self.assertEqual(fields["thread_key"], "fixture:t1")

    def test_unknown_types_and_missing_ids_are_not_messages(self):
        self.assertIsNone(message_fields({"id": "x", "type": "message.deleted"}))
        self.assertIsNone(message_fields({"type": "message.received"}))
        self.assertIsNone(message_fields("nope"))

    def test_naive_or_malformed_instants_are_refused(self):
        for bad in ("2026-03-02T14:30:00", "yesterday", None):
            with self.assertRaises(AdapterError):
                parse_instant(bad)
