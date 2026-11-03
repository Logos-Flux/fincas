"""CRM Phase 3 step 7: the engagement / task / communication webhook events
(docs/crm/phase3-engagement.md 5.1, docs/webhooks.md section 1).

Payloads carry ids, registry keys and timestamps, never titles, subjects,
bodies or addresses; each event is registered at v1 so ``@1`` pins it.
"""

from __future__ import annotations

import datetime
import json
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from communications import ingest
from core.outbox.testing import drain
from crm import due, engagements, tasks
from crm.tests.helpers import CrmTestCase, person, profile
from webhooks import events
from webhooks.models import WebhookSubscription

CRM_EVENTS = (
    "engagement.created", "engagement.stage_changed", "engagement.won", "engagement.lost",
    "engagement.reopened", "task.created", "task.completed", "task.due", "communication.logged",
)
SECRET_TEXT = "Confidential pricing for Ada"


@override_settings(WEBHOOKS_DELIVER_ON_SIGNALS=True)
class CrmWebhookTests(CrmTestCase):
    def setUp(self):
        drain()  # the fixtures' own party.created rows, before any subscription exists
        self.delivered = []
        patcher = mock.patch("webhooks.services.deliver_webhook", side_effect=self._deliver)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sub = WebhookSubscription.objects.create(
            target_url="https://hooks.example.com/a", subscribed_events=["*"], company=self.company)
        self.other_sub = WebhookSubscription.objects.create(
            target_url="https://hooks.example.com/b", subscribed_events=["*"],
            company=self.other_company)

    def _deliver(self, sub, event_type, payload, **kwargs):
        self.delivered.append((sub.pk, event_type, payload))
        return mock.Mock()

    def events_for(self, sub):
        drain()  # signal deliveries run from the outbox
        return [(e, p) for s, e, p in self.delivered if s == sub.pk]

    def test_the_whole_lifecycle_delivers_each_event_once(self):
        reopener = profile(self.company, "boss", perms=["crm.reopen_engagement"])
        with self.captureOnCommitCallbacks(execute=True):
            eng = engagements.create(company=self.company, party=self.ada, owner=self.rep,
                                     title=SECRET_TEXT)
        with self.captureOnCommitCallbacks(execute=True):
            engagements.move(eng, self.stage("negotiation"), by=self.rep)
        with self.captureOnCommitCallbacks(execute=True):
            engagements.win(eng, by=self.rep)
        with self.captureOnCommitCallbacks(execute=True):
            engagements.reopen(eng, by=reopener)
        with self.captureOnCommitCallbacks(execute=True):
            engagements.lose(eng, "competitor", by=self.rep, competitor="Rival Inc",
                             notes=SECRET_TEXT)
        past = timezone.now() - datetime.timedelta(minutes=1)
        with self.captureOnCommitCallbacks(execute=True):
            task = tasks.create(engagement=eng, title=SECRET_TEXT, due=past, assignee=self.rep)
        with self.captureOnCommitCallbacks(execute=True):
            due.emit_due()
        with self.captureOnCommitCallbacks(execute=True):
            tasks.complete(task, by=self.rep)
        with self.captureOnCommitCallbacks(execute=True):
            ingest.record(self.company, "email", direction="outbound", occurred_at=timezone.now(),
                          subject=SECRET_TEXT, body_text=SECRET_TEXT, engagement=eng,
                          participants=[{"role": "to", "address": "ada@example.com"}])
        mine = self.events_for(self.sub)
        self.assertEqual(sorted(e for e, _ in mine), sorted(CRM_EVENTS))
        self.assertEqual(self.events_for(self.other_sub), [])  # another tenant hears nothing
        for event, payload in mine:
            with self.subTest(event=event):
                body = json.dumps(payload)
                self.assertNotIn(SECRET_TEXT, body)
                self.assertNotIn("ada@example.com", body)
                self.assertNotIn("Rival Inc", body)
                self.assertEqual(payload["schema_version"], 1)
                self.assertEqual(payload["company_id"], str(self.company.pk))
        by_event = dict(mine)
        self.assertEqual(by_event["engagement.stage_changed"]["to_stage_key"], "negotiation")
        self.assertEqual(by_event["engagement.lost"]["loss_reason"], "competitor")
        self.assertEqual(by_event["engagement.reopened"]["previous_status"], "won")
        self.assertEqual(by_event["task.due"]["task_id"], str(task.pk))
        self.assertEqual(
            {k: by_event["communication.logged"][k] for k in ("kind", "direction", "engagement_id")},
            {"kind": "email", "direction": "outbound", "engagement_id": str(eng.pk)})

    def test_task_assigned_and_reopened_have_no_webhook(self):
        task = tasks.create(company=self.company, title="x", assignee=self.rep)
        tasks.complete(task)
        drain()  # task.created / task.completed
        self.delivered.clear()
        with self.captureOnCommitCallbacks(execute=True):
            tasks.assign(task, profile(self.company, "other"))
            tasks.reopen(task)
        drain()
        self.assertEqual(self.delivered, [])

    def test_off_by_default(self):
        with override_settings(WEBHOOKS_DELIVER_ON_SIGNALS=False), \
                self.captureOnCommitCallbacks(execute=True):
            engagements.create(company=self.other_company, party=person(self.other_company))
        drain()
        self.assertEqual(self.delivered, [])


class CrmEventCatalogueTests(CrmTestCase):
    def test_every_event_is_registered_at_v1_and_pinnable(self):
        from webhooks.events import EVENTS, validate_entries

        for name in CRM_EVENTS:
            with self.subTest(event=name):
                self.assertEqual(EVENTS[name].supported(), (1,))
                self.assertEqual(validate_entries([f"{name}@1"]), [])
                self.assertTrue(validate_entries([f"{name}@2"]))

    def test_engagement_payload_keys_are_stable(self):
        eng = engagements.create(company=self.company, party=self.org, owner=self.rep)
        self.assertEqual(list(events.engagement_payload(eng)), [
            "schema_version", "engagement_id", "company_id", "number", "pipeline_key",
            "stage_key", "status", "party_id", "owner_id", "source", "origin", "expected_value",
            "currency", "expected_close_date", "created_at",
        ])
