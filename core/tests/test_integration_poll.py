"""``integrations_poll`` (docs/crm/phase6-adapters.md 2.4, 2.9).

A 429 sets ``paused_until`` and the next poll skips the account; disabled
accounts are never polled; the first sync runs suppressed and records
``initial_sync_done``; a failing item keeps the cursor where it was; a
disconnect revokes a grant locally and enqueues the provider revocation.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from communications.models import Communication
from core.integrations import accounts as services
from core.integrations import ratelimit
from core.integrations.poll import poll_accounts
from core.models import IntegrationCredential, OutboxEvent, UserProfile
from core.outbox.testing import drain
from core.testing.integration_fixtures import FixtureTransport, fixture_secret, unlimited_buckets
from core.tests.helpers import make_staff_user
from parties.models import Party, PartyEmail

from .test_integration_secrets import PLUGINS, IntegrationTestCase


def page(data, next_after="", more=False, status=200, headers=None, path="/v1/messages", provider="fixture"):
    return {"schema_version": 1, "provider": provider, "request": {"method": "GET", "path": path},
            "response": {"status": status, "headers": headers or {}},
            "payload": {"data": data, "next_after": next_after, "has_more": more}}


def msg(i, frm="ada@example.com"):
    return {"id": f"fx_msg_{i}", "type": "message.received", "occurred_at": "2026-03-02T14:30:00Z",
            "from": frm, "to": ["sales@example.org"], "subject": f"m{i}"}


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class PollTests(IntegrationTestCase):
    def setUp(self):
        ratelimit.reset()
        self.addCleanup(ratelimit.reset)
        self.acct = self.account(enabled=True)
        services.set_credential(self.acct, "api_key", fixture_secret("api_key"))
        ada = Party.objects.create(company=self.company, kind=Party.Kind.PERSON, given_name="Ada",
                                   family_name="Lovelace", origin=Party.Origin.ERP)
        PartyEmail.objects.create(party=ada, email="ada@example.com", purpose="work")

    def poll(self, *responses):
        transport = FixtureTransport(responses=list(responses))
        with unlimited_buckets():
            report = poll_accounts(transport=transport)
        return report, transport

    def test_a_429_pauses_the_account_and_the_next_poll_skips_it(self):
        report, _ = self.poll(page([], status=429, headers={"Retry-After": "300"}))
        self.assertEqual(report.stopped, [(str(self.acct.pk), "Throttled")])
        self.acct.refresh_from_db()
        self.assertTrue(self.acct.is_paused())
        report, transport = self.poll(page([msg(1)]))
        self.assertEqual((report.accounts, report.skipped_paused), (0, 1))
        self.assertEqual(transport.recorded, [], "a paused account is not asked")
        self.acct.paused_until = timezone.now() - timedelta(seconds=1)
        self.acct.save()
        report, transport = self.poll(page([msg(1)]))
        self.assertEqual(report.accounts, 1)

    def test_disabled_accounts_are_not_polled(self):
        self.acct.enabled = False
        self.acct.save()
        report, transport = self.poll()
        self.assertEqual((report.accounts, transport.recorded), (0, []))

    def test_first_sync_is_suppressed_and_marked_done(self):
        from core.plugins import dispatch
        from golden_erp.addons.fixture_crm.adapter import FixtureCrmAdapter

        seen = []
        real = FixtureCrmAdapter.ingest

        def spy(adapter, item):
            seen.append(dispatch.is_suppressed())
            return real(adapter, item)

        with mock.patch.object(FixtureCrmAdapter, "ingest", spy):
            report, transport = self.poll(page([msg(1)], next_after="fx_msg_1"))
        self.assertEqual(seen, [True])
        self.assertIn("since", transport.recorded[0]["query"])
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.sync_state, {"cursor": {"after": "fx_msg_1"}, "initial_sync_done": True})
        self.assertEqual(Communication.objects.filter(provider="fixture").count(), 1)
        seen.clear()
        with mock.patch.object(FixtureCrmAdapter, "ingest", spy):
            _, transport = self.poll(page([msg(2)], next_after="fx_msg_2"))
        self.assertEqual(seen, [False], "later polls are live")
        self.assertEqual(transport.recorded[0]["query"], {"after": "fx_msg_1"})

    def test_a_failing_item_keeps_the_cursor(self):
        broken = dict(msg(3), occurred_at="not a date")
        report, _ = self.poll(page([msg(2), broken], next_after="fx_msg_3"))
        self.assertEqual(report.failed, 1)
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.sync_state, {})
        self.assertEqual(self.acct.last_error, "ingest_AdapterError")

    def test_the_command_prints_counts_only(self):
        out = StringIO()
        from core.integrations.transport import use_transport

        with use_transport(FixtureTransport(responses=[page([])])), unlimited_buckets():
            call_command("integrations_poll", stdout=out)
        self.assertIn("1 account(s), 1 page(s)", out.getvalue())
        self.assertNotIn(fixture_secret("api_key"), out.getvalue())


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class DisconnectTests(IntegrationTestCase):
    def setUp(self):
        self.acct = self.account(provider="fixture_oauth", enabled=True)
        services.set_credential(self.acct, "oauth_client", fixture_secret("oauth_client"))
        self.user = make_staff_user("rep", password="pw")
        self.profile = UserProfile.objects.create(user=self.user, company=self.company, display_name="rep")
        services.set_credential(self.acct, "oauth_refresh", "fixture-rt-rep", owner=self.profile)
        services.set_credential(self.acct, "oauth_access", "fixture-at-rep", owner=self.profile)
        self.client.force_login(self.user)

    def test_disconnect_revokes_locally_and_enqueues_the_provider_revocation(self):
        response = self.client.post(reverse("erp-integration-oauth-disconnect", args=[self.acct.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(IntegrationCredential.objects.filter(owner=self.profile, revoked_at=None).exists())
        row = OutboxEvent.objects.get(consumer="fixture_oauth.revoke")
        self.assertNotIn("fixture-rt-rep", str(row.payload))
        revoke = {"schema_version": 1, "provider": "fixture_oauth",
                  "request": {"method": "POST", "path": "/oauth/revoke"}, "payload": {}}
        transport = FixtureTransport(responses=[revoke])
        from core.integrations.transport import use_transport

        with use_transport(transport), unlimited_buckets():
            drain()
        self.assertEqual(transport.recorded[0]["form"], {"token": "<redacted:oauth_refresh>"})
        row.refresh_from_db()
        self.assertEqual(row.state, "done")

    def test_someone_elses_grant_needs_the_change_permission(self):
        other = make_staff_user("nosy", password="pw")
        UserProfile.objects.create(user=other, company=self.company, display_name="nosy")
        self.client.force_login(other)
        response = self.client.post(reverse("erp-integration-oauth-disconnect", args=[self.acct.pk]),
                                    {"owner": str(self.profile.pk)})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(IntegrationCredential.objects.filter(owner=self.profile, revoked_at=None).count(), 2)


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class AccountHandlerTests(IntegrationTestCase):
    """``account_handler``: the 2.9 policy as outbox outcomes, with no provider call while paused."""

    def setUp(self):
        ratelimit.reset()
        self.addCleanup(ratelimit.reset)
        self.acct = self.account(enabled=True)
        services.set_credential(self.acct, "api_key", fixture_secret("api_key"))
        self.ada = Party.objects.create(company=self.company, kind=Party.Kind.PERSON, given_name="Ada",
                                        family_name="Lovelace", origin=Party.Origin.ERP)

    def handle(self, *responses):
        from core import outbox
        from core.integrations.transport import use_transport
        from golden_erp.addons.fixture_crm.adapter import FixtureCrmAdapter

        consumer = next(c for c in FixtureCrmAdapter.outbox_consumers() if c.name == "fixture.contact")
        outbox.enqueue("fixture.contact", outbox.EventSpec(
            idempotency_key=f"h:{len(responses)}:{timezone.now().timestamp()}",
            payload={"object_id": str(self.ada.pk)}, company_id=self.company.pk))
        row = OutboxEvent.objects.filter(consumer="fixture.contact").order_by("-occurred_at").first()
        transport = FixtureTransport(responses=list(responses))
        with use_transport(transport), unlimited_buckets():
            return consumer.handle(row), transport

    def post(self, status, headers=None):
        return {"schema_version": 1, "provider": "fixture", "request": {"method": "POST", "path": "/v1/contacts"},
                "response": {"status": status, "headers": headers or {}}, "payload": {"id": "fx_ct_1"}}

    def test_429_is_retry_after_and_a_paused_account_is_not_called(self):
        from core import outbox

        outcome, _ = self.handle(self.post(429, {"Retry-After": "120"}))
        self.assertIsInstance(outcome, outbox.Retry)
        self.assertEqual(outcome.after, 120.0)
        outcome, transport = self.handle(self.post(201))
        self.assertIsInstance(outcome, outbox.Retry)
        self.assertGreater(outcome.after, 100)
        self.assertEqual(transport.recorded, [], "nothing is sent while paused")

    def test_401_retries_after_the_pause_and_4xx_is_dead_with_a_code(self):
        from core import outbox

        outcome, _ = self.handle(self.post(401))
        self.assertIsInstance(outcome, outbox.Retry)
        self.assertEqual(outcome.error, "credentials_rejected")
        self.acct.paused_until = None
        self.acct.save()
        outcome, _ = self.handle(self.post(409))
        self.assertIsInstance(outcome, outbox.Dead)
        self.assertEqual(outcome.error, "http_409")

    def test_5xx_is_a_plain_retry_and_a_disabled_account_is_dead(self):
        from core import outbox

        outcome, _ = self.handle(self.post(502))
        self.assertIsInstance(outcome, outbox.Retry)
        self.assertIsNone(outcome.after)
        self.acct.enabled = False
        self.acct.save()
        outcome, transport = self.handle(self.post(201))
        self.assertEqual(outcome, outbox.Cancel("account_disabled"))
        self.assertEqual(transport.recorded, [])
