"""The transactional outbox (docs/crm/phase6-adapters.md section 1, step 0)."""

from __future__ import annotations

import datetime as dt
import io
import threading
import time
import unittest
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from core import outbox
from core.models import Company, OutboxEvent
from core.outbox.testing import drain
from core.plugins import signals
from core.plugins.base import BasePlugin
from core.plugins.dispatch import emit, suppressed
from core.plugins.registry import plugin_registry

T0 = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc)


class _Recorder:
    """A test consumer: records every row it handles; ``outcomes`` scripts replies."""

    def __init__(self, name="test.rec", *, signals_=("order_cancelled",), provider="", outcomes=None,
                 build=None, max_attempts=10, sleep=0.0):
        self.name = name
        self.handled = []
        self.autocommit = []
        self.outcomes = list(outcomes or [])
        self.sleep = sleep
        self._lock = threading.Lock()
        self.consumer = outbox.OutboxConsumer(
            name=name, signals=tuple(signals_), build=build or self.build, handle=self.handle,
            provider=provider, max_attempts=max_attempts,
        )

    def build(self, signal_name, sender, obj, kwargs):
        key = kwargs.get("key", "k")
        return [outbox.EventSpec(idempotency_key=f"{self.name}:{key}", event=signal_name,
                                 payload={"key": key}, company_id=kwargs.get("company_id"))]

    def handle(self, event):
        with self._lock:
            self.handled.append(event.pk)
            self.autocommit.append(transaction.get_autocommit())
        if self.sleep:
            time.sleep(self.sleep)
        return self.outcomes.pop(0) if self.outcomes else outbox.Done()


class _ConsumerMixin:
    def add_consumer(self, recorder):
        outbox.register(recorder.consumer, "tests")
        self.addCleanup(outbox._builtin.pop, recorder.consumer.name, None)
        return recorder

    def emit_one(self, key="k", **kw):
        emit(signals.order_cancelled, None, None, key=key, **kw)

    def rows(self, consumer="test.rec"):
        return OutboxEvent.objects.filter(consumer=consumer)


