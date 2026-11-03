"""Erasure reaches the providers (docs/crm/phase6-adapters.md section 4, step 8).

A toy ``ERASE`` adapter (``eraser``) proves the core sweep: the collector
reads the person's provider ids before ``_flag_refs`` hashes them, one
``eraser.erase`` row carries them, the row survives any provider failure and
is reported until done, and nothing about the person is left queued. The
real adapters' sweeps are in ``test_erasure_sweep_adapters.py``.
"""

from __future__ import annotations

import datetime as dt
from io import StringIO
from unittest import mock, skipUnless

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.integrations import ratelimit
from core.integrations.erasure import allow_account_delete, consumer_name
from core.integrations.transport import use_transport
from core.models import AuditLog, Company, ExternalRef, IntegrationAccount, OutboxEvent, UserProfile
from core.outbox.testing import drain
from core.plugins.base import BasePlugin
from core.plugins.crm_adapter import BaseCrmAdapter, Capability, ProviderRef, RateLimit
from core.provenance import is_erased_source
from core.testing.integration_fixtures import FixtureTransport
from core.tests.helpers import LOGIN_LOCK_ORDER, login_lock_order, make_staff_user
from crm.models import Task
from parties.erasure import erase_party
from parties.models import Party
from parties.tests.test_erasure import person

PROVIDER = "eraser"
ERASE = consumer_name(PROVIDER)


class EraserAdapter(BaseCrmAdapter):
    """Erases a profile per party ref and scrubs a task per mirrored task."""

    provider_id = PROVIDER
    capabilities = frozenset({Capability.ERASE})
    rate_limit = RateLimit(requests=1000, per_seconds=1)
    base_url = "https://api.eraser.example.com"

    def erasure_refs(self, footprint):
        party_ct = ContentType.objects.get_for_model(Party)
        task_ct = ContentType.objects.get_for_model(Task)
        rows = ExternalRef._base_manager.filter(company=self.account.company, source=PROVIDER).filter(
            content_type=party_ct, object_id__in=footprint.parties) | ExternalRef._base_manager.filter(
            company=self.account.company, source=PROVIDER, content_type=task_ct, object_id__in=footprint.tasks)
        return [ProviderRef(ot, sid) for ot, sid in rows.order_by("object_type", "source_id").values_list(
            "object_type", "source_id")]

    def erase(self, refs, *, done=None):
        for ref in refs:
            if ref.object_type == "profile":
                self.http("DELETE", f"/v1/profiles/{ref.source_id}", expect=(404,))
            elif ref.object_type == "task":
                self.http("PUT", f"/v1/tasks/{ref.source_id}", json={"name": "Erased", "description": ""})
            done(ref)


class EraserPlugin(BasePlugin):
    name = "eraser_test"

    def get_crm_adapters(self):
        return {PROVIDER: EraserAdapter}


def ok(method, path, status=200):
    return {"schema_version": 1, "provider": PROVIDER, "request": {"method": method, "path": path},
            "response": {"status": status, "headers": {}}, "payload": {}}


