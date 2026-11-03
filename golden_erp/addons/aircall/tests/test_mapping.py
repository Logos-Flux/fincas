"""Pure mapping of an Aircall call object (docs/crm/phase6-adapters.md 3.4)."""

from __future__ import annotations

from datetime import datetime, timezone

from django.test import SimpleTestCase

from core.plugins.payroll import AdapterError

from .. import mapping

T0 = 1772461200


def call(**kw):
    base = {"id": 1001, "direction": "inbound", "status": "done", "started_at": T0, "answered_at": T0 + 12,
            "ended_at": T0 + 400, "duration": 400, "raw_digits": "+1 202-555-0143",
            "number": {"id": 5101, "digits": "+1 202-555-0100"}, "user": {"id": 7201, "email": "sam@example.org"},
            "comments": [], "tags": []}
    base.update(kw)
    return base


class OutcomeTests(SimpleTestCase):
    def test_table(self):
        rows = [
            (call(), ("completed", "answered")),
            (call(answered_at=None, missed_call_reason="agents_did_not_answer"), ("failed", "missed")),
            (call(answered_at=None, missed_call_reason="short_abandoned"), ("failed", "abandoned")),
            (call(answered_at=None, missed_call_reason="abandoned_in_ivr"), ("failed", "abandoned")),
            (call(answered_at=None, missed_call_reason="out_of_opening_hours",
                  voicemail="https://media.example.com/v.mp3"), ("failed", "voicemail")),
            (call(answered_at=None), ("failed", "missed")),
            (call(direction="outbound", answered_at=None), ("failed", "not_answered")),
        ]
        for value, expected in rows:
            with self.subTest(expected=expected):
                self.assertEqual(mapping.outcome(value, value["direction"]), expected)


class FieldTests(SimpleTestCase):
    def test_inbound(self):
        f = mapping.call_fields(call(recording="https://media.example.com/r.mp3",
                                     tags=[{"name": "quote"}, {"name": "vip"}, {"name": "quote"}]))
        self.assertEqual(f["external_id"], "1001")
        self.assertEqual(f["occurred_at"], datetime(2026, 3, 2, 14, 20, tzinfo=timezone.utc))
        self.assertEqual(f["duration_seconds"], 400)
        self.assertEqual(f["participants"], [("caller", "+12025550143"), ("callee", "+12025550100")])
        self.assertEqual(f["metadata"], {"outcome": "answered", "tags": ["quote", "vip"], "has_recording": True,
                                         "has_voicemail": False, "user_id": 7201, "number_id": 5101})
        self.assertNotIn("media.example.com", repr(f))

    def test_outbound_roles(self):
        f = mapping.call_fields(call(direction="outbound"))
        self.assertEqual(f["participants"], [("callee", "+12025550143"), ("caller", "+12025550100")])

    def test_withheld_number_is_kept_raw_and_matches_nobody(self):
        f = mapping.call_fields(call(raw_digits="anonymous"))
        self.assertEqual(f["external_number"], "anonymous")

    def test_duration_from_times_when_missing(self):
        self.assertEqual(mapping.call_fields(call(duration=None))["duration_seconds"], 400)

    def test_summary_joins_comments_in_order_and_caps(self):
        f = mapping.call_fields(call(comments=[{"id": 2, "content": "second", "posted_at": T0 + 9},
                                               {"id": 1, "content": " first ", "posted_at": T0 + 5}]))
        self.assertEqual(f["summary"], "first\nsecond")
        long = mapping.call_fields(call(comments=[{"id": 1, "content": "x" * 9000, "posted_at": T0}]))
        self.assertEqual(len(long["summary"]), 4000)

    def test_malformed(self):
        for bad in (call(id=None), call(id="1;drop"), call(direction="sideways"), call(started_at="today"),
                    call(ended_at=T0 - 1)):
            with self.subTest(bad=bad), self.assertRaises(AdapterError):
                mapping.call_fields(bad)


class UnwrapAndDeliveryTests(SimpleTestCase):
    def test_unwrap(self):
        self.assertEqual(mapping.unwrap({"resource": "call", "event": "call.ended", "data": call()})[0], "call.ended")
        self.assertIsNone(mapping.unwrap({"resource": "call", "event": "call.created", "data": call()})[1])
        self.assertIsNone(mapping.unwrap({"resource": "contact", "event": "contact.created", "data": {}})[1])
        self.assertIsNone(mapping.unwrap(["not", "a", "dict"])[1])

    def test_delivery_id(self):
        self.assertEqual(mapping.delivery_id({"event": "call.ended", "data": call()}), "call.ended:1001")
        commented = call(comments=[{"id": 9}, {"id": 10}])
        self.assertEqual(mapping.delivery_id({"event": "call.commented", "data": commented}),
                         "call.commented:1001:10")
        self.assertEqual(mapping.delivery_id({"event": "call.ended", "data": {}}), "")
        self.assertEqual(mapping.delivery_id({"event": "call.commented", "data": call(comments=[{"x": 1}])}), "")
