"""ClickUp adapter behaviour the fixture snapshot cannot show (phase6-adapters.md 3.1, step 2).

The contract test (``test_contract.py``) replays every fixture case and
compares requests and a canonical snapshot. These tests cover what that
snapshot leaves out: the links golden owns (engagement, party), the mirror's
metadata and flags, the echo rule against the webhooks consumer, tenant
isolation, settings validation, the erasure collector and the rule that no
credential reaches any output.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.test import TestCase, override_settings

from core import outbox
from core.integrations.accounts import set_credential
from core.integrations.transport import use_transport
from core.models import Company, ExternalRef, IntegrationAccount, OutboxEvent, UserProfile
from core.outbox.testing import drain
from core.plugins.crm_adapter import envelope
from core.plugins.dispatch import suppressed
from core.testing.integration_fixtures import FixtureTransport, canonical_json, fixture_secret, run_case
from crm import engagements, tasks
from crm.models import Engagement, Task
from parties.models import Party

from ..adapter import KEY_ASSIGNEES, KEY_DELETED, KEY_FLAGS, KEY_LIST, TASK_CONSUMER, ClickUpAdapter, build_push
from .seed import seed_hook

PLUGINS = ["golden_erp.addons.clickup.plugin.ClickUpPlugin"]
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
LIST = "901"
SETTINGS = {"lists": {LIST: {
    "golden_id_field": "cf_golden_id", "engagement_field": "cf_engagement",
    "status_map": {"to do": "open", "in progress": "in_progress", "complete": "done"}}}}
API_KEY = fixture_secret("api_key")
WEBHOOK_SECRET = fixture_secret("webhook_secret")


def cu_task(cu_id, name="Measure the roof", *, status=("to do", "open"), updated="1772463600000",
            engagement="", golden_id="", assignees=(), list_id=LIST):
    fields = []
    if golden_id:
        fields.append({"id": "cf_golden_id", "value": golden_id})
    if engagement:
        fields.append({"id": "cf_engagement", "value": engagement})
    return {"id": cu_id, "name": name, "description": "", "status": {"status": status[0], "type": status[1]},
            "date_created": "1772460000000", "date_updated": updated, "date_done": None, "date_closed": None,
            "assignees": [{"id": int(i), "email": e} for i, e in assignees], "priority": None,
            "due_date": None, "due_date_time": False, "list": {"id": list_id},
            "url": f"https://app.clickup.com/t/{cu_id}", "custom_fields": fields}


def response(method, path, payload, status=200):
    out = {"schema_version": 1, "provider": "clickup", "request": {"method": method, "path": path},
           "payload": payload}
    if status != 200:
        out["response"] = {"status": status}
    return out


def tenant(name, number):
    company = Company.objects.create(name=name)
    user = get_user_model().objects.create_user(f"sam-{name}", email=f"sam.{name.lower()}@example.com",
                                                password="x", is_staff=True)
    sam = UserProfile.objects.create(user=user, company=company, display_name="sam")
    ada = Party.objects.create(company=company, kind=Party.Kind.PERSON, given_name="Ada", family_name="Lovelace",
                               origin=Party.Origin.ERP)
    deal = engagements.create(company=company, party=ada)
    Engagement._base_manager.filter(pk=deal.pk).update(number=number)
    deal.refresh_from_db()
    account = IntegrationAccount.objects.create(company=company, provider="clickup", settings=SETTINGS, enabled=True)
    set_credential(account, "api_key", API_KEY)
    set_credential(account, "webhook_secret", WEBHOOK_SECRET)
    return SimpleNamespace(company=company, sam=sam, ada=ada, deal=deal, account=account)


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class ClickUpTestCase(TestCase):
    def setUp(self):
        self.a = tenant("Alpha", "EN-00042")
        self.b = tenant("Beta", "EN-00043")  # numbers are unique across tenants

    def adapter(self, t=None, responses=()):
        transport = FixtureTransport(responses=list(responses))
        return ClickUpAdapter((t or self.a).account, transport), transport

    def ingest(self, adapter, payload):
        with outbox.origin("clickup"), transaction.atomic():
            return adapter.ingest(envelope("clickup", payload))

    def linked(self, t=None, cu_id="86a0fx01", **kwargs):
        t = t or self.a
        with suppressed():
            task = tasks.create(engagement=t.deal, title="Call Ada back", assignee=t.sam,
                                metadata={KEY_LIST: LIST}, **kwargs)
        ExternalRef.objects.create(company=t.company, source="clickup", object_type="task", source_id=cu_id,
                                   content_type=ContentType.objects.get_for_model(Task), object_id=task.pk,
                                   metadata={"date_updated": "1772460000000"})
        return task


class LinksGoldenOwnsTests(ClickUpTestCase):
    def test_an_inbound_task_with_an_engagement_number_links_engagement_and_party(self):
        adapter, _ = self.adapter()
        (outcome,) = self.ingest(adapter, cu_task("86a0fx02", engagement="EN-00042"))
        self.assertTrue(outcome.created)
        task = Task.objects.get(company=self.a.company, source="clickup")
        self.assertEqual((task.engagement_id, task.party_id), (self.a.deal.pk, self.a.ada.pk))
        self.assertEqual(task.kind, "todo")
        self.assertEqual(task.metadata[KEY_LIST], LIST)

    def test_an_unknown_engagement_number_leaves_the_link_empty_and_flags_it(self):
        adapter, _ = self.adapter()
        self.ingest(adapter, cu_task("86a0fx02", engagement="EN-99999"))
        task = Task.objects.get(company=self.a.company, source="clickup")
        self.assertIsNone(task.engagement_id)
        self.assertEqual(task.metadata[KEY_FLAGS], ["engagement_unresolved"])

    def test_a_clickup_change_to_the_engagement_is_refused_and_reported(self):
        other = engagements.create(company=self.a.company, party=self.a.ada)
        Engagement._base_manager.filter(pk=other.pk).update(number="EN-00099")
        task = self.linked()
        adapter, _ = self.adapter()
        with self.assertLogs("golden_erp.integrations.clickup", level="WARNING") as logs:
            (outcome,) = self.ingest(adapter, cu_task("86a0fx01", name="Renamed", engagement="EN-00099"))
        task.refresh_from_db()
        self.assertEqual(task.engagement_id, self.a.deal.pk)  # golden keeps its link
        self.assertEqual(task.title, "Renamed")  # ClickUp's own field still applies
        self.assertEqual(outcome.reason, "refused:engagement")
        self.assertIn("refused_engagement", task.metadata[KEY_FLAGS])
        self.assertIn("refused", logs.output[0])

    def test_a_golden_id_field_naming_another_task_is_refused(self):
        task = self.linked()
        stranger = tasks.create(company=self.a.company, title="Someone else's")
        adapter, _ = self.adapter()
        (outcome,) = self.ingest(adapter, cu_task("86a0fx01", golden_id=str(stranger.pk)))
        self.assertEqual(outcome.reason, "refused:golden_id")
        self.assertEqual(ExternalRef.objects.get(source="clickup", source_id="86a0fx01").object_id, task.pk)

    def test_a_clickup_copy_of_a_mirrored_task_is_refused(self):
        task = self.linked()
        adapter, _ = self.adapter()
        (outcome,) = self.ingest(adapter, cu_task("86a0fx09", golden_id=str(task.pk)))
        self.assertEqual(outcome.reason, "refused:golden_id_taken")
        self.assertFalse(ExternalRef.objects.filter(source_id="86a0fx09").exists())

    def test_a_golden_id_adopts_an_unlinked_golden_task(self):
        with suppressed():
            task = tasks.create(engagement=self.a.deal, title="Pushed long ago")
        adapter, _ = self.adapter()
        (outcome,) = self.ingest(adapter, cu_task("86a0fx07", name="Pushed long ago", golden_id=str(task.pk)))
        self.assertTrue(outcome.changed)
        self.assertEqual(ExternalRef.objects.get(source_id="86a0fx07").object_id, task.pk)


class MirrorTests(ClickUpTestCase):
    def test_assignees_are_kept_as_ids_and_an_unmatched_one_is_flagged(self):
        task = self.linked()
        adapter, _ = self.adapter()
        self.ingest(adapter, cu_task("86a0fx01", name="Call Ada back", assignees=[("81000009", "nobody@example.net")]))
        task.refresh_from_db()
        self.assertIsNone(task.assignee_id)
        self.assertEqual(task.metadata[KEY_ASSIGNEES], ["81000009"])
        self.assertEqual(task.metadata[KEY_FLAGS], ["assignee_unmatched"])

    def test_staff_of_another_tenant_never_match(self):
        task = self.linked()
        adapter, _ = self.adapter()
        self.ingest(adapter, cu_task("86a0fx01", assignees=[("81000002", self.b.sam.user.email)]))
        task.refresh_from_db()
        self.assertIsNone(task.assignee_id)

    def test_a_long_name_is_truncated_and_flagged(self):
        task = self.linked()
        adapter, _ = self.adapter()
        self.ingest(adapter, cu_task("86a0fx01", name="x" * 300))
        task.refresh_from_db()
        self.assertEqual(len(task.title), 255)
        self.assertIn("title_truncated", task.metadata[KEY_FLAGS])

    def test_a_deletion_cancels_and_stops_syncing(self):
        task = self.linked()
        adapter, _ = self.adapter()
        self.ingest(adapter, {"event": "taskDeleted", "task_id": "86a0fx01", "history_items": []})
        task.refresh_from_db()
        self.assertEqual(task.status, "cancelled")
        self.assertTrue(task.metadata[KEY_DELETED])
        # Stopped: a later copy is ignored, and a golden edit pushes nothing.
        (outcome,) = self.ingest(adapter, cu_task("86a0fx01", name="Back from the dead", updated="1872463600000"))
        self.assertEqual(outcome.reason, "deleted")
        tasks.reopen(task)
        self.assertFalse(OutboxEvent.objects.filter(consumer=TASK_CONSUMER).exists())

    def test_the_mirror_is_dated_with_clickups_times(self):
        task = self.linked()
        adapter, _ = self.adapter()
        done = {**cu_task("86a0fx01", status=("complete", "closed")), "date_done": "1772461800000"}
        self.ingest(adapter, done)
        task.refresh_from_db()
        self.assertEqual(task.completed_at, dt.datetime(2026, 3, 2, 14, 30, tzinfo=dt.UTC))
        event = task.events.get(event="completed")
        self.assertEqual(event.at, task.completed_at)


@override_settings(WEBHOOKS_DELIVER_ON_SIGNALS=True)
class EchoTests(ClickUpTestCase):
    def test_an_inbound_completion_reaches_webhooks_but_enqueues_no_clickup_row(self):
        self.linked()
        OutboxEvent.objects.all().delete()
        adapter, _ = self.adapter()
        self.ingest(adapter, cu_task("86a0fx01", status=("complete", "closed")))
        self.assertTrue(OutboxEvent.objects.filter(consumer="webhooks", event="task.completed").exists())
        self.assertFalse(OutboxEvent.objects.filter(consumer=TASK_CONSUMER).exists())

    def test_the_same_completion_made_in_golden_is_pushed(self):
        task = self.linked()
        OutboxEvent.objects.all().delete()
        tasks.complete(task)
        row = OutboxEvent.objects.get(consumer=TASK_CONSUMER)
        self.assertEqual(row.payload["fields"], {"status": "done"})
        self.assertEqual(row.party_id, self.a.ada.pk)  # erasure reaches it (4.2)
        self.assertTrue(OutboxEvent.objects.filter(consumer="webhooks", event="task.completed").exists())


class BuildTests(ClickUpTestCase):
    def test_two_saves_are_two_rows_each_pinning_its_own_columns(self):
        task = self.linked()
        tasks.edit(task, title="One")
        tasks.reschedule(task, dt.date(2026, 4, 1))
        rows = OutboxEvent.objects.filter(consumer=TASK_CONSUMER).order_by("created_at")
        self.assertEqual(sorted(sorted(r.payload["fields"]) for r in rows), [["all_day", "due_at"], ["title"]])

    def test_a_save_writing_no_clickup_column_is_no_row(self):
        task = self.linked()
        task.metadata = {**task.metadata, "clickup.flags": ["x"]}
        task.save(update_fields={"metadata", "updated_at"})
        self.assertFalse(OutboxEvent.objects.filter(consumer=TASK_CONSUMER).exists())

    def test_a_task_outside_the_mapped_lists_is_no_row(self):
        tasks.create(company=self.a.company, title="Not for ClickUp")
        tasks.create(company=self.a.company, title="Unknown list", metadata={KEY_LIST: "555"})
        self.assertFalse(OutboxEvent.objects.filter(consumer=TASK_CONSUMER).exists())

    def test_a_push_rule_sends_a_kind_to_a_list(self):
        IntegrationAccount.objects.filter(pk=self.a.account.pk).update(
            settings={**SETTINGS, "push_rules": {"call": LIST}})
        tasks.create(company=self.a.company, title="Ring Ada", kind="call")
        row = OutboxEvent.objects.get(consumer=TASK_CONSUMER)
        self.assertEqual(row.payload, {"task": str(row.object_id), "op": "create", "list_id": LIST})

    def test_a_disabled_account_or_another_tenant_is_no_row(self):
        IntegrationAccount.objects.filter(pk=self.a.account.pk).update(enabled=False)
        tasks.create(company=self.a.company, title="Off", metadata={KEY_LIST: LIST})
        self.assertFalse(OutboxEvent.objects.filter(consumer=TASK_CONSUMER).exists())
        tasks.create(company=self.b.company, title="On", metadata={KEY_LIST: LIST})
        self.assertEqual(OutboxEvent.objects.get(consumer=TASK_CONSUMER).company_id, self.b.company.pk)

    def test_build_is_pure_of_network(self):
        task = self.linked()
        specs = build_push("task_updated", Task, task, {"task": task, "update_fields": ("title",)})
        self.assertEqual(specs[0].payload["fields"], {"title": "Call Ada back"})


class TenantIsolationTests(ClickUpTestCase):
    def test_a_golden_id_from_another_tenant_is_unknown_here(self):
        theirs = self.linked(self.a, cu_id="86a0fx01")
        adapter, _ = self.adapter(self.b)
        (outcome,) = self.ingest(adapter, cu_task("86a0fx01", golden_id=str(theirs.pk)))
        self.assertEqual(outcome.reason, "golden_id_unknown")
        self.assertFalse(Task.objects.filter(company=self.b.company).exists())
        theirs.refresh_from_db()
        self.assertEqual(theirs.title, "Call Ada back")

    def test_the_same_clickup_id_in_two_tenants_touches_only_its_own(self):
        mine = self.linked(self.a)
        theirs = self.linked(self.b)
        adapter, _ = self.adapter(self.b)
        self.ingest(adapter, cu_task("86a0fx01", name="Beta's title"))
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual((mine.title, theirs.title), ("Call Ada back", "Beta's title"))

    def test_an_engagement_number_resolves_in_the_accounts_tenant_only(self):
        adapter, _ = self.adapter()
        self.ingest(adapter, cu_task("86a0fx02", engagement="EN-00043"))  # Beta's engagement
        task = Task.objects.get(company=self.a.company, source="clickup")
        self.assertIsNone(task.engagement_id)

    def test_a_push_handler_never_loads_another_tenants_task(self):
        theirs = self.linked(self.b)
        outbox.enqueue(TASK_CONSUMER, outbox.EventSpec(
            idempotency_key="cross", company_id=self.a.company.pk,
            payload={"task": str(theirs.pk), "op": "update", "list_id": LIST, "fields": {"title": "x"}}))
        transport = FixtureTransport(responses=[])
        with use_transport(transport):
            drain(TASK_CONSUMER)
        self.assertEqual(transport.recorded, [])
        self.assertEqual(OutboxEvent.objects.get(idempotency_key="cross").state, "cancelled")


class WebhookTests(ClickUpTestCase):
    def body(self):
        return json.dumps({"event": "taskCreated", "task_id": "86a0fx02"}, separators=(",", ":")).encode()

    def test_signature(self):
        adapter, _ = self.adapter()
        body = self.body()
        good = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(adapter.verify_webhook({"x-signature": good}, body).ok)
        self.assertEqual(adapter.verify_webhook({"x-signature": good}, body + b" ").reason, "bad_signature")
        self.assertEqual(adapter.verify_webhook({}, body).reason, "missing_signature")

    def test_another_tenants_secret_does_not_verify(self):
        set_credential(self.b.account, "webhook_secret", "beta-secret-only")
        body = self.body()
        signed_for_a = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        adapter, _ = self.adapter(self.b)
        self.assertFalse(adapter.verify_webhook({"x-signature": signed_for_a}, body).ok)


class SettingsTests(ClickUpTestCase):
    def test_valid_settings(self):
        adapter, _ = self.adapter()
        self.assertEqual(adapter.validate_settings({**SETTINGS, "push_rules": {"call": LIST}}), [])

    def test_errors_name_problem_cause_and_fix(self):
        adapter, _ = self.adapter()
        errors = adapter.validate_settings({
            "lists": {LIST: {"status_map": {"to do": "nonsense"}, "colour": "red"}},
            "push_rules": {"call": "404", "no_such_kind": LIST},
        })
        self.assertEqual(len(errors), 5, errors)
        for error in errors:
            self.assertIn("cause:", error)
            self.assertIn("Fix:", error)


class ErasureTests(ClickUpTestCase):
    def test_erasure_refs_names_the_mirrors_of_the_scrubbed_tasks(self):
        task = self.linked()
        other = self.linked(cu_id="86a0fx05")
        self.linked(self.b, cu_id="86a0fx08")
        adapter, _ = self.adapter()
        refs = adapter.erasure_refs(SimpleNamespace(tasks=[task.pk, other.pk], parties=[self.a.ada.pk]))
        self.assertEqual([r.source_id for r in refs], ["86a0fx01", "86a0fx05"])
        self.assertEqual(adapter.erasure_refs(SimpleNamespace(parties=[self.a.ada.pk])), [])

    def test_erase_drops_the_provider_ids_from_its_row(self):
        self.linked()
        outbox.enqueue(ERASE := "clickup.erase", outbox.EventSpec(
            idempotency_key="erase:1", company_id=self.a.company.pk,
            payload={"refs": [{"object_type": "task", "source_id": "86a0fx01"}]}), ignore_suppression=True)
        transport = FixtureTransport(responses=[
            response("PUT", "/api/v2/task/86a0fx01", cu_task("86a0fx01", name="Erased")),
            response("DELETE", "/api/v2/task/86a0fx01/field/cf_engagement", {})])
        with use_transport(transport):
            drain(ERASE)
        row = OutboxEvent.objects.get(idempotency_key="erase:1")
        self.assertEqual((row.state, row.payload), ("done", {"erased": True, "count": 1}))
        self.assertEqual([r["method"] for r in transport.recorded], ["PUT", "DELETE"])  # no DELETE of the task


class SecretsNeverLeakTests(ClickUpTestCase):
    def test_a_refused_push_leaks_the_token_nowhere(self):
        task = self.linked()
        tasks.edit(task, title="Rename")
        transport = FixtureTransport(responses=[response("PUT", "/api/v2/task/86a0fx01",
                                                         {"err": f"bad token {API_KEY}"}, status=400)])
        with use_transport(transport), self.assertLogs(level=logging.DEBUG) as logs:
            drain(TASK_CONSUMER)
        row = OutboxEvent.objects.get(consumer=TASK_CONSUMER)
        self.assertEqual(row.state, "dead")
        haystacks = ["\n".join(logs.output), json.dumps(row.payload), row.last_error,
                     json.dumps(list(ExternalRef.objects.values_list("metadata", flat=True))),
                     json.dumps(Task.objects.get(pk=task.pk).metadata), json.dumps(transport.recorded)]
        for text in haystacks:
            self.assertNotIn(API_KEY, text)
        self.assertEqual(transport.recorded[0]["headers"]["authorization"], "<redacted:api_key>")


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class DeterminismTests(TestCase):
    def test_every_case_replays_byte_identically(self):
        for case_dir in sorted(p for p in FIXTURES.iterdir() if (p / "case.json").exists()):
            outputs = []
            for _ in range(2):
                with transaction.atomic():
                    result = run_case(ClickUpAdapter, case_dir, seed_hook=seed_hook)
                    transaction.set_rollback(True)
                outputs.append((canonical_json(result.requests), canonical_json(result.db)))
            self.assertEqual(outputs[0], outputs[1], case_dir.name)


class ReviewRegressionTests(ClickUpTestCase):
    """The Code Reviewer pass on the draft (echo loops, ordering, ownership)."""

    def test_an_unresolved_engagement_is_stable_across_ingests(self):
        adapter, _ = self.adapter()
        self.ingest(adapter, cu_task("86a0fx02", engagement="EN-99999"))
        (again,) = self.ingest(adapter, cu_task("86a0fx02", engagement="EN-99999"))
        self.assertFalse(again.changed)
        self.assertEqual(again.reason, "")

    def test_an_empty_link_is_filled_once_the_number_resolves(self):
        adapter, _ = self.adapter()
        self.ingest(adapter, cu_task("86a0fx02", engagement="EN-99999"))
        Engagement._base_manager.filter(pk=self.a.deal.pk).update(number="EN-99999")
        self.ingest(adapter, cu_task("86a0fx02", engagement="EN-99999", updated="1772463700000"))
        task = Task.objects.get(company=self.a.company, source="clickup")
        self.assertEqual((task.engagement_id, task.party_id), (self.a.deal.pk, self.a.ada.pk))
        self.assertNotIn(KEY_FLAGS, task.metadata)
        self.assertFalse(OutboxEvent.objects.filter(consumer=TASK_CONSUMER).exists())

    def test_a_full_save_of_an_untouched_task_pushes_nothing(self):
        task = self.linked()
        task = Task.objects.get(pk=task.pk)
        task.save()  # the admin, the activity compat endpoint
        self.assertFalse(OutboxEvent.objects.filter(consumer=TASK_CONSUMER).exists())
        task.title = "Only this"
        task.save()
        row = OutboxEvent.objects.get(consumer=TASK_CONSUMER)
        self.assertEqual(row.payload["fields"], {"title": "Only this"})

    def test_a_retried_older_change_never_rolls_back_a_newer_one(self):
        import datetime as _dt

        task = self.linked()
        t0 = _dt.datetime(2026, 3, 2, 15, 0, tzinfo=_dt.UTC)
        for i, title in enumerate(("One", "Two")):
            outbox.enqueue(TASK_CONSUMER, outbox.EventSpec(
                idempotency_key=f"t{i}", company_id=self.a.company.pk, occurred_at=t0 + _dt.timedelta(seconds=i),
                payload={"task": str(task.pk), "op": "update", "list_id": LIST, "fields": {"title": title}}))
        two = OutboxEvent.objects.get(idempotency_key="t1")
        transport = FixtureTransport(responses=[response("PUT", "/api/v2/task/86a0fx01", cu_task("86a0fx01", "Two"))])
        from golden_erp.addons.clickup.adapter import handle_push

        adapter = ClickUpAdapter(self.a.account, transport)
        handle_push(adapter, two)  # "Two" lands first; "One" was delayed by a retry
        one = OutboxEvent.objects.get(idempotency_key="t0")
        self.assertIsInstance(handle_push(adapter, one), outbox.Done)
        self.assertEqual([r["json"] for r in transport.recorded], [{"name": "Two"}])

    def test_an_update_behind_a_pending_create_is_covered_by_it(self):
        with suppressed():
            task = tasks.create(company=self.a.company, title="Never pushed", metadata={KEY_LIST: LIST})
        outbox.enqueue(TASK_CONSUMER, outbox.EventSpec(
            idempotency_key=f"{task.pk}:created", company_id=self.a.company.pk,
            payload={"task": str(task.pk), "op": "create", "list_id": LIST}))
        tasks.edit(task, title="Renamed before the push")
        update = OutboxEvent.objects.get(consumer=TASK_CONSUMER, event="update")
        from golden_erp.addons.clickup.adapter import handle_push

        adapter, transport = self.adapter()
        self.assertIsInstance(handle_push(adapter, update), outbox.Done)
        self.assertEqual(transport.recorded, [])

    def test_any_retry_of_a_create_searches_first(self):
        with suppressed():
            task = tasks.create(company=self.a.company, title="Lost answer", metadata={KEY_LIST: LIST})
        outbox.enqueue(TASK_CONSUMER, outbox.EventSpec(
            idempotency_key=f"{task.pk}:created", company_id=self.a.company.pk,
            payload={"task": str(task.pk), "op": "create", "list_id": LIST}))
        row = OutboxEvent.objects.get(consumer=TASK_CONSUMER)
        row.attempts = 2  # the worker died after the POST: no create_unknown was written
        found = cu_task("86a0fx0a", "Lost answer", golden_id=str(task.pk))
        transport = FixtureTransport(responses=[response("GET", f"/api/v2/list/{LIST}/task",
                                                         {"tasks": [found], "last_page": True}),
                                                response("PUT", "/api/v2/task/86a0fx0a", found)])
        from golden_erp.addons.clickup.adapter import handle_push

        handle_push(ClickUpAdapter(self.a.account, transport), row)
        self.assertEqual([r["method"] for r in transport.recorded], ["GET", "PUT"])  # search, then reassert
        self.assertEqual(ExternalRef.objects.get(source="clickup", object_id=task.pk).source_id, "86a0fx0a")

    def test_a_raced_second_create_is_refused_not_a_second_ref(self):
        from golden_erp.addons.clickup.adapter import DuplicateTask, _adopt

        task = self.linked()
        adapter, _ = self.adapter()
        with self.assertRaises(DuplicateTask):
            _adopt(adapter, task, LIST, cu_task("86a0fx0b"))
        self.assertEqual(ExternalRef.objects.filter(source="clickup", object_id=task.pk).count(), 1)

    def test_an_echoed_due_with_microseconds_does_not_reschedule(self):
        task = self.linked(due=dt.datetime(2026, 3, 9, 16, 30, 0, 123456, tzinfo=dt.UTC))
        events = task.events.count()
        adapter, _ = self.adapter()
        echo = {**cu_task("86a0fx01", "Call Ada back", assignees=[("81000001", self.a.sam.user.email)]),
                "due_date": "1773073800123", "due_date_time": True}
        self.ingest(adapter, echo)
        self.assertEqual(task.events.count(), events)

    def test_an_unmapped_status_pushes_the_rest_then_dead_letters(self):
        task = self.linked()
        tasks.cancel(task)  # "cancelled" is not in SETTINGS' status_map
        task.title = "Also renamed"
        row = OutboxEvent.objects.get(consumer=TASK_CONSUMER)
        row.payload = {**row.payload, "fields": {"status": "cancelled", "title": "Also renamed"}}
        from golden_erp.addons.clickup.adapter import handle_push

        transport = FixtureTransport(responses=[response("PUT", "/api/v2/task/86a0fx01", cu_task("86a0fx01"))])
        outcome = handle_push(ClickUpAdapter(self.a.account, transport), row)
        self.assertEqual(outcome, outbox.Dead("status_unmapped"))
        self.assertEqual([r["json"] for r in transport.recorded], [{"name": "Also renamed"}])

    def test_done_in_clickup_after_a_golden_cancel_completes(self):
        task = self.linked()
        with suppressed():
            tasks.cancel(task)
        adapter, _ = self.adapter()
        self.ingest(adapter, cu_task("86a0fx01", status=("complete", "closed")))
        task.refresh_from_db()
        self.assertEqual(task.status, "done")

    def test_moving_out_of_the_mapped_lists_pauses_the_sync_and_coming_back_resumes(self):
        task = self.linked()
        adapter, _ = self.adapter()
        (outcome,) = self.ingest(adapter, cu_task("86a0fx01", list_id="999"))
        self.assertEqual(outcome.reason, "left_mapped_lists")
        task.refresh_from_db()
        tasks.edit(task, title="Not pushed")
        self.assertFalse(OutboxEvent.objects.filter(consumer=TASK_CONSUMER).exists())
        self.ingest(adapter, cu_task("86a0fx01", name="Back", updated="1772463700000"))
        task.refresh_from_db()
        self.assertEqual((task.title, task.metadata.get(KEY_LIST)), ("Back", LIST))
        tasks.edit(task, title="Pushed again")
        self.assertTrue(OutboxEvent.objects.filter(consumer=TASK_CONSUMER).exists())

    def test_giving_a_task_its_list_later_is_its_create(self):
        with suppressed():
            task = tasks.create(company=self.a.company, title="Later")
        task.metadata = {KEY_LIST: LIST}
        task.save(update_fields={"metadata", "updated_at"})
        row = OutboxEvent.objects.get(consumer=TASK_CONSUMER)
        self.assertEqual((row.event, row.idempotency_key), ("create", f"{task.pk}:created"))

    def test_push_status_must_map_back(self):
        adapter, _ = self.adapter()
        errors = adapter.validate_settings({"lists": {LIST: {
            "golden_id_field": "g", "status_map": {"to do": "open"}, "push_status": {"cancelled": "closed"}}}})
        self.assertEqual(len(errors), 1)
        self.assertIn('"closed": "cancelled"', errors[0])


class SecondReviewTests(ClickUpTestCase):
    def test_an_update_behind_a_running_create_waits(self):
        from golden_erp.addons.clickup.adapter import handle_push

        with suppressed():
            task = tasks.create(company=self.a.company, title="In flight", metadata={KEY_LIST: LIST})
        outbox.enqueue(TASK_CONSUMER, outbox.EventSpec(
            idempotency_key=f"{task.pk}:created", company_id=self.a.company.pk,
            payload={"task": str(task.pk), "op": "create", "list_id": LIST}))
        OutboxEvent.objects.filter(idempotency_key=f"{task.pk}:created").update(state="running")
        tasks.edit(task, title="Edited while creating")
        update = OutboxEvent.objects.get(consumer=TASK_CONSUMER, event="update")
        adapter, transport = self.adapter()
        outcome = handle_push(adapter, update)
        self.assertEqual(outcome.error, "awaiting_create")
        self.assertEqual(transport.recorded, [])

    def test_the_high_water_mark_compares_instants_not_text(self):
        from golden_erp.addons.clickup.adapter import handle_push

        task = self.linked()
        ExternalRef.objects.filter(source_id="86a0fx01").update(
            metadata={"date_updated": "1", "pushed": {"title": "2026-03-02T15:00:00Z"}})
        outbox.enqueue(TASK_CONSUMER, outbox.EventSpec(
            idempotency_key="later", company_id=self.a.company.pk,
            occurred_at=dt.datetime(2026, 3, 2, 15, 0, 0, 500000, tzinfo=dt.UTC),
            payload={"task": str(task.pk), "op": "update", "list_id": LIST, "fields": {"title": "Later"}}))
        transport = FixtureTransport(responses=[response("PUT", "/api/v2/task/86a0fx01", cu_task("86a0fx01", "Later"))])
        handle_push(ClickUpAdapter(self.a.account, transport), OutboxEvent.objects.get(idempotency_key="later"))
        self.assertEqual([r["json"] for r in transport.recorded], [{"name": "Later"}])

    def test_clickup_cannot_link_a_task_golden_left_unlinked(self):
        with suppressed():
            task = tasks.create(company=self.a.company, title="Unlinked on purpose", metadata={KEY_LIST: LIST})
        ExternalRef.objects.create(company=self.a.company, source="clickup", object_type="task", source_id="86a0fx0c",
                                   content_type=ContentType.objects.get_for_model(Task), object_id=task.pk)
        adapter, _ = self.adapter()
        (outcome,) = self.ingest(adapter, cu_task("86a0fx0c", "Unlinked on purpose", engagement="EN-00042"))
        task.refresh_from_db()
        self.assertIsNone(task.engagement_id)
        self.assertEqual(outcome.reason, "refused:engagement")

    def test_a_moved_out_task_keeps_its_list_for_erasure(self):
        task = self.linked()
        adapter, _ = self.adapter()
        self.ingest(adapter, cu_task("86a0fx01", list_id="999"))
        task.refresh_from_db()
        self.assertEqual(task.metadata[KEY_LIST], LIST)
        self.assertEqual(adapter._engagement_field_for("86a0fx01"), "cf_engagement")