@override_settings(GOLDEN_PLUGINS=[EraserPlugin])
class SweepTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other = Company.objects.create(name="Tenant B")

    def setUp(self):
        ratelimit.reset()
        self.addCleanup(ratelimit.reset)
        self.staff = make_staff_user("sweeper", password="pw")
        UserProfile.objects.update_or_create(user=self.staff, defaults={"company": self.company})
        self.party = person(self.company, "Quill", "Zebedee", email="qz@example.com")
        self.task = Task.objects.create(company=self.company, party=self.party, title="Call Quill")
        self.account = IntegrationAccount.objects.create(company=self.company, provider=PROVIDER, enabled=True)
        self.profile_ref = self.ref(self.party, "profile", "P-1")
        self.task_ref = self.ref(self.task, "task", "T-1", metadata={"date_updated": "1700000000000"})
        # Tenant B: the same provider, a person of its own, a colliding id.
        self.other_party = person(self.other, "Other", "Person", email="op@example.com")
        IntegrationAccount.objects.create(company=self.other, provider=PROVIDER, enabled=True)
        self.other_ref = self.ref(self.other_party, "profile", "P-1", company=self.other)

    def ref(self, obj, object_type, source_id, company=None, metadata=None):
        return ExternalRef.objects.create(
            company=company or self.company, source=PROVIDER, object_type=object_type, source_id=source_id,
            content_type=ContentType.objects.get_for_model(type(obj)), object_id=obj.pk,
            source_url=f"https://app.eraser.example.com/{source_id}", metadata=metadata or {})

    def erase(self):
        with self.captureOnCommitCallbacks(execute=True):
            return erase_party(self.party, self.staff, "Art. 17 request #7")

    def erase_rows(self):
        return OutboxEvent.objects.filter(consumer=ERASE)

    # -- the row --------------------------------------------------------------

    def test_one_row_carries_the_clear_ids_read_before_hashing(self):
        result = self.erase()
        row = self.erase_rows().get()
        self.assertEqual(row.idempotency_key, f"erase:{result.erasure_id}")
        self.assertEqual(row.company_id, self.company.pk)
        self.assertEqual(row.party_id, self.party.pk)
        self.assertEqual(row.state, "pending")
        self.assertEqual(row.max_attempts, 20)
        self.assertEqual(row.payload["refs"], [{"object_type": "profile", "source_id": "P-1"},
                                               {"object_type": "task", "source_id": "T-1"}])
        self.assertEqual(row.payload["erasure_id"], str(result.erasure_id))
        # The party's ref is re-keyed now; the mirror ref keeps its clear id.
        self.profile_ref.refresh_from_db()
        self.assertTrue(self.profile_ref.source_id.startswith("erased:"))
        self.task_ref.refresh_from_db()
        self.assertEqual(self.task_ref.source_id, "T-1")
        self.assertEqual(self.task_ref.source_url, "")
        self.assertTrue(self.task_ref.metadata["erased"])
        self.assertEqual(self.task_ref.metadata["date_updated"], "1700000000000")
        self.assertEqual(result.counts["provider_erasures"], 1)
        self.assertEqual(result.counts["mirror_refs_flagged"], 1)
        audit = AuditLog.objects.get(action="erase", object_id=self.party.pk)
        self.assertEqual(audit.changes["erasure_id"], str(result.erasure_id))

    def test_tenant_isolation(self):
        self.erase()
        self.assertFalse(self.erase_rows().filter(company=self.other).exists())
        self.other_ref.refresh_from_db()
        self.assertEqual(self.other_ref.source_id, "P-1")
        self.assertNotIn("erased", self.other_ref.metadata)
        self.assertFalse(is_erased_source(self.other.pk, PROVIDER, "profile", "P-1"))

    def test_no_refs_no_row(self):
        ExternalRef.objects.filter(company=self.company).delete()
        result = self.erase()
        self.assertFalse(self.erase_rows().exists())
        self.assertEqual(result.counts["provider_erasures"], 0)

    def test_tombstones_block_re_sync(self):
        self.erase()
        self.assertTrue(is_erased_source(self.company.pk, PROVIDER, "profile", "P-1"))
        self.assertTrue(is_erased_source(self.company.pk, PROVIDER, "task", "T-1"))

    # -- the outbox purge ------------------------------------------------------

    def test_queued_work_about_the_person_is_purged_but_erase_rows_are_not(self):
        now = timezone.now()

        def row(consumer, state, key):
            return OutboxEvent.objects.create(
                company_id=self.company.pk, consumer=consumer, event="sync", idempotency_key=key,
                party_id=self.party.pk, payload={"email": "qz@example.com"}, state=state,
                next_attempt_at=now, occurred_at=now, completed_at=now if state in ("done", "dead") else None)

        row("eraser.sync", "pending", "a")
        row("eraser.sync", "dead", "b")
        done = row("eraser.sync", "done", "c")
        earlier = row(ERASE, "pending", "erase:earlier")  # an earlier erasure's, still to run
        self.erase()
        self.assertFalse(OutboxEvent.objects.filter(consumer="eraser.sync", state__in=["pending", "dead"]).exists())
        done.refresh_from_db()
        self.assertEqual(done.payload, {"erased": True})
        earlier.refresh_from_db()
        self.assertEqual(earlier.state, "pending")
        self.assertEqual(earlier.payload, {"email": "qz@example.com"})
        self.assertEqual(self.erase_rows().count(), 2)

    def test_rows_keyed_by_a_footprint_object_not_the_party_are_purged_too(self):
        """A task push (``clickup.task``) pins the task's title and names the task, not the party."""
        now = timezone.now()

        def row(state, key, obj=None):
            obj = obj or self.task
            return OutboxEvent.objects.create(
                company_id=self.company.pk, consumer="eraser.task", event="push", idempotency_key=key,
                object_type=obj._meta.label_lower, object_id=obj.pk, payload={"title": "Call Quill"},
                state=state, next_attempt_at=now, occurred_at=now,
                completed_at=now if state in ("done", "dead") else None)

        row("pending", "p")
        row("dead", "d")
        done = row("done", "x")
        stranger = Task.objects.create(company=self.company, title="Unrelated")
        kept = row("pending", "k", obj=stranger)
        result = self.erase()
        self.assertEqual(set(OutboxEvent.objects.filter(consumer="eraser.task").values_list("pk", flat=True)),
                         {done.pk, kept.pk})
        done.refresh_from_db()
        self.assertEqual(done.payload, {"erased": True})
        kept.refresh_from_db()
        self.assertEqual(kept.payload, {"title": "Call Quill"}, "another task's row is not touched")
        self.assertEqual(result.counts["outbox_deleted"], 2)

    # -- delivery ----------------------------------------------------------------

    def test_draining_sends_exactly_the_provider_operations_then_forgets_the_ids(self):
        self.erase()
        transport = FixtureTransport(responses=[ok("DELETE", "/v1/profiles/P-1", 202),
                                                ok("PUT", "/v1/tasks/T-1")])
        with use_transport(transport):
            drain(ERASE)
        self.assertEqual([(r["method"], r["path"], r["json"]) for r in transport.comparable()], [
            ("DELETE", "/v1/profiles/P-1", None),
            ("PUT", "/v1/tasks/T-1", {"name": "Erased", "description": ""}),
        ])
        row = self.erase_rows().get()
        self.assertEqual(row.state, "done")
        self.assertEqual(row.payload, {"erased": True, "count": 2})

    def test_a_provider_failure_is_a_pending_sweep_never_a_skip(self):
        result = self.erase()
        self.assertIsNotNone(Party._base_manager.get(pk=self.party.pk).erased_at, "the local erasure is done")
        with use_transport(FixtureTransport(responses=[ok("DELETE", "/v1/profiles/P-1", 202),
                                                       ok("PUT", "/v1/tasks/T-1", 503)])):
            drain(ERASE)
        row = self.erase_rows().get()
        self.assertEqual(row.state, "pending")
        self.assertEqual(row.last_error, "http_503")
        self.assertEqual(row.payload["refs"], [{"object_type": "task", "source_id": "T-1"}],
                         "the finished profile left the row; only the task is retried")
        out = StringIO()
        call_command("check_erasure", stdout=out)
        self.assertIn("eraser.erase pending", out.getvalue())
        self.assertIn(f"party={self.party.pk}", out.getvalue())
        self.assertNotIn("T-1", out.getvalue(), "the report names no provider id")
        with self.assertRaises(SystemExit):
            call_command("check_erasure", "--max-age-hours", "0", stdout=StringIO())
        # The retry, once due, sends only what is left.
        transport = FixtureTransport(responses=[ok("PUT", "/v1/tasks/T-1")])
        with use_transport(transport):
            drain(ERASE, now=timezone.now() + dt.timedelta(hours=1))
        self.assertEqual([r["path"] for r in transport.comparable()], ["/v1/tasks/T-1"])
        row.refresh_from_db()
        self.assertEqual((row.state, row.payload), ("done", {"erased": True, "count": 2}))
        self.assertEqual(result.counts["provider_erasures"], 1)

    def test_a_dead_row_fails_check_erasure(self):
        self.erase()
        with use_transport(FixtureTransport(responses=[ok("DELETE", "/v1/profiles/P-1", 400)])):
            drain(ERASE)
        self.assertEqual(self.erase_rows().get().state, "dead")
        with self.assertRaises(SystemExit):
            call_command("check_erasure", stdout=StringIO())

    def test_a_disabled_account_is_still_swept_and_its_row_waits(self):
        IntegrationAccount.objects.filter(pk=self.account.pk).update(enabled=False)
        self.erase()
        with use_transport(FixtureTransport(responses=[])):
            drain(ERASE)
        row = self.erase_rows().get()
        self.assertEqual((row.state, row.last_error), ("pending", "account_disabled"))
        self.assertEqual(len(row.payload["refs"]), 2)

    def test_a_failing_collector_falls_back_and_never_blocks_the_erasure(self):
        def broken(adapter, footprint):
            with connection.cursor() as cursor:  # aborts a Postgres transaction without the savepoint
                cursor.execute("SELECT * FROM no_such_table_p6s8")

        with mock.patch.object(EraserAdapter, "erasure_refs", broken), \
                self.assertLogs("golden_erp.integrations", "ERROR"):
            result = self.erase()
        self.assertIsNotNone(Party._base_manager.get(pk=self.party.pk).erased_at)
        row = self.erase_rows().get()
        self.assertTrue(row.payload["collector_failed"])
        self.assertEqual(row.payload["refs"], [{"object_type": "profile", "source_id": "P-1"},
                                               {"object_type": "task", "source_id": "T-1"}])
        self.assertEqual(result.counts["provider_erasures"], 1)

    def test_an_uninstalled_adapter_with_refs_leaves_an_orphaned_row_that_is_reported(self):
        with override_settings(GOLDEN_PLUGINS=[]):
            self.erase()
            row = self.erase_rows().get()
            self.assertEqual(row.payload["refs"][0], {"object_type": "profile", "source_id": "P-1"})
            out = StringIO()
            with self.assertRaises(SystemExit):
                call_command("check_erasure", stdout=out)
            self.assertIn("orphaned", out.getvalue())

    def test_a_disabled_account_never_spends_the_rows_attempts(self):
        IntegrationAccount.objects.filter(pk=self.account.pk).update(enabled=False)
        self.erase()
        later = timezone.now()
        with use_transport(FixtureTransport(responses=[])):
            for _ in range(25):  # more than ERASE_MAX_ATTEMPTS hourly waits
                later += dt.timedelta(hours=2)
                drain(ERASE, now=later)
        row = self.erase_rows().get()
        self.assertEqual((row.state, row.attempts), ("pending", 0))
        OutboxEvent.objects.filter(pk=row.pk).update(created_at=timezone.now() - dt.timedelta(days=2))
        report = StringIO()
        with self.assertRaises(SystemExit):  # late after a day: reported, never dropped
            call_command("check_erasure", stdout=report)

    def test_a_deleted_account_still_gets_its_row_and_a_new_account_is_never_taken_for_it(self):
        with allow_account_delete():
            IntegrationAccount.objects.filter(pk=self.account.pk).delete()
        self.erase()
        row = self.erase_rows().get()
        self.assertEqual(row.payload["account_id"], "none")
        self.assertEqual(len(row.payload["refs"]), 2)
        with use_transport(FixtureTransport(responses=[])):
            drain(ERASE)
        row.refresh_from_db()
        self.assertEqual((row.state, row.last_error), ("pending", "account_missing"))
        IntegrationAccount.objects.create(company=self.company, provider=PROVIDER, enabled=True)
        transport = FixtureTransport(responses=[])
        with use_transport(transport):
            drain(ERASE, now=timezone.now() + dt.timedelta(hours=2))
        row.refresh_from_db()
        self.assertEqual((row.state, row.last_error), ("dead", "account_changed"))
        self.assertEqual(transport.recorded, [], "the new account's provider is never called for old records")

    def test_a_replaced_account_dead_letters_instead_of_reading_404_as_done(self):
        self.erase()
        with allow_account_delete():
            IntegrationAccount.objects.filter(pk=self.account.pk).delete()
        IntegrationAccount.objects.create(company=self.company, provider=PROVIDER, enabled=True)
        with use_transport(FixtureTransport(responses=[])):
            drain(ERASE)
        row = self.erase_rows().get()
        self.assertEqual((row.state, row.last_error), ("dead", "account_changed"))
        self.assertEqual(len(row.payload["refs"]), 2)

    def test_an_adapter_cancel_becomes_dead_never_a_quiet_drop(self):
        from core import outbox

        self.erase()
        with mock.patch.object(EraserAdapter, "erase", lambda self, refs, done=None: outbox.Cancel("nope")), \
                use_transport(FixtureTransport(responses=[])):
            drain(ERASE)
        row = self.erase_rows().get()
        self.assertEqual((row.state, row.last_error), ("dead", "cancelled:nope"))
        with self.assertRaises(SystemExit):
            call_command("check_erasure", stdout=StringIO())

    def test_check_erasure_refuses_a_malformed_company(self):
        from django.core.management import CommandError

        with self.assertRaises(CommandError):
            call_command("check_erasure", "--company", "not-a-uuid", stdout=StringIO())

    @skipUnless(connection.features.has_select_for_update, "FOR UPDATE is a no-op on this backend")
    def test_the_sweep_keeps_the_login_lock_order(self):
        with CaptureQueriesContext(connection) as queries:
            erase_party(self.party, self.staff, "Art. 17 request #7")
        order = login_lock_order(queries)
        self.assertIn("parties_party", order)
        positions = [LOGIN_LOCK_ORDER.index(table) for table in order]
        self.assertEqual(positions, sorted(set(positions)), f"locks out of the login order: {order}")
        self.assertFalse([q for q in queries if "FOR UPDATE" in q["sql"]
                          and ("core_externalref" in q["sql"] or "core_integrationaccount" in q["sql"])])
