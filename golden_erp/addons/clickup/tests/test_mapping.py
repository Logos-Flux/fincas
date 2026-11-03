"""The 3.1 mapping table as pure functions (no database)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from .. import mapping

CFG = {"golden_id_field": "g", "status_map": {"To Do": "open", "in review": "waiting", "complete": "done"}}


class StatusTests(SimpleTestCase):
    def test_the_list_map_wins_case_insensitively(self):
        self.assertEqual(mapping.status_in({"status": "to do", "type": "open"}, CFG), "open")
        self.assertEqual(mapping.status_in({"status": "In Review", "type": "custom"}, CFG), "waiting")

    def test_unmapped_statuses_fall_back_by_type(self):
        for kind, golden in (("open", "open"), ("custom", "in_progress"), ("done", "done"), ("closed", "done")):
            self.assertEqual(mapping.status_in({"status": "zzz", "type": kind}, CFG), golden)
        self.assertIsNone(mapping.status_in({"status": "zzz", "type": "weird"}, CFG))

    def test_outbound_needs_one_answer(self):
        self.assertEqual(mapping.status_out("done", CFG), "complete")
        self.assertIsNone(mapping.status_out("cancelled", CFG))
        two = {"status_map": {"a": "done", "b": "done"}}
        self.assertIsNone(mapping.status_out("done", two))
        self.assertEqual(mapping.status_out("done", {**two, "push_status": {"done": "b"}}), "b")


class ValueTests(SimpleTestCase):
    def test_priority_both_ways(self):
        for cid, golden in ((1, "high"), (2, "high"), (3, "normal"), (4, "low")):
            self.assertEqual(mapping.priority_in({"id": str(cid)}), golden)
        self.assertEqual(mapping.priority_in(None), "normal")
        self.assertEqual([mapping.priority_out(p) for p in ("high", "normal", "low")], [2, 3, 4])

    def test_due_with_and_without_time(self):
        ny = ZoneInfo("America/New_York")
        instant = {"due_date": "1772773199999", "due_date_time": True}
        self.assertEqual(mapping.due_in(instant, ny), dt.datetime(2026, 3, 6, 4, 59, 59, 999000, tzinfo=dt.UTC))
        self.assertEqual(mapping.due_in({**instant, "due_date_time": False}, ny), dt.date(2026, 3, 5))
        self.assertIsNone(mapping.due_in({"due_date": None}, ny))

    def test_title_is_truncated_never_refused(self):
        self.assertEqual(mapping.title_in("x" * 300), ("x" * 255, True))
        self.assertEqual(mapping.title_in(" Call "), ("Call", False))

    def test_custom_field(self):
        task = {"custom_fields": [{"id": "g", "value": " 42 "}, {"id": "e", "value": None}, {"id": "o", "value": {}}]}
        self.assertEqual([mapping.custom_field(task, f) for f in ("g", "e", "o", "missing", "")], ["42", "", "", "", ""])


class BodyTests(SimpleTestCase):
    def test_golden_values_pins_only_the_written_columns(self):
        task = SimpleNamespace(title="T", description="", status="open", priority="low",
                               due_at=dt.datetime(2026, 3, 9, 16, 30, tzinfo=dt.UTC), all_day=False, assignee_id=None)
        self.assertEqual(mapping.golden_values(task, ["title", "updated_at"]), {"title": "T"})
        self.assertEqual(mapping.golden_values(task, ["due_at"]), {"all_day": False, "due_at": "2026-03-09T16:30:00Z"})
        self.assertEqual(sorted(mapping.golden_values(task)),
                         ["all_day", "assignee", "description", "due_at", "priority", "status", "title"])

    def test_update_body_carries_only_what_was_pinned(self):
        self.assertEqual(mapping.update_body({"title": "New"}, status_name=None, add_assignee=None,
                                             remove_assignees=[]), {"name": "New"})
        self.assertEqual(mapping.update_body({"assignee": "p"}, status_name=None, add_assignee="7",
                                             remove_assignees=["5", "7"]), {"assignees": {"add": [7], "rem": [5]}})
        self.assertEqual(mapping.update_body({"due_at": None, "all_day": True}, status_name=None, add_assignee=None,
                                             remove_assignees=[]), {"due_date": None, "due_date_time": False})

    def test_create_body_sets_the_golden_id_field(self):
        body = mapping.create_body({"title": "T", "description": "", "priority": "normal", "due_at": None,
                                    "all_day": True}, status_name="to do", golden_id="abc", golden_id_field="g",
                                   assignee_id=None)
        self.assertEqual(body["custom_fields"], [{"id": "g", "value": "abc"}])
        self.assertEqual(body["status"], "to do")

    def test_erase_body_renames_and_clears(self):
        self.assertEqual(mapping.erase_body(), {"name": "Erased", "description": ""})