class EnqueueTests(_ConsumerMixin, TestCase):
    def setUp(self):
        self.rec = self.add_consumer(_Recorder())

    def test_emit_writes_the_row_in_the_emitting_transaction(self):
        with transaction.atomic():
            self.emit_one()
            self.assertEqual(self.rows().count(), 1)  # before commit, same transaction
        row = self.rows().get()
        self.assertEqual((row.state, row.attempts, row.event, row.payload), ("pending", 0, "order_cancelled",
                                                                           {"key": "k"}))

    def test_a_rollback_leaves_no_row(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.emit_one()
            raise RuntimeError("the business write failed")
        self.assertFalse(self.rows().exists())

    def test_a_raising_build_fails_nothing_and_writes_no_row(self):
        def boom(*a):
            raise ValueError("bad build")

        self.add_consumer(_Recorder("test.boom", build=boom))
        company = Company.objects.create(name="Acme")
        with self.assertLogs("golden_erp.outbox", "ERROR") as logs, transaction.atomic():
            self.emit_one()
            Company.objects.filter(pk=company.pk).update(name="Acme 2")  # the transaction is still usable
        self.assertIn("consumer=test.boom", logs.output[0])
        self.assertFalse(self.rows("test.boom").exists())
        self.assertEqual(self.rows().count(), 1)  # the healthy consumer still wrote
        self.assertEqual(Company.objects.get(pk=company.pk).name, "Acme 2")

    def test_suppressed_writes_nothing(self):
        with suppressed():
            self.emit_one()
            self.assertIsNone(outbox.enqueue("test.rec", outbox.EventSpec("x", {})))
            self.assertIsNotNone(outbox.enqueue("test.rec", outbox.EventSpec("y", {}), ignore_suppression=True))
        self.assertEqual(list(self.rows().values_list("idempotency_key", flat=True)), ["y"])

    def test_origin_skips_only_the_matching_provider(self):
        self.add_consumer(_Recorder("clicky.task", provider="clicky"))
        self.add_consumer(_Recorder("other.task", provider="other"))
        with outbox.origin("clicky"):
            self.emit_one()
        self.assertFalse(self.rows("clicky.task").exists())
        self.assertTrue(self.rows("other.task").exists())
        self.assertTrue(self.rows().exists())

    def test_idempotency_key_and_pending_coalesce_key_are_unique(self):
        self.emit_one()
        self.emit_one()
        self.assertEqual(self.rows().count(), 1)
        first = outbox.enqueue("test.rec", outbox.EventSpec("a", {}, coalesce_key="party:1"))
        self.assertIsNone(outbox.enqueue("test.rec", outbox.EventSpec("b", {}, coalesce_key="party:1")))
        drain("test.rec")
        self.assertEqual(OutboxEvent.objects.get(pk=first.pk).state, "done")
        # Once the first is no longer pending, the next sync of the object queues.
        self.assertIsNotNone(outbox.enqueue("test.rec", outbox.EventSpec("b", {}, coalesce_key="party:1")))

    def test_a_retry_superseded_by_a_newer_pending_sync_is_cancelled(self):
        self.rec.outcomes = [outbox.Retry(error="http_503")]
        first = outbox.enqueue("test.rec", outbox.EventSpec("a", {}, coalesce_key="party:1"))
        now = timezone.now()
        self.assertTrue(outbox.claim(first.pk, now, worker="w"))
        second = outbox.enqueue("test.rec", outbox.EventSpec("b", {}, coalesce_key="party:1"))
        self.assertIsNotNone(second)  # the running row no longer holds the key
        held = OutboxEvent.objects.get(pk=first.pk)
        self.assertEqual(outbox.handle_one(held, self.rec.consumer, worker="w", require_autocommit=False),
                         "cancelled")
        self.assertEqual(OutboxEvent.objects.get(pk=first.pk).last_error, "coalesced")
        self.assertEqual(OutboxEvent.objects.get(pk=second.pk).state, "pending")

    def test_a_database_error_inside_build_leaves_the_transaction_usable(self):
        def bad_query(*a):
            from django.db import connection as conn

            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM no_such_table_for_outbox_test")
            return []

        self.add_consumer(_Recorder("test.badsql", build=bad_query))
        with self.assertLogs("golden_erp.outbox", "ERROR"), transaction.atomic():
            self.emit_one()
            company = Company.objects.create(name="still usable")  # fails on Postgres without the savepoint
        self.assertTrue(Company.objects.filter(pk=company.pk).exists())
        self.assertEqual(self.rows().count(), 1)

    def test_unknown_consumer_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            outbox.enqueue("nope", outbox.EventSpec("a", {}))


class _AlphaPlugin(BasePlugin):
    name = "outbox-alpha"

    def get_outbox_consumers(self):
        return [outbox.OutboxConsumer(name="alpha.sync", handle=lambda e: outbox.Done())]


class _BetaPlugin(BasePlugin):
    name = "outbox-beta"

    def get_outbox_consumers(self):
        return [outbox.OutboxConsumer(name="beta.sync", handle=lambda e: outbox.Done())]


class RegistryTests(TestCase):
    def test_a_plugin_reload_always_rebuilds_the_consumers(self):
        """Regression: the cache was keyed on id() of plugin instances; after a reload CPython
        reuses the freed ids, so a suite switching GOLDEN_PLUGINS got the previous set's
        consumers ("No outbox consumer named 'gmail.inbound' is registered")."""
        for _round in range(25):  # each switch frees one instance and allocates one: ids recycle
            with override_settings(GOLDEN_PLUGINS=["core.tests.test_outbox._AlphaPlugin"]):
                names = set(outbox.consumers())
                self.assertIn("alpha.sync", names)
                self.assertNotIn("beta.sync", names)
            with override_settings(GOLDEN_PLUGINS=["core.tests.test_outbox._BetaPlugin"]):
                names = set(outbox.consumers())
                self.assertIn("beta.sync", names)
                self.assertNotIn("alpha.sync", names)

    def test_the_cache_key_is_the_registry_generation_not_instance_ids(self):
        before = plugin_registry.generation
        plugin_registry.reload()
        self.assertEqual(plugin_registry.generation, before + 1)
        with mock.patch("core.outbox._collect", wraps=outbox._collect) as collect:
            outbox.consumers()
            outbox.consumers()
            self.assertEqual(collect.call_count, 1, "cached between reloads")
            plugin_registry.reload()
            outbox.consumers()
            self.assertEqual(collect.call_count, 2, "a reload is always a fresh lookup")

    def test_a_name_claimed_twice_fails_naming_both(self):
        consumer = outbox.OutboxConsumer(name="test.dupe", handle=lambda e: outbox.Done())
        outbox.register(consumer, "owner-a")
        self.addCleanup(outbox._builtin.pop, "test.dupe", None)
        with self.assertRaisesMessage(ImproperlyConfigured, "owner-a and owner-b"):
            outbox.register(consumer, "owner-b")

    def test_plugin_consumers_join_and_cannot_steal_a_core_name(self):
        class P(BasePlugin):
            name = "outbox-test-plugin"

            def get_outbox_consumers(self):
                return [outbox.OutboxConsumer(name=self.claim, handle=lambda e: outbox.Done())]

        good, thief = P(), P()
        good.claim, thief.claim = "test.plugin", "webhooks.deliver"
        plugins = mock.PropertyMock(return_value={good.name: good})
        with mock.patch.object(type(plugin_registry), "plugins", new_callable=lambda: plugins):
            plugin_registry.generation += 1  # what reload() does: the cache must not serve the old set
            self.assertIn("test.plugin", outbox.consumers())
            plugins.return_value = {thief.name: thief}  # a reload: new instances, fresh lookup
            plugin_registry.generation += 1
            with self.assertRaisesMessage(ImproperlyConfigured, "webhooks (core) and plugin 'outbox-test-plugin'"):
                outbox.consumers()
            from core.checks import outbox_consumers_check

            [error] = outbox_consumers_check(None)
            self.assertEqual(error.id, "core.E050")
        plugin_registry.generation += 1  # the mocked set is gone

    def test_core_registers_the_webhook_consumers(self):
        self.assertTrue({"webhooks", "webhooks.deliver"} <= set(outbox.consumers()))


class ClaimTests(_ConsumerMixin, TestCase):
    def setUp(self):
        self.rec = self.add_consumer(_Recorder())
        self.emit_one()
        self.row = self.rows().get()

    def test_a_stale_candidate_replay_claims_nothing(self):
        now = timezone.now()
        stale = outbox.candidates(now, names=["test.rec"])
        first = outbox.process(names=["test.rec"], now=now, require_autocommit=False)
        self.assertEqual(first.claimed, 1)
        # A second worker that listed the same candidates before the first claimed:
        self.assertEqual([pk for pk, _ in stale], [self.row.pk])
        self.assertFalse(outbox.claim(self.row.pk, now, worker="w2"))
        self.assertEqual(self.rec.handled, [self.row.pk])

    def test_an_expired_lease_is_reclaimed_and_the_slow_worker_cannot_finalize(self):
        now = timezone.now()
        self.assertTrue(outbox.claim(self.row.pk, now, worker="slow", lease_seconds=60))
        self.assertFalse(outbox.claim(self.row.pk, now + dt.timedelta(seconds=30), worker="fast"))
        later = now + dt.timedelta(seconds=61)
        self.assertTrue(outbox.claim(self.row.pk, later, worker="fast"))
        held = OutboxEvent.objects.get(pk=self.row.pk)
        self.assertEqual((held.locked_by, held.attempts), ("fast", 2))
        self.assertIsNone(outbox.finalize(held, outbox.Done(), worker="slow"))  # refused
        self.assertEqual(OutboxEvent.objects.get(pk=self.row.pk).state, "running")
        self.assertEqual(outbox.finalize(held, outbox.Done(), worker="fast"), "done")

    def test_the_lease_is_stamped_when_the_row_is_claimed_not_when_the_pass_began(self):
        self.emit_one("second")
        clock = [timezone.now()]
        leases = []

        def slow(event):  # each delivery takes 90 s, longer than the 60 s lease
            leases.append(OutboxEvent.objects.get(pk=event.pk).locked_until - clock[0])
            clock[0] += dt.timedelta(seconds=90)
            return outbox.Done()

        outbox._builtin["test.rec"] = (outbox.OutboxConsumer(name="test.rec", handle=slow), "tests")
        with mock.patch("core.outbox.timezone.now", side_effect=lambda: clock[0]):
            result = outbox.process(names=["test.rec"], require_autocommit=False)
        self.assertEqual(result.done, 2)
        self.assertEqual(leases, [dt.timedelta(seconds=outbox.DEFAULT_LEASE_S)] * 2)

    def test_a_row_at_max_attempts_with_an_expired_lease_goes_dead(self):
        now = timezone.now()
        OutboxEvent.objects.filter(pk=self.row.pk).update(
            state="running", attempts=10, locked_until=now - dt.timedelta(seconds=1), locked_by="dead-worker")
        result = outbox.process(names=["test.rec"], now=now, require_autocommit=False)
        self.assertEqual(result.expired_dead, 1)
        row = OutboxEvent.objects.get(pk=self.row.pk)
        self.assertEqual((row.state, row.last_error), ("dead", "lease_expired"))
        self.assertIsNotNone(row.completed_at)
        self.assertEqual(self.rec.handled, [])

    def test_a_raising_handler_is_a_retry_named_by_its_class(self):
        def boom(event):
            raise ConnectionError("secret-bearing message")

        outbox._builtin["test.rec"] = (outbox.OutboxConsumer(name="test.rec", handle=boom), "tests")
        drain("test.rec")
        row = OutboxEvent.objects.get(pk=self.row.pk)
        self.assertEqual((row.state, row.attempts, row.last_error), ("pending", 1, "ConnectionError"))


class BackoffTests(_ConsumerMixin, TestCase):
    def test_backoff_doubles_from_30s_caps_at_6h_and_jitters_20_percent(self):
        mid = lambda: 0.5  # noqa: E731 - no jitter
        self.assertEqual([outbox.backoff(n, rng=mid).total_seconds() for n in (1, 2, 3, 9, 10, 20)],
                         [30, 60, 120, 7680, 15360, 21600])
        self.assertEqual(outbox.backoff(1, rng=lambda: 0.0).total_seconds(), 24)
        self.assertEqual(outbox.backoff(1, rng=lambda: 1.0).total_seconds(), 36)

    def test_ten_failures_follow_the_table_then_dead_after_about_4_3_hours(self):
        rec = self.add_consumer(_Recorder(outcomes=[outbox.Retry(error="http_503")] * 10))
        self.emit_one()
        self.rows().update(next_attempt_at=T0)
        row = self.rows().get()
        now = T0
        waits = []
        with mock.patch("core.outbox.random.random", return_value=0.5):
            for attempt in range(1, 11):
                with mock.patch("core.outbox.timezone.now", return_value=now):
                    outbox.process(names=["test.rec"], now=now, require_autocommit=False)
                row.refresh_from_db()
                self.assertEqual(row.attempts, attempt)
                if attempt < 10:
                    self.assertEqual(row.state, "pending")
                    waits.append((row.next_attempt_at - now).total_seconds())
                    now = row.next_attempt_at
        self.assertEqual(waits, [30 * 2 ** i for i in range(9)])
        self.assertEqual(sum(waits), 15330)  # 4.26 h
        self.assertEqual((row.state, row.last_error), ("dead", "http_503"))
        self.assertEqual(len(rec.handled), 10)

    def test_retry_after_from_the_provider_wins(self):
        self.add_consumer(_Recorder(outcomes=[outbox.Retry(after=600, error="http_429")]))
        self.emit_one()
        self.rows().update(next_attempt_at=T0)
        with mock.patch("core.outbox.timezone.now", return_value=T0):
            outbox.process(names=["test.rec"], now=T0, require_autocommit=False)
        self.assertEqual(self.rows().get().next_attempt_at, T0 + dt.timedelta(seconds=600))

    def test_dead_and_cancel_outcomes(self):
        self.add_consumer(_Recorder(outcomes=[outbox.Dead("http_410"), outbox.Cancel("gone")]))
        self.emit_one("a")
        self.emit_one("b")
        drain("test.rec")
        self.assertEqual(sorted(self.rows().values_list("state", "last_error")),
                         [("cancelled", "gone"), ("dead", "http_410")])


class OperationsTests(_ConsumerMixin, TestCase):
    def setUp(self):
        self.rec = self.add_consumer(_Recorder(outcomes=[outbox.Dead("http_400")]))
        self.emit_one()
        drain("test.rec")
        self.row = self.rows().get()

    def run_cmd(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            call_command(*args, stdout=out, stderr=err)
        except SystemExit as exc:
            code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_check_outbox_is_red_on_a_dead_row_and_green_once_requeued_and_done(self):
        code, out, err = self.run_cmd("check_outbox")
        self.assertEqual(code, 1)
        self.assertIn("test.rec: dead=1", out)
        self.assertIn("1 dead row", err)
        code, out, _ = self.run_cmd("requeue_outbox", str(self.row.pk))
        self.assertIn("requeued 1", out)
        row = OutboxEvent.objects.get(pk=self.row.pk)
        self.assertEqual((row.state, row.attempts, row.completed_at), ("pending", 0, None))
        drain("test.rec")
        self.assertEqual(self.run_cmd("check_outbox")[0], 0)

    def test_check_outbox_is_red_on_an_old_pending_row(self):
        OutboxEvent.objects.all().delete()
        self.emit_one("late")
        self.assertEqual(self.run_cmd("check_outbox")[0], 0)
        OutboxEvent.objects.update(next_attempt_at=timezone.now() + dt.timedelta(hours=2),
                                   created_at=timezone.now() - dt.timedelta(hours=3))
        self.assertEqual(self.run_cmd("check_outbox")[0], 0)  # waiting out its backoff: not late
        OutboxEvent.objects.update(next_attempt_at=timezone.now() - dt.timedelta(minutes=31))
        code, _, err = self.run_cmd("check_outbox", "--max-age-minutes", "30")
        self.assertEqual(code, 1)
        self.assertIn("process_outbox", err)

    def test_check_outbox_is_red_on_stuck_and_orphaned_rows(self):
        OutboxEvent.objects.all().delete()
        OutboxEvent.objects.create(consumer="gone.plugin", event="e", idempotency_key="x")
        code, _, err = self.run_cmd("check_outbox")
        self.assertEqual(code, 1)
        self.assertIn("gone.plugin", err)
        OutboxEvent.objects.all().delete()
        OutboxEvent.objects.create(consumer="test.rec", event="e", idempotency_key="y", state="running",
                                   attempts=1, locked_until=timezone.now() - dt.timedelta(minutes=6))
        code, _, err = self.run_cmd("check_outbox")
        self.assertEqual(code, 1)
        self.assertIn("lease expired", err)

    def test_outbox_requeue_by_consumer_and_list_never_prints_the_payload(self):
        OutboxEvent.objects.filter(pk=self.row.pk).update(payload={"email": "ada@example.com"})
        _, out, _ = self.run_cmd("outbox", "list", "--state", "dead")
        self.assertIn(str(self.row.pk), out)
        self.assertIn("http_400", out)
        self.assertNotIn("ada@example.com", out)
        _, out, _ = self.run_cmd("outbox", "requeue", "--consumer", "test.rec")
        self.assertIn("requeued 1", out)

    def test_outbox_cancel(self):
        self.run_cmd("outbox", "cancel", str(self.row.pk))
        self.assertEqual(OutboxEvent.objects.get(pk=self.row.pk).state, "cancelled")

    def test_process_outbox_command_once(self):
        outbox.requeue(pks=[self.row.pk])
        self.rec.outcomes = []
        with mock.patch("core.outbox.transaction.get_autocommit", return_value=True):
            _, out, _ = self.run_cmd("process_outbox", "--once", "--consumer", "test.rec")
        self.assertIn("claimed 1, done 1", out)

    def test_prune_windows(self):
        now = timezone.now()
        OutboxEvent.objects.all().delete()
        old, fresh = now - dt.timedelta(days=8), now - dt.timedelta(hours=2)

        def row(key, state, consumer="test.rec", completed=old):
            return OutboxEvent.objects.create(consumer=consumer, event="e", idempotency_key=key, state=state,
                                              completed_at=completed if state != "pending" else None)

        row("done-old", "done")
        row("done-fresh", "done", completed=fresh)
        row("inbound-day", "done", consumer="x.inbound", completed=now - dt.timedelta(days=2))
        row("cancel-old", "cancelled")
        row("dead-old", "dead")
        row("pending", "pending")
        counts = outbox.prune(now)
        self.assertEqual(counts, {"inbound": 1, "done": 1, "cancelled": 1})
        self.assertEqual(sorted(OutboxEvent.objects.values_list("idempotency_key", flat=True)),
                         ["dead-old", "done-fresh", "pending"])


class AutocommitTests(_ConsumerMixin, TransactionTestCase):
    """Real commits: the handler runs with no transaction open."""

    def test_the_handler_runs_in_autocommit(self):
        rec = self.add_consumer(_Recorder())
        self.emit_one()
        result = outbox.process(names=["test.rec"])
        self.assertEqual(result.done, 1)
        self.assertEqual(rec.autocommit, [True])

    def test_processing_inside_a_transaction_is_refused(self):
        self.add_consumer(_Recorder())
        self.emit_one()
        with self.assertRaisesMessage(RuntimeError, "no transaction open"), transaction.atomic():
            outbox.process(names=["test.rec"])


@unittest.skipUnless(connection.vendor == "postgresql", "concurrent row claims need a second connection")
class ConcurrentWorkersTests(_ConsumerMixin, TransactionTestCase):
    """Two ``process_outbox`` workers on one queue never handle a row twice."""

    ROWS = 60

    def test_two_workers_never_double_deliver(self):
        rec = self.add_consumer(_Recorder(sleep=0.005))
        for i in range(self.ROWS):
            self.emit_one(f"k{i}")
        start = threading.Barrier(2)
        results, errors = {}, []

        def work(name):
            try:
                start.wait(5)
                total = outbox.RunResult()
                while True:
                    r = outbox.process(names=["test.rec"], limit=10, worker=name)
                    total.merge(r)
                    if not (r.claimed or r.skipped):
                        break
                results[name] = total
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=work, args=(f"worker-{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(len(rec.handled), self.ROWS)
        self.assertEqual(len(set(rec.handled)), self.ROWS)  # no row handled twice
        self.assertEqual(sum(r.claimed for r in results.values()), self.ROWS)
        self.assertTrue(all(r.claimed for r in results.values()), results)  # both really worked
        self.assertEqual(set(self.rows().values_list("state", flat=True)), {"done"})
        self.assertTrue(all(a for a in rec.autocommit))


@override_settings(WEBHOOKS_DELIVER_ON_SIGNALS=False)
class NoConsumerNoRowTests(TestCase):
    def test_webhooks_off_writes_nothing_and_costs_no_query(self):
        from django.test.utils import CaptureQueriesContext

        Company.objects.create(name="Acme")
        with CaptureQueriesContext(connection) as queries:
            emit(signals.order_placed, None, None, order=mock.MagicMock())
        self.assertEqual(queries.captured_queries, [])
        self.assertFalse(OutboxEvent.objects.exists())


class BatchTests(_ConsumerMixin, TestCase):
    """``handle_batch`` (Phase 6 step 6): rows sharing a batch key go to one call."""

    def setUp(self):
        self.calls = []
        self.script = None

        def handle_batch(events):
            self.calls.append(sorted(e.payload["key"] for e in events))
            if self.script == "raise":
                raise RuntimeError("boom")
            return [outbox.Dead("refused") if e.payload["key"] == "bad" else outbox.Done() for e in events]

        consumer = outbox.OutboxConsumer(
            name="test.batch", signals=("order_cancelled",), handle=lambda e: outbox.Done(),
            build=lambda s, sender, obj, kw: [outbox.EventSpec(
                idempotency_key=kw["key"], event="up", payload={"key": kw["key"]},
                company_id=kw.get("company_id"))],
            handle_batch=handle_batch, batch_key=lambda e: e.company_id, batch_size=2,
        )
        outbox.register(consumer, "tests")
        self.addCleanup(outbox._builtin.pop, "test.batch", None)
        self.a = Company.objects.create(name="A")
        self.b = Company.objects.create(name="B")

    def test_rows_batch_per_key_and_size_and_get_their_own_outcome(self):
        for key, company in (("a1", self.a), ("a2", self.a), ("bad", self.a), ("b1", self.b)):
            self.emit_one(key=key, company_id=company.pk)
        result = drain("test.batch")
        self.assertEqual(sorted(map(tuple, self.calls)), [("a1", "a2"), ("b1",), ("bad",)])
        self.assertEqual((result.claimed, result.done, result.dead), (4, 3, 1))
        self.assertEqual(self.rows("test.batch").get(payload__key="bad").last_error, "refused")

    def test_a_raising_batch_retries_every_row(self):
        self.script = "raise"
        for key in ("a1", "a2"):
            self.emit_one(key=key, company_id=self.a.pk)
        result = drain("test.batch")
        self.assertEqual(result.retried, 2)
        self.assertEqual(set(self.rows("test.batch").values_list("state", "last_error")),
                         {("pending", "RuntimeError")})

    def test_a_row_whose_lease_was_taken_while_it_waited_is_not_sent(self):
        self.emit_one(key="a1", company_id=self.a.pk)
        self.emit_one(key="a2", company_id=self.a.pk)
        # Simulate directly: claim both, steal one, then flush.
        stolen = OutboxEvent.objects.get(payload__key="a1")
        original_claim = outbox.claim

        def claim_and_steal(pk, now, **kw):
            won = original_claim(pk, now, **kw)
            if pk == stolen.pk:
                OutboxEvent.objects.filter(pk=pk).update(locked_by="other:1:x")
            return won

        with mock.patch.object(outbox, "claim", side_effect=claim_and_steal):
            result = outbox.process(names=["test.batch"], limit=None, require_autocommit=False)
        self.assertEqual(self.calls, [["a2"]])
        self.assertEqual(result.lost_lease, 1)

    def test_a_raising_batch_key_retries_the_row(self):
        consumer = outbox.consumers()["test.batch"]
        broken = outbox.OutboxConsumer(**{**consumer.__dict__, "batch_key": lambda e: 1 / 0})
        outbox._builtin["test.batch"] = (broken, "tests")
        self.emit_one(key="a1", company_id=self.a.pk)
        result = drain("test.batch")
        self.assertEqual((result.retried, self.calls), (1, []))
        self.assertEqual(self.rows("test.batch").get().last_error, "ZeroDivisionError")
