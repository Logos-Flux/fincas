"""``ingest.record`` moves the linked engagement's ``last_activity_at``
(spec 1.3; hand-off section 5): forward only, one UPDATE, never inside
``suppressed()``; and ``crm.engagements.touch_activity`` on its own."""

from __future__ import annotations

import datetime

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from communications.ingest import record
from communications.participants import Participant
from core.plugins.dispatch import suppressed
from crm import engagements
from crm.models import Engagement

from .helpers import CommTestCase
from .test_ingest import envelope

DAY = datetime.timedelta(days=1)


def _stamp(eng, value):
    Engagement._base_manager.filter(pk=eng.pk).update(last_activity_at=value)


def _activity(eng):
    return Engagement._base_manager.values_list("last_activity_at", flat=True).get(pk=eng.pk)


class RecordTouchesActivityTests(CommTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.eng = engagements.create(company=self.company, party=self.ada)
        _stamp(self.eng, self.now - 30 * DAY)

    def _log(self, occurred_at, **kwargs):
        kwargs.setdefault("engagement", self.eng)
        return record(self.company, "email", direction="outbound", occurred_at=occurred_at,
                      participants=[Participant("to", "ada@example.com")], **kwargs).communication

    def test_bumps_forward_to_occurred_at(self):
        when = self.now - DAY
        self._log(when)
        self.assertEqual(_activity(self.eng), when)

    def test_a_null_last_activity_is_filled(self):
        _stamp(self.eng, None)
        when = self.now - 2 * DAY
        self._log(when)
        self.assertEqual(_activity(self.eng), when)

    def test_never_moves_backward(self):
        recent = self.now - DAY
        _stamp(self.eng, recent)
        self._log(self.now - 90 * DAY)
        self.assertEqual(_activity(self.eng), recent)

    def test_the_one_open_engagement_guess_bumps_too(self):
        comm = record(self.company, "email", direction="inbound", occurred_at=self.now - DAY,
                      participants=[Participant("from", "ada@example.com")]).communication
        self.assertEqual(comm.engagement, self.eng)
        self.assertEqual(_activity(self.eng), self.now - DAY)

    def test_no_bump_inside_suppressed(self):
        before = _activity(self.eng)
        with suppressed():
            self._log(self.now - DAY)
        self.assertEqual(_activity(self.eng), before)

    def test_no_bump_for_a_suppressed_provider_import(self):
        before = _activity(self.eng)
        with suppressed():
            record(self.company, "email", direction="inbound", occurred_at=self.now - DAY,
                   provider="hubspot", external_id="em-1", known_max_version=1,
                   envelope=envelope({"id": "em-1"}, provider="hubspot"), engagement=self.eng)
        self.assertEqual(_activity(self.eng), before)

    def test_no_bump_when_not_completed_or_in_the_future(self):
        before = _activity(self.eng)
        self._log(self.now - DAY, status="scheduled")
        self._log(self.now + DAY)
        self.assertEqual(_activity(self.eng), before)

    @override_settings(GOLDEN_COMMUNICATION_KINDS=[
        {"name": "newsletter", "label": "Newsletter", "directions": ["outbound"],
         "counts_as_activity": False}])
    def test_no_bump_for_a_kind_that_does_not_count(self):
        before = _activity(self.eng)
        record(self.company, "newsletter", direction="outbound", occurred_at=self.now - DAY,
               engagement=self.eng)
        self.assertEqual(_activity(self.eng), before)

    def test_party_only_communication_touches_nothing(self):
        before = _activity(self.eng)
        comm = self._log(self.now - DAY, engagement=None, link_engagement=False)
        self.assertIsNone(comm.engagement_id)
        self.assertEqual(_activity(self.eng), before)

    def test_constant_query_count(self):
        """The bump is one UPDATE whether it moves the value or not."""
        with CaptureQueriesContext(connection) as moved:
            self._log(self.now - DAY)
        with CaptureQueriesContext(connection) as stale:
            self._log(self.now - 60 * DAY)
        self.assertEqual(len(moved), len(stale))
        updates = [q["sql"] for q in moved.captured_queries
                   if q["sql"].startswith("UPDATE") and "last_activity_at" in q["sql"]]
        self.assertEqual(len(updates), 1)
        with suppressed(), CaptureQueriesContext(connection) as quiet:
            self._log(self.now - DAY)
        self.assertEqual(len(quiet), len(moved) - 1)


class TouchActivityTests(CommTestCase):
    def setUp(self):
        self.eng = engagements.create(company=self.company, party=self.ada)
        self.base = timezone.now() - 10 * DAY
        _stamp(self.eng, self.base)

    def test_forward_only(self):
        self.assertFalse(engagements.touch_activity(self.eng.pk, self.base - DAY))
        self.assertFalse(engagements.touch_activity(self.eng.pk, self.base))
        self.assertEqual(_activity(self.eng), self.base)
        self.assertTrue(engagements.touch_activity(self.eng.pk, self.base + DAY))
        self.assertEqual(_activity(self.eng), self.base + DAY)

    def test_none_and_a_null_value(self):
        self.assertFalse(engagements.touch_activity(self.eng.pk, None))
        _stamp(self.eng, None)
        self.assertTrue(engagements.touch_activity(self.eng.pk, self.base))
        self.assertEqual(_activity(self.eng), self.base)

    def test_one_query_and_no_save(self):
        updated_at = Engagement._base_manager.values_list("updated_at", flat=True).get(pk=self.eng.pk)
        with self.assertNumQueries(1):
            engagements.touch_activity(self.eng.pk, self.base + DAY)
        self.assertEqual(
            Engagement._base_manager.values_list("updated_at", flat=True).get(pk=self.eng.pk), updated_at)
