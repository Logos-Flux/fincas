"""Erasure across the five installed adapters, in one tenant (docs/crm/phase6-adapters.md 4, step 8).

Ada is mirrored in every adapter the way each one mirrors a person: a ClickUp
task, a Klaviyo profile (and an event it reported), a Gmail message, an
Aircall call and a pending Google Ads upload. Erasing her must write exactly
one ``<provider>.erase`` row per adapter that wrote something to a provider
(ClickUp and Klaviyo), none for the others; purge the queued work about her;
flag the mirror refs while keeping their clear ids; make every adapter's
inbound replay for her write nothing; and, drained, send exactly the requests
in each adapter's own ``erase`` fixture.
"""

from __future__ import annotations

import copy
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from core import outbox
from core.integrations import accounts as services
from core.integrations import ratelimit
from core.integrations.transport import use_transport
from core.models import Company, ExternalRef, IntegrationAccount, OutboxEvent, UserProfile
from core.outbox.testing import drain
from core.provenance import is_erased_source
from core.testing.integration_fixtures import FixtureTransport, fixture_secret, load_json, snapshot
from crm.models import Task
from parties.erasure import erase_party
from parties.models import Party, PartyEmail, PartyPhone
from webhooks.models import WebhookSubscription

ADDONS = Path(settings.BASE_DIR) / "golden_erp" / "addons"
PLUGINS = [
    "golden_erp.addons.clickup.plugin.ClickUpPlugin",
    "golden_erp.addons.klaviyo.plugin.KlaviyoPlugin",
    "golden_erp.addons.gmail.plugin.GmailPlugin",
    "golden_erp.addons.aircall.plugin.AircallPlugin",
    "golden_erp.addons.google_ads.plugin.GoogleAdsPlugin",
]
FIXTURES = {
    "clickup": ADDONS / "clickup/fixtures",
    "klaviyo": ADDONS / "klaviyo/fixtures",
    "gmail": ADDONS / "gmail/fixtures/gmail",
    "aircall": ADDONS / "aircall/fixtures",
    "google_ads": ADDONS / "google_ads/fixtures",
}
CLICKUP_TASK = "86a0fx01"
KLAVIYO_PROFILE = "01FIXTUREPROFILEADA0000000"
CALL_ID = "1009"


def account_spec(provider, case):
    return load_json(FIXTURES[provider] / case / "account.json")


@override_settings(GOLDEN_PLUGINS=PLUGINS, WEBHOOKS_DELIVER_ON_SIGNALS=True)
class EraseAcrossAdaptersTests(TestCase):
    def setUp(self):
        ratelimit.reset()
        self.addCleanup(ratelimit.reset)
        self.company = Company.objects.create(name="Fixture tenant")
        self.ada = Party.objects.create(company=self.company, kind=Party.Kind.PERSON, given_name="Ada",
                                        family_name="Lovelace", origin=Party.Origin.ERP)
        PartyEmail.objects.create(party=self.ada, email="ada@example.com", purpose="work")
        PartyPhone.objects.create(party=self.ada, number_raw="+1 202-555-0143", purpose="work")
        self.sam = self.staff("sam", "sam@example.org")
        self.rep = self.staff("rep", "rep@example.org")
        self.eraser = self.staff("eraser", "eraser@example.org")
        WebhookSubscription.objects.create(target_url="https://hooks.example.com/in", company=self.company,
                                           subscribed_events=["*"])
        self.accounts = {}
        for provider, case in (("clickup", "erase"), ("klaviyo", "erase"), ("gmail", "erased-sender-skipped"),
                               ("aircall", "erased-call-skipped"), ("google_ads", "erased-party")):
            spec = account_spec(provider, case)
            account = IntegrationAccount.objects.create(company=self.company, provider=provider,
                                                        settings=spec.get("settings", {}), enabled=True)
            cls = services.adapter_class(provider)
            for kind in sorted(cls.credential_kinds - {"oauth_refresh", "oauth_access"}):
                services.set_credential(account, kind, fixture_secret(kind))
            self.accounts[provider] = account
        services.set_credential(self.accounts["gmail"], "oauth_refresh", fixture_secret("oauth_refresh"),
                                owner=self.rep, subject="rep@example.org")
        services.set_credential(self.accounts["gmail"], "oauth_access", fixture_secret("oauth_access"),
                                owner=self.rep, subject="rep@example.org",
                                expires_at=timezone.now() + timezone.timedelta(hours=1))
        self.mirror()

    def staff(self, key, email):
        user = get_user_model().objects.create_user(key, email=email, password="x", is_staff=True)
        return UserProfile.objects.create(user=user, company=self.company, display_name=key)

    def adapter(self, provider, transport=None):
        return services.adapter_for(IntegrationAccount.objects.get(pk=self.accounts[provider].pk),
                                    transport or FixtureTransport())

    # -- the mirrors, written through each adapter's own paths ------------------

    def envelopes(self):
        gmail_message = load_json(FIXTURES["gmail"] / "inbound-known-contact/responses/005.json")["payload"]
        klaviyo_event = load_json(FIXTURES["klaviyo"] / "erased-profile-skipped/inbound/001-event.json")
        klaviyo_event = copy.deepcopy(klaviyo_event)
        klaviyo_event["payload"]["profile"]["id"] = KLAVIYO_PROFILE
        klaviyo_event["payload"]["profile"]["attributes"]["email"] = "ada@example.com"
        return {
            "clickup": load_json(FIXTURES["clickup"] / "inbound-erased-skipped/inbound/001.json"),
            "klaviyo": klaviyo_event,
            "gmail": {"schema_version": 1, "provider": "gmail",
                      "payload": {"message": gmail_message, "mailbox": "rep@example.org"}},
            "aircall": load_json(FIXTURES["aircall"] / "erased-call-skipped/inbound/001.json"),
        }

    def ingest(self, provider, envelope):
        transport = FixtureTransport(FIXTURES["gmail"] / "inbound-known-contact") if provider == "gmail" else None
        adapter = self.adapter(provider, transport)
        with use_transport(adapter.transport), outbox.origin(provider), transaction.atomic():
            return adapter.ingest(envelope)

    def mirror(self):
        # ClickUp: a golden task pushed to list 901 (the push wrote this ref).
        self.task = Task.objects.create(company=self.company, party=self.ada, title="Call Ada back",
                                        assignee=self.sam, metadata={"clickup.list_id": "901"})
        self.ref(self.task, "clickup", "task", CLICKUP_TASK, {"date_updated": "1772460000000"})
        # Klaviyo: the profile golden upserted, then an event Klaviyo reported for it.
        self.ref(self.ada, "klaviyo", "profile", KLAVIYO_PROFILE)
        envelopes = self.envelopes()
        for provider in ("klaviyo", "gmail", "aircall"):
            outcomes = self.ingest(provider, envelopes[provider])
            self.assertTrue(any(o.changed for o in outcomes), f"{provider} mirror not written: {outcomes}")
        # Google Ads: an upload waiting for its worker (party_id: the deal's party).
        from golden_erp.addons.google_ads import values as ads

        now = timezone.now()
        OutboxEvent.objects.create(
            company_id=self.company.pk, consumer=ads.CONSUMER, event=ads.UPLOAD, idempotency_key="conv:fixture",
            party_id=self.ada.pk, payload={"change": "fixture"}, next_attempt_at=now, occurred_at=now)

    def ref(self, obj, source, object_type, source_id, metadata=None):
        return ExternalRef.objects.create(
            company=self.company, source=source, object_type=object_type, source_id=source_id,
            content_type=ContentType.objects.get_for_model(type(obj)), object_id=obj.pk, metadata=metadata or {})

    def erase(self):
        with self.captureOnCommitCallbacks(execute=True):
            return erase_party(self.ada, None, "Art. 17 request")

    # -- the tests ----------------------------------------------------------------

    def test_one_erase_row_per_adapter_that_wrote_to_its_provider(self):
        result = self.erase()
        rows = {r.consumer: r for r in OutboxEvent.objects.filter(consumer__endswith=".erase")}
        self.assertEqual(set(rows), {"clickup.erase", "klaviyo.erase"}, "Gmail, Aircall, Google Ads: no row")
        self.assertEqual(rows["clickup.erase"].payload["refs"], [{"object_type": "task", "source_id": CLICKUP_TASK}])
        self.assertEqual(rows["klaviyo.erase"].payload["refs"],
                         [{"object_type": "profile", "source_id": KLAVIYO_PROFILE}])
        for row in rows.values():
            self.assertEqual(row.idempotency_key, f"erase:{result.erasure_id}")
            self.assertEqual((row.company_id, row.party_id), (self.company.pk, self.ada.pk))
        self.assertEqual(result.counts["provider_erasures"], 2)
        for key in ("outbox_deleted", "outbox_scrubbed", "provider_erasures"):
            self.assertIn(key, result.counts)

    def test_no_queued_work_about_her_survives_but_the_erased_event_does(self):
        self.erase()
        left = OutboxEvent.objects.filter(party_id=self.ada.pk, state__in=["pending", "dead"]).exclude(
            consumer__endswith=".erase")
        self.assertEqual(list(left.values_list("consumer", flat=True)), [])
        self.assertTrue(OutboxEvent.objects.filter(consumer="webhooks", event="party.erased").exists())

    def test_mirror_refs_are_flagged_and_keep_their_clear_ids(self):
        self.erase()
        for source, object_type, source_id in (("clickup", "task", CLICKUP_TASK), ("aircall", "call", CALL_ID)):
            ref = ExternalRef._base_manager.get(company=self.company, source=source, source_id=source_id)
            self.assertTrue(ref.metadata.get("erased"), source)
            self.assertTrue(is_erased_source(self.company.pk, source, object_type, source_id), source)
        gmail = ExternalRef._base_manager.get(company=self.company, source="gmail")
        self.assertTrue(gmail.metadata.get("erased"))
        self.assertFalse(gmail.source_id.startswith("erased:"), "a Message-ID hash is not re-keyed")
        profile = ExternalRef._base_manager.get(company=self.company, source="klaviyo", object_type="profile")
        self.assertTrue(profile.source_id.startswith("erased:"), "the person's own ref is hashed")

    def test_replaying_every_adapter_inbound_for_her_writes_nothing(self):
        self.erase()
        before = snapshot(self.company)
        for provider, envelope in self.envelopes().items():
            outcomes = self.ingest(provider, envelope)
            self.assertFalse(any(o.changed or o.created for o in outcomes), f"{provider}: {outcomes}")
        self.assertEqual(snapshot(self.company), before)

    def test_a_clickup_task_naming_her_deal_is_not_mirrored_after_erasure(self):
        """A task golden never mirrored (no ref, no golden id) whose engagement field
        holds the erased person's deal number: creating it would hand her a task."""
        from crm import engagements
        from crm.models import Pipeline

        pipeline = Pipeline.objects.get(company=self.company, key="sales")
        deal = engagements.create(company=self.company, party=self.ada, pipeline=pipeline, title="Ada roof")
        self.erase()
        envelope = copy.deepcopy(self.envelopes()["clickup"])
        envelope["payload"]["id"] = "86a0fx99"
        envelope["payload"]["custom_fields"] = [{"id": "cf_engagement", "value": deal.number}]
        before = snapshot(self.company)
        outcomes = self.ingest("clickup", envelope)
        self.assertEqual([o.reason for o in outcomes], ["erased_party"])
        self.assertEqual(snapshot(self.company), before)
        self.assertFalse(Task._base_manager.filter(engagement=deal).exclude(pk=self.task.pk).exists())

    def test_draining_sends_exactly_each_adapters_erase_fixture(self):
        self.erase()
        self.task.refresh_from_db()
        self.assertNotIn("clickup.list_id", self.task.metadata,
                         "erasure dropped the list key: the engagement field comes from ClickUp's answer")
        transports = {p: FixtureTransport(FIXTURES[p] / "erase") for p in ("clickup", "klaviyo")}
        with use_transport(lambda account: transports[account.provider]):
            drain(["clickup.erase", "klaviyo.erase"])
        for provider, transport in transports.items():
            expected = load_json(FIXTURES[provider] / "erase/requests.expected.json")
            self.assertEqual(transport.comparable(), expected, provider)
            row = OutboxEvent.objects.get(consumer=f"{provider}.erase")
            self.assertEqual((row.state, row.payload), ("done", {"erased": True, "count": 1}))

    def test_clickup_task_rows_are_purged_by_object_not_party(self):
        """A task naming her under no party: its ``clickup.task`` rows carry the
        title and no ``party_id``. Erasure finds them by ``(crm.task, pk)``:
        the pending one goes, the done one keeps only the tombstone, and the
        task is renamed at ClickUp by the erase row."""
        naming = Task.objects.create(company=self.company, title="Roof quote",
                                     description="Follow up with ada@example.com", assignee=self.sam)
        self.ref(naming, "clickup", "task", "86a0fx02")
        now = timezone.now()
        common = dict(company_id=self.company.pk, consumer="clickup.task", object_type="crm.task",
                      object_id=naming.pk, next_attempt_at=now, occurred_at=now)
        queued = OutboxEvent.objects.create(event="task_updated", idempotency_key="t:1", state="pending",
                                            payload={"title": naming.title, "description": naming.description},
                                            **common)
        sent = OutboxEvent.objects.create(event="task_created", idempotency_key="t:0", state="done", completed_at=now,
                                          payload={"title": naming.title, "description": naming.description},
                                          **common)
        # Tenant B's row about a task with the same shape is not hers.
        other = Company.objects.create(name="Tenant B")
        theirs = OutboxEvent.objects.create(
            company_id=other.pk, consumer="clickup.task", object_type="crm.task", object_id=naming.pk,
            event="task_updated", idempotency_key="t:b", state="pending", payload={"title": "theirs"},
            next_attempt_at=now, occurred_at=now)
        self.erase()
        self.assertFalse(OutboxEvent.objects.filter(pk=queued.pk).exists())
        sent.refresh_from_db()
        self.assertEqual(sent.payload, {"erased": True})
        theirs.refresh_from_db()
        self.assertEqual(theirs.payload, {"title": "theirs"}, "another tenant's row is never touched")
        row = OutboxEvent.objects.get(consumer="clickup.erase")
        self.assertEqual(sorted(r["source_id"] for r in row.payload["refs"]), [CLICKUP_TASK, "86a0fx02"])

    def test_every_adapters_purge_hook_runs_and_a_failing_one_never_blocks(self):
        from unittest import mock

        from golden_erp.addons.google_ads.adapter import GoogleAdsAdapter
        from golden_erp.addons.klaviyo.adapter import KlaviyoAdapter

        with mock.patch.object(GoogleAdsAdapter, "purge_parties", return_value=0) as ads, \
                mock.patch.object(KlaviyoAdapter, "purge_parties", side_effect=RuntimeError("bug")), \
                self.assertLogs("golden_erp.integrations", "ERROR"):
            result = self.erase()
        ads.assert_called_once_with(self.company.pk, [self.ada.pk])
        self.assertIsNotNone(Party._base_manager.get(pk=self.ada.pk).erased_at)
        self.assertEqual(result.counts["provider_erasures"], 2)

    def test_google_ads_dead_upload_about_her_is_purged(self):
        from golden_erp.addons.google_ads import values as ads

        now = timezone.now()
        OutboxEvent.objects.create(
            company_id=self.company.pk, consumer=ads.CONSUMER, event=ads.UPLOAD, idempotency_key="conv:dead",
            party_id=self.ada.pk, state="dead", payload={"change": "dead"}, next_attempt_at=now, occurred_at=now,
            completed_at=now)
        self.erase()
        self.assertFalse(OutboxEvent.objects.filter(consumer=ads.CONSUMER, party_id=self.ada.pk,
                                                    state__in=["pending", "dead"]).exists())

    def test_the_sweep_across_five_adapters_keeps_the_login_lock_order(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from core.tests.helpers import LOGIN_LOCK_ORDER, login_lock_order

        with CaptureQueriesContext(connection) as queries:
            erase_party(self.ada, None, "Art. 17 request")
        order = login_lock_order(queries)
        positions = [LOGIN_LOCK_ORDER.index(table) for table in order]
        self.assertEqual(positions, sorted(set(positions)), f"locks out of the login order: {order}")
        self.assertFalse([q for q in queries if "FOR UPDATE" in q["sql"]
                          and ("core_externalref" in q["sql"] or "core_integrationaccount" in q["sql"])])

    # -- pushes in flight, creates never recorded, accounts (review of step 8) ------

    def queued_push(self, task, op="create", **extra):
        now = timezone.now()
        return OutboxEvent.objects.create(
            company_id=self.company.pk, consumer="clickup.task", event="task_created", object_type="crm.task",
            object_id=task.pk, idempotency_key=f"{task.pk}:{op}", state="running", attempts=1,
            payload={"task": str(task.pk), "op": op, "list_id": "901", **extra}, next_attempt_at=now,
            occurred_at=now, locked_by="w1", locked_until=now + timezone.timedelta(minutes=5))

    def test_a_clickup_create_that_lands_after_the_erasure_is_erased_too(self):
        """The worker read the task before the erasure; ClickUp answered after it
        committed. The ref is written tombstoned and a late erase row renames it."""
        from golden_erp.addons.clickup.adapter import _adopt

        fresh = Task.objects.create(company=self.company, party=self.ada, title="Visit Ada at 12 Main St")
        event = self.queued_push(fresh)
        stale = Task.objects.get(pk=fresh.pk)  # what the worker read
        self.erase()
        event.refresh_from_db()
        adapter = self.adapter("clickup")
        self.assertTrue(_adopt(adapter, stale, "901", {"id": "86a0fx55", "url": "https://x"}, event=event))
        self.assertTrue(is_erased_source(self.company.pk, "clickup", "task", "86a0fx55"))
        late = [r for r in OutboxEvent.objects.filter(consumer="clickup.erase")
                if {"object_type": "task", "source_id": "86a0fx55"} in (r.payload.get("refs") or [])]
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0].party_id, self.ada.pk)
        # The webhook ClickUp then sends for it writes nothing.
        envelope = copy.deepcopy(self.envelopes()["clickup"])
        envelope["payload"].update(id="86a0fx55", name="Visit Ada at 12 Main St")
        before = snapshot(self.company)
        outcomes = self.ingest("clickup", envelope)
        self.assertFalse(any(o.changed for o in outcomes))
        self.assertEqual(snapshot(self.company), before)

    def test_a_clickup_update_that_lands_after_the_erasure_is_renamed_again(self):
        from golden_erp.addons.clickup.adapter import _update

        event = self.queued_push(self.task, op="update", fields={"title": "Call Ada back"})
        ref = ExternalRef._base_manager.get(company=self.company, source="clickup", source_id=CLICKUP_TASK)
        stale = Task.objects.get(pk=self.task.pk)
        self.erase()
        event.refresh_from_db()
        transport = FixtureTransport(responses=[{
            "schema_version": 1, "provider": "clickup",
            "request": {"method": "PUT", "path": f"/api/v2/task/{CLICKUP_TASK}"},
            "response": {"status": 200, "headers": {}},
            "payload": {"id": CLICKUP_TASK, "date_updated": "1772463600001"}}])
        adapter = self.adapter("clickup", transport)
        cfg = adapter.lists()["901"]
        with use_transport(transport):
            outcome = _update(adapter, stale, ref, "901", cfg, {"title": "Call Ada back"}, event)
        self.assertIsInstance(outcome, outbox.Done)
        rows = OutboxEvent.objects.filter(consumer="clickup.erase")
        self.assertEqual(rows.count(), 2, "the erasure's row and a late one for the re-sent title")

    def test_a_queued_push_for_an_erased_task_sends_nothing(self):
        from golden_erp.addons.clickup.adapter import handle_push

        event = self.queued_push(self.task, op="update", fields={"title": "Call Ada back"})
        self.erase()
        event = OutboxEvent.objects.get(pk=event.pk)
        event.payload = {"task": str(self.task.pk), "op": "update", "list_id": "901",
                         "fields": {"title": "Call Ada back"}}  # a copy read before the tombstone
        transport = FixtureTransport(responses=[])
        with use_transport(transport):
            outcome = handle_push(self.adapter("clickup", transport), event)
        self.assertIsInstance(outcome, outbox.Cancel)
        self.assertEqual(transport.recorded, [])

    def test_a_clickup_create_golden_never_recorded_is_found_and_renamed(self):
        """A create hit a timeout (create_unknown): ClickUp may hold the task, golden
        has no ref. Erasure searches the list by the golden id field and renames it."""
        fresh = Task.objects.create(company=self.company, party=self.ada, title="Visit Ada at 12 Main St")
        event = self.queued_push(fresh, create_unknown=True)
        OutboxEvent.objects.filter(pk=event.pk).update(state="pending", locked_by="", locked_until=None)
        self.erase()
        self.assertFalse(OutboxEvent.objects.filter(pk=event.pk).exists(), "the pending create is purged")
        row = OutboxEvent.objects.get(consumer="clickup.erase")
        self.assertIn({"object_type": "golden_task", "source_id": f"901:{fresh.pk}"}, row.payload["refs"])
        found = {"id": "86a0fx66", "list": {"id": "901"}}
        transport = FixtureTransport(responses=[
            *load_json_list(FIXTURES["clickup"] / "erase/responses"),
            {"schema_version": 1, "provider": "clickup",
             "request": {"method": "GET", "path": "/api/v2/list/901/task"},
             "response": {"status": 200, "headers": {}}, "payload": {"tasks": [found]}},
            {"schema_version": 1, "provider": "clickup", "request": {"method": "PUT", "path": "/api/v2/task/86a0fx66"},
             "response": {"status": 200, "headers": {}}, "payload": found},
            {"schema_version": 1, "provider": "clickup",
             "request": {"method": "DELETE", "path": "/api/v2/task/86a0fx66/field/cf_engagement"},
             "response": {"status": 200, "headers": {}}, "payload": {}},
        ])
        with use_transport(lambda account: transport):
            drain(["clickup.erase"])
        sent = [(r["method"], r["path"]) for r in transport.comparable()]
        self.assertIn(("PUT", "/api/v2/task/86a0fx66"), sent)
        self.assertEqual(OutboxEvent.objects.get(consumer="clickup.erase").state, "done")

    def test_an_unrecorded_clickup_task_naming_an_erased_task_is_not_adopted(self):
        """The same lost create, seen from ClickUp's side: its webhook names the golden
        id of a task erasure scrubbed. Adopting it would write the erased text back."""
        fresh = Task.objects.create(company=self.company, party=self.ada, title="Visit Ada at 12 Main St")
        self.erase()
        envelope = copy.deepcopy(self.envelopes()["clickup"])
        envelope["payload"].update(id="86a0fx77", name="Visit Ada at 12 Main St",
                                   custom_fields=[{"id": "cf_golden_id", "value": str(fresh.pk)}])
        before = snapshot(self.company)
        outcomes = self.ingest("clickup", envelope)
        self.assertEqual([o.reason for o in outcomes], ["erased_task"])
        after = snapshot(self.company)
        before.pop("external_refs", None), after.pop("external_refs", None)  # the tombstone ref is new
        fresh.refresh_from_db()
        self.assertNotEqual(fresh.title, "Visit Ada at 12 Main St")
        late = [r for r in OutboxEvent.objects.filter(consumer="clickup.erase")
                if {"object_type": "task", "source_id": "86a0fx77"} in (r.payload.get("refs") or [])]
        self.assertEqual(len(late), 1, "the moment golden learns of it, it is erased at ClickUp")
        self.assertTrue(is_erased_source(self.company.pk, "clickup", "task", "86a0fx77"))
        # ClickUp's webhook for the rename (and any later copy) erases nothing more.
        for _ in range(2):
            self.assertEqual([o.reason for o in self.ingest("clickup", envelope)], ["erased_source"])
        self.assertEqual(len([r for r in OutboxEvent.objects.filter(consumer="clickup.erase")
                              if {"object_type": "task", "source_id": "86a0fx77"} in (r.payload.get("refs") or [])]),
                         1, "no erase loop")
        self.assertEqual({k: v for k, v in after.items() if k != "outbox"},
                         {k: v for k, v in before.items() if k != "outbox"})

    def test_a_clickup_webhook_racing_the_erasure_writes_nothing_back(self):
        """The webhook passed its tombstone check before the erasure committed; it
        must look again once it holds the task, never write ClickUp's text back."""
        from unittest import mock

        import golden_erp.addons.clickup.adapter as clickup

        self.erase()
        envelope = copy.deepcopy(self.envelopes()["clickup"])
        envelope["payload"].update(id=CLICKUP_TASK, name="Call Ada back", date_updated="1872460000000")
        real, calls = clickup.is_erased_source, []

        def stale_first(*args):
            calls.append(args)
            return False if len(calls) == 1 else real(*args)

        before = snapshot(self.company)
        with mock.patch.object(clickup, "is_erased_source", side_effect=stale_first):
            outcomes = self.ingest("clickup", envelope)
        self.assertEqual([o.reason for o in outcomes], ["erased_source"])
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(snapshot(self.company), before)

    def test_a_klaviyo_upsert_that_lands_after_the_erasure_is_deleted_too(self):
        from golden_erp.addons.klaviyo.adapter import _adopt_ref

        stale = Party.objects.get(pk=self.ada.pk)
        self.erase()
        self.assertIsNone(_adopt_ref(self.adapter("klaviyo"), stale, "01FIXTUREPROFILELATE000000", "d"))
        self.assertTrue(is_erased_source(self.company.pk, "klaviyo", "profile", "01FIXTUREPROFILELATE000000"))
        late = [r for r in OutboxEvent.objects.filter(consumer="klaviyo.erase")
                if {"object_type": "profile", "source_id": "01FIXTUREPROFILELATE000000"} in r.payload["refs"]]
        self.assertEqual(len(late), 1)

    def test_an_account_holding_records_is_disabled_never_deleted(self):
        from django.db.models import ProtectedError

        from core.admin import IntegrationAccountAdmin
        from core.integrations.erasure import allow_account_delete

        account = self.accounts["clickup"]
        with self.assertRaises(ProtectedError), transaction.atomic():
            IntegrationAccount.objects.filter(pk=account.pk).delete()
        admin = IntegrationAccountAdmin(IntegrationAccount, None)
        self.assertFalse(admin.has_delete_permission(None, account))
        IntegrationAccount.objects.filter(pk=account.pk).update(enabled=False)  # disabling is fine
        with allow_account_delete():
            IntegrationAccount.objects.filter(pk=account.pk).delete()
        self.assertFalse(IntegrationAccount.objects.filter(pk=account.pk).exists())

    def test_an_account_whose_records_were_all_erased_can_be_deleted(self):
        self.erase()
        transports = {p: FixtureTransport(FIXTURES[p] / "erase") for p in ("clickup", "klaviyo")}
        with use_transport(lambda account: transports[account.provider]):
            drain(["clickup.erase", "klaviyo.erase"])
        ExternalRef._base_manager.filter(company=self.company, source="google_ads").delete()
        IntegrationAccount.objects.filter(pk=self.accounts["google_ads"].pk).delete()
        self.assertFalse(IntegrationAccount.objects.filter(pk=self.accounts["google_ads"].pk).exists())


def load_json_list(directory):
    return [load_json(path) for path in sorted(Path(directory).glob("*.json"))]
