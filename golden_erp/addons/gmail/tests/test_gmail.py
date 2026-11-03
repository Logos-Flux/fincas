"""Gmail adapter behaviour beyond the fixture contract (docs/crm/phase6-adapters.md 3.3, step 4).

The contract test (``test_contract.py``) replays every fixture case twice and
compares requests and rows byte for byte. These tests pin what the snapshot
does not show: drop counters, the raw envelope and body policy, who
performed an email, erasure tombstones, revoked grants, tenant isolation,
mailbox ownership on connect, and that no token or body reaches a log.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from communications.models import Communication, CommunicationParticipant, CommunicationRaw
from core.integrations import accounts as services
from core.integrations.transport import use_transport
from core.models import ExternalRef, IntegrationAccount, IntegrationCredential, OutboxEvent
from core.plugins.crm_adapter import AdapterError
from core.plugins.dispatch import suppressed
from core.testing.integration_fixtures import FixtureTransport, fixture_secret, run_case

from .. import mapping
from ..adapter import GmailAdapter, MailboxNotOwned
from .helpers import PLUGINS, GmailTestCase, message
from .seed import seed

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gmail"


@contextlib.contextmanager
def case(name):
    """Replay a fixture case (both runs); inspect inside the block, rolled back after it."""
    with transaction.atomic():
        yield run_case(GmailAdapter, FIXTURES / name, seed_hook=seed)
        transaction.set_rollback(True)


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class FixtureCaseDetailTests(TestCase):
    """Facts about a replayed case that ``db.expected.json`` does not carry."""

    def grant_stats(self, owner_email="rep@example.org"):
        cred = IntegrationCredential.objects.get(kind="oauth_refresh", revoked_at=None, subject=owner_email,
                                                 account__company__name="Fixture tenant")
        return cred.sync_state["cursor"]["stats"]

    def test_unknown_mail_is_dropped_counted_and_never_stored(self):
        with case("unknown-only-skipped"):
            self.assertEqual(self.grant_stats(), {"no_known_party": 4})
            self.assertFalse(Communication.objects.filter(provider="gmail").exists())
            self.assertFalse(CommunicationRaw.objects.exists())
            self.assertFalse(CommunicationParticipant.objects.exists())
            self.assertFalse(ExternalRef.objects.filter(source="gmail").exists())

    def test_drop_reasons_are_counted_per_grant(self):
        with case("excluded-label"):
            self.assertEqual(self.grant_stats(), {"excluded_label": 4, "fetched": 1})
        with case("golden-send-bcc-skipped"):
            self.assertEqual(self.grant_stats(), {"fetched": 2, "golden_send": 1})
        with case("erased-sender-skipped"):
            self.assertEqual(self.grant_stats(), {"erased_address": 1})

    def test_raw_envelope_is_the_message_minus_attachment_bodies(self):
        with case("inbound-known-contact"):
            comm = Communication.objects.get(provider="gmail")
            raw = CommunicationRaw.objects.get(communication=comm)
            self.assertEqual((raw.provider, raw.schema_version), ("gmail", 1))
            self.assertEqual(raw.payload["id"], "19b0000000000001")
            self.assertNotIn("mailbox", raw.payload)  # the routing wrapper is not stored
            attachment = raw.payload["payload"]["parts"][1]
            self.assertEqual(attachment["filename"], "site-plan.pdf")
            self.assertEqual(attachment["body"], {"attachmentId": "ANGjdJ-fixture-attachment-0001", "size": 51234})
            self.assertEqual(comm.body_text, "Hello,\ncould you send a quote for 40 panels?\nAda")
            self.assertEqual(comm.metadata, {"gmail": {"attachments": 1}})
            self.assertFalse(comm.is_editable)

    def test_outbound_is_performed_by_the_sender(self):
        with case("outbound-from-owner"):
            comm = Communication.objects.get(provider="gmail")
            self.assertEqual(comm.direction, "outbound")
            self.assertEqual(comm.performed_by.user.email, "rep@example.org")
        with case("thread-across-mailboxes"):
            reply = Communication.objects.get(provider="gmail", subject="Re: Site visit", direction="outbound")
            self.assertEqual(reply.performed_by.user.email, "boss@example.org")

    def test_html_only_body_becomes_text(self):
        with case("html-only-body"):
            self.assertEqual(Communication.objects.get(provider="gmail").body_text, "Hello Rep,\n\nSee the spec below.\nThanks")

    def test_a_revoked_grant_stops_with_a_clear_status(self):
        with case("oauth-invalid-grant-revokes"):
            account = IntegrationAccount.objects.get(company__name="Fixture tenant")
            rep = IntegrationCredential.objects.filter(account=account, subject="rep@example.org")
            self.assertFalse(rep.filter(revoked_at=None).exists())
            adapter = GmailAdapter(account, FixtureTransport())
            self.assertEqual(adapter.grant_status(rep.first().owner)["state"], "reconnect")
            boss = IntegrationCredential.objects.get(account=account, subject="boss@example.org", kind="oauth_refresh")
            self.assertEqual(adapter.grant_status(boss.owner)["state"], "active")
            self.assertEqual(account.last_error, "GrantRevoked")

    def test_first_sync_is_suppressed_and_then_polls_history(self):
        with case("first-sync-lists-recent"):
            cred = IntegrationCredential.objects.get(kind="oauth_refresh", revoked_at=None,
                                                     account__company__name="Fixture tenant")
            self.assertTrue(cred.sync_state["initial_sync_done"])
            self.assertEqual(cred.sync_state["cursor"]["history_id"], "1000")
            self.assertFalse(OutboxEvent.objects.exists())

    def test_no_token_body_or_address_reaches_a_log(self):
        with self.assertLogs("golden_erp", level="DEBUG") as logs:
            logging.getLogger("golden_erp").debug("start")
            for name in ("inbound-known-contact", "oauth-refresh", "oauth-invalid-grant-revokes",
                         "push-notification-polls", "push-wrong-audience", "unknown-only-skipped"):
                with case(name):
                    pass
        text = "\n".join(logs.output)
        for needle in ("fixture-access", "fixture-refresh", "fixture-client-secret", "fixture-webhook-secret",
                       "panels", "ada@example.com", "rep@example.org", "news@example.net", "Bearer ", "eyJ"):
            self.assertNotIn(needle, text)


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class IngestRuleTests(GmailTestCase):
    def test_cc_qualifies_and_bcc_never_does(self):
        cc = message(mid="<cc-1@example.org>", frm="rep@example.org", to=["stranger@example.net"],
                     cc=["ada@example.com"])
        self.assertTrue(self.ingest(cc).created)
        bcc = message(gid="b2", mid="<bcc-1@example.org>", frm="rep@example.org", to=["stranger@example.net"],
                      bcc=["ada@example.com", "grace@example.net"])
        outcome = self.ingest(bcc)
        self.assertEqual((outcome.changed, outcome.reason), (False, "no_known_party"))
        self.assertEqual(Communication.objects.filter(provider="gmail").count(), 1)

    def test_a_party_that_is_staff_or_shared_does_not_qualify(self):
        # A party holding a staff address, a match-N address, and the tenant's own party.
        self.person("Rep", "Self", "rep@example.org")
        self.person("Shared", "One", "team@example.com")
        self.person("Shared", "Two", "team@example.com")
        outcome = self.ingest(message(frm="team@example.com", to=["rep@example.org"]))
        self.assertEqual(outcome.reason, "no_known_party")
        from parties.models import Party

        self_party = self.person("Tenant", "Itself", "office@example.com")
        Party.objects.filter(pk=self_party.pk).update(is_self=True)
        self.assertEqual(self.ingest(message(gid="s2", mid="<s2@x>", frm="office@example.com")).reason,
                         "no_known_party")

    def test_another_tenants_party_is_unknown_here(self):
        self.person("Ada", "Elsewhere", "ada@example.org", company=self.other)
        self.assertEqual(self.ingest(message(frm="ada@example.org")).reason, "no_known_party")

    def test_reingest_is_a_no_op_and_a_new_copy_only_adds(self):
        first = self.ingest(message(frm="rep@example.org", to=["ada@example.com"]))
        self.assertTrue(first.created)
        again = self.ingest(message(frm="rep@example.org", to=["ada@example.com"]))
        self.assertEqual((again.changed, again.reason), (False, "duplicate"))
        with_bcc = message(gid="other-copy", frm="rep@example.org", to=["ada@example.com"], bcc=["boss@example.org"])
        self.assertTrue(self.ingest(with_bcc).changed)
        comm = Communication.objects.get(provider="gmail")
        roles = sorted(comm.participants.values_list("role", "address_normalized"))
        self.assertEqual(roles, [("bcc", "boss@example.org"), ("from", "rep@example.org"), ("to", "ada@example.com")])
        self.assertEqual(Communication.objects.filter(provider="gmail").count(), 1)

    def test_party_only_without_a_thread_engagement(self):
        from crm import engagements

        engagements.create(company=self.company, party=self.ada)  # Ada's one open deal: not guessed
        self.ingest(message())
        comm = Communication.objects.get(provider="gmail")
        self.assertEqual(comm.party_id, self.ada.pk)
        self.assertIsNone(comm.engagement_id)

    def test_first_sync_touches_the_thread_engagement(self):
        from communications.ingest import record
        from crm import engagements
        from crm.models import Engagement

        eng = engagements.create(company=self.company, party=self.ada)
        Engagement.objects.filter(pk=eng.pk).update(last_activity_at=None)
        root = message(mid="<root-1@example.com>")
        record(self.company, "email", direction="inbound", occurred_at=mapping.occurred_at(root), provider="manual",
               thread_key=mapping.thread_key(root, "rep@example.org"), engagement=eng, party=self.ada)
        reply = message(gid="r2", mid="<reply-1@example.com>", irt="<root-1@example.com>",
                        refs="<root-1@example.com>", at="2026-03-02T16:00:00+00:00")
        Engagement.objects.filter(pk=eng.pk).update(last_activity_at=None)
        with suppressed():
            self.ingest(reply)
        eng.refresh_from_db()
        comm = Communication.objects.get(provider="gmail")
        self.assertEqual(comm.engagement_id, eng.pk)
        self.assertEqual(eng.last_activity_at, comm.occurred_at)

    def test_a_message_without_message_id_is_keyed_per_mailbox(self):
        self.ingest(message(mid=""))
        ref = ExternalRef.objects.get(source="gmail")
        self.assertEqual(ref.source_id, f"gmail:{mapping.mailbox_hash('rep@example.org')}:19b00000000000f1")
        self.assertNotIn("rep@example.org", ref.source_id)

    def test_excluded_labels_follow_settings(self):
        IntegrationAccount.objects.filter(pk=self.account.pk).update(settings={"exclude_labels": ["Label_9"]})
        self.account.refresh_from_db()
        self.assertEqual(self.ingest(message(labels=("INBOX", "Label_9"))).reason, "excluded_label")
        self.assertTrue(self.ingest(message(gid="p2", mid="<p2@x>", labels=("CATEGORY_PROMOTIONS",))).created,
                        "a configured list replaces the default promotions exclusion")
        for label in ("DRAFT", "SPAM", "TRASH", "CHAT"):  # never configurable away
            self.assertEqual(self.ingest(message(gid=f"x-{label}", mid=f"<{label}@x>", labels=(label,))).reason,
                             "excluded_label")


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class ErasureTests(GmailTestCase):
    def test_erasure_scrubs_and_the_tombstone_holds_across_resync(self):
        from parties.erasure import erase_party

        msg = message(text="My home address is 1 Example Road")
        self.ingest(msg)
        comm = Communication.objects.get(provider="gmail")
        erase_party(self.ada, reason="data subject request")
        raw = CommunicationRaw.objects.get(communication=comm)
        self.assertEqual(raw.payload, {"erased": True})
        # The same message again (any mailbox), and new mail from the address: nothing written.
        self.assertEqual(self.ingest(msg).reason, "erased_address")
        later = message(gid="n2", mid="<new-after-erasure@example.com>", text="me again")
        self.assertEqual(self.ingest(later).reason, "erased_address")
        comm.refresh_from_db()
        self.assertEqual(comm.body_text, "")
        self.assertEqual(Communication.objects.filter(provider="gmail").count(), 1)

    def test_a_scrubbed_communication_is_never_rewritten(self):
        msg = message(frm="grace@example.net")
        self.ingest(msg)
        comm = Communication.objects.get(provider="gmail")
        CommunicationRaw.objects.filter(communication=comm).update(payload={"erased": True})
        copy = message(gid="copy", frm="grace@example.net", bcc=["boss@example.org"])
        outcome = self.ingest(copy)
        self.assertEqual((outcome.changed, outcome.reason), (False, "erased_communication"))
        self.assertFalse(comm.participants.filter(role="bcc").exists())

    def test_an_erased_ref_blocks_the_message_key(self):
        from django.contrib.contenttypes.models import ContentType

        msg = message(frm="grace@example.net")
        ExternalRef.objects.create(
            company=self.company, source="gmail", object_type="message",
            source_id=mapping.external_id(msg, "rep@example.org"), metadata={"erased": True},
            content_type=ContentType.objects.get_for_model(Communication), object_id=self.grace.pk)
        self.assertEqual(self.ingest(msg).reason, "erased_source")

    def test_the_provider_side_forget_operation_is_an_explicit_no_op(self):
        from core import outbox

        adapter = self.adapter()
        self.assertEqual(adapter.erasure_refs(object()), [])
        self.assertIsInstance(adapter.erase([]), outbox.Done)


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class GrantAndTenancyTests(GmailTestCase):
    def test_a_mailbox_without_an_active_grant_ingests_nothing(self):
        IntegrationCredential.objects.filter(account=self.account).update(revoked_at="2026-03-01T00:00:00Z")
        outcome = self.ingest(message())
        self.assertEqual((outcome.skip, outcome.reason), (True, "grant_revoked"))
        self.assertFalse(Communication.objects.filter(provider="gmail").exists())
        self.assertEqual(self.adapter().grant_status(self.rep)["state"], "reconnect")

    def test_a_grant_never_ingests_into_another_company(self):
        other_account = IntegrationAccount.objects.create(company=self.other, provider="gmail", enabled=True)
        self.person("Ada", "Other", "ada@example.com", company=self.other)
        # Tenant B's adapter is handed tenant A's mailbox: B holds no grant for it.
        outcome = self.ingest(message(), adapter=self.adapter(other_account))
        self.assertEqual(outcome.reason, "grant_revoked")
        # The owner moves company: the grant stops ingesting into A as well.
        from core.models import UserProfile

        UserProfile.objects.filter(pk=self.rep.pk).update(company=self.other)
        self.assertEqual(self.ingest(message()).reason, "grant_revoked")
        self.assertFalse(Communication.objects.filter(provider="gmail").exists())

    def test_poll_refuses_a_grant_whose_owner_left(self):
        from core.models import UserProfile

        grant = IntegrationCredential.objects.get(account=self.account, owner=self.rep, kind="oauth_refresh")
        UserProfile.objects.filter(pk=self.rep.pk).update(company=self.other)
        grant.refresh_from_db()
        transport = FixtureTransport()
        with self.assertRaises(AdapterError):
            GmailAdapter(self.account, transport).poll({"history_id": "1000"}, credential=grant)
        self.assertEqual(transport.recorded, [], "no provider call for a grant of another company")

    def test_a_notification_for_an_unknown_or_unsynced_mailbox_enqueues_nothing(self):
        from .test_pubsub import push_body

        adapter = self.adapter()
        self.assertEqual(self.ingest_push(adapter, push_body("nobody@example.org")).reason, "grant_revoked")
        IntegrationCredential.objects.filter(account=self.account).update(sync_state={})
        self.assertEqual(self.ingest_push(adapter, push_body("rep@example.org")).reason, "initial_sync_pending")
        self.assertFalse(OutboxEvent.objects.filter(consumer="gmail.poll").exists())

    def ingest_push(self, adapter, body):
        envelope = adapter.webhook_envelope({}, json.dumps(body).encode())
        return adapter.ingest(envelope)[0]


REVOKED = {"schema_version": 1, "provider": "gmail", "request": {"method": "POST", "path": "/revoke"}, "payload": {}}


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class ConnectTests(GmailTestCase):
    """The OAuth callback stores a grant only for the user's own mailbox."""

    def setUp(self):
        self.user = self.rep.user
        self.client.force_login(self.user)

    def callback(self, mailbox):
        response = self.client.get(reverse("erp-integration-oauth-connect", args=[self.account.pk]))
        self.assertEqual(response.status_code, 302)
        state = parse_qs(urlsplit(response["Location"]).query)["state"][0]
        self.assertIn("scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.readonly", response["Location"])
        transport = FixtureTransport(responses=[
            {"schema_version": 1, "provider": "gmail", "request": {"method": "POST", "path": "/token"},
             "payload": {"access_token": "fixture-access-new", "refresh_token": "fixture-refresh-new",
                         "expires_in": 3599, "scope": "https://www.googleapis.com/auth/gmail.readonly"}},
            {"schema_version": 1, "provider": "gmail",
             "request": {"method": "GET", "path": "/gmail/v1/users/me/profile"},
             "payload": {"emailAddress": mailbox, "historyId": "1"}},
            REVOKED,
        ])
        with use_transport(transport):
            response = self.client.get(reverse("erp-integration-oauth-callback", args=[self.account.pk]),
                                       {"state": state, "code": "fixture-code"})
        return response, transport

    def grants(self):
        return IntegrationCredential.objects.filter(account=self.account, owner=self.rep, revoked_at=None,
                                                    kind="oauth_refresh")

    def test_own_mailbox_is_stored_encrypted(self):
        IntegrationCredential.objects.filter(account=self.account, owner=self.rep).delete()
        response, transport = self.callback("Rep@Example.org")
        self.assertEqual(response.status_code, 302)
        grant = self.grants().get()
        self.assertEqual(grant.subject, "Rep@Example.org")
        self.assertNotIn("fixture-refresh-new", grant.ciphertext)
        self.assertEqual(transport.recorded[1]["headers"]["authorization"], "Bearer <redacted:oauth_access>")

    def test_someone_elses_mailbox_is_refused_and_nothing_stored(self):
        IntegrationCredential.objects.filter(account=self.account, owner=self.rep).delete()
        response, transport = self.callback("boss@example.org")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.grants().exists())
        revoke = transport.recorded[-1]
        self.assertEqual((revoke["path"], revoke["form"]), ("/revoke", {"token": "<redacted:oauth_refresh>"}),
                         "the refused grant is revoked at Google, not left live")
        page = self.client.get(response["Location"]).content.decode()
        self.assertIn("not yours", page)
        self.assertNotIn("fixture-refresh-new", page)

    def test_a_verified_login_email_counts_as_the_users(self):
        from core.identity.models import UserIdentity

        IntegrationCredential.objects.filter(account=self.account, owner=self.rep).delete()
        UserIdentity.objects.create(user=self.user, provider="google", subject="g-1", email="rep.alt@example.org",
                                    email_verified=True)
        self.callback("rep.alt@example.org")
        self.assertTrue(self.grants().exists())

    def test_store_grant_refuses_another_companys_profile(self):
        from core.plugins.crm_adapter import TokenSet

        outsider = self.staff("outsider", "outsider@example.org", company=self.other)
        adapter = GmailAdapter(self.account, FixtureTransport(responses=[REVOKED]))
        with self.assertRaises(MailboxNotOwned):
            adapter.store_grant(outsider, TokenSet(access_token="fixture-access-x", refresh_token="fixture-refresh-x"),
                                subject="outsider@example.org")
        self.assertFalse(IntegrationCredential.objects.filter(owner=outsider).exists())


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class SettingsTests(GmailTestCase):
    def test_validate_settings(self):
        adapter = self.adapter()
        self.assertEqual(adapter.validate_settings({"exclude_labels": ["SPAM"], "private_label": "x",
                                                    "pubsub_service_account": "push@example.com"}), [])
        errors = adapter.validate_settings({"exclude_labels": "SPAM", "private_label": "",
                                            "pubsub_service_account": "nope", "mailbox": 1})
        self.assertEqual(len(errors), 4)

    def test_settings_keys_and_credentials_are_declared(self):
        self.assertEqual(GmailAdapter.settings_keys, {"exclude_labels", "private_label", "pubsub_service_account"})
        self.assertEqual(GmailAdapter.credential_kinds,
                         {"oauth_client", "oauth_refresh", "oauth_access", "webhook_secret"})
        self.assertEqual(GmailAdapter.oauth_scopes, ("https://www.googleapis.com/auth/gmail.readonly",))

    def test_secrets_never_serialised(self):
        services.set_credential(self.account, "webhook_secret", "audience-Zq9-never-shown")
        dumped = json.dumps(list(IntegrationCredential.objects.values("subject", "scopes", "sync_state")))
        self.assertNotIn("audience-Zq9", dumped)
        self.assertNotIn(fixture_secret("oauth_refresh"), dumped)


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class ReviewHardeningTests(GmailTestCase):
    """Regression tests for the step 4 review (reused ids, rate limits, spoofed headers, push bodies)."""

    def test_a_reused_message_id_never_rewrites_stored_mail(self):
        self.ingest(message(subject="Original", text="original body"))
        forged = message(gid="f2", subject="Forged", text="forged body", cc=["grace@example.net"],
                         extra=[("Date", "Tue, 3 Mar 2026 09:00:00 +0000")])
        self.assertTrue(self.ingest(forged).created, "a different message keeps a key of its own")
        original = Communication.objects.get(provider="gmail", subject="Original")
        self.assertEqual(original.body_text, "original body")
        self.assertFalse(original.participants.filter(address_normalized="grace@example.net").exists())
        self.assertEqual(Communication.objects.filter(provider="gmail").count(), 2)

    def test_a_later_copy_adds_participants_and_nothing_else(self):
        self.ingest(message(text="first copy body"))
        copy_ = message(gid="c2", text="a different body in this copy", bcc=["boss@example.org"])
        outcome = self.ingest(copy_)
        self.assertTrue(outcome.changed)
        comm = Communication.objects.get(provider="gmail")
        self.assertEqual(comm.body_text, "first copy body")
        self.assertEqual(CommunicationRaw.objects.get(communication=comm).payload["id"], "19b00000000000f1")
        self.assertTrue(comm.participants.filter(role="bcc", address_normalized="boss@example.org").exists())

    def test_rate_limit_mid_page_keeps_the_progress(self):
        from unittest import mock

        from core.integrations import ratelimit

        msgs = [message(gid=f"g{i}", mid=f"<rl-{i}@example.com>") for i in range(3)]
        responses = [{"schema_version": 1, "provider": "gmail",
                      "request": {"method": "GET", "path": "/gmail/v1/users/me/labels"}, "payload": {"labels": []},
                      "repeatable": True}]
        for m in msgs:
            for fmt in ("metadata", "full"):
                responses.append({"schema_version": 1, "provider": "gmail", "payload": m, "repeatable": True,
                                  "request": {"method": "GET", "path": f"/gmail/v1/users/me/messages/{m['id']}",
                                              "query": {"format": fmt}}})
        grant = IntegrationCredential.objects.get(account=self.account, owner=self.rep, kind="oauth_refresh")
        transport = FixtureTransport(responses=responses)
        adapter = GmailAdapter(self.account, transport)
        budget = iter([0.0] * 4 + [0.5] * 50)  # four requests, then the bucket is empty
        with mock.patch.object(ratelimit, "take", lambda *a, **k: next(budget)):
            page = adapter.poll({"pending": ["g0", "g1", "g2"], "pending_more": False, "history_id": "7"},
                                credential=grant)
        self.assertEqual(len(page.items), 1)  # labels, g0 metadata + full, g1 metadata: then empty
        self.assertTrue(page.more)
        self.assertEqual(page.cursor["pending"], ["g1", "g2"])
        self.assertEqual(page.cursor["history_id"], "7")
        with mock.patch.object(ratelimit, "take", lambda *a, **k: 0.0):
            page = GmailAdapter(self.account, transport).poll(page.cursor, credential=grant)
        self.assertEqual([i["payload"]["message"]["id"] for i in page.items], ["g1", "g2"])
        self.assertNotIn("pending", page.cursor)
        self.assertFalse(page.more)
        self.assertFalse(any(r["path"].endswith("/history") for r in transport.recorded))

    def test_the_golden_header_is_honoured_only_for_this_company_and_its_recipient(self):
        import uuid as uuid_

        from communications.models import EmailLog

        log = EmailLog.objects.create(id=uuid_.uuid4(), document_type="quote", document_id=uuid_.uuid4(),
                                      document_number="Q-9", recipient_email="ada@example.com", subject="Q-9",
                                      template_name="quote_email", status="sent", triggered_by="manual")
        header = [("X-Golden-Email-Log", str(log.pk))]
        sent = message(frm="sales@example.org", to=["ada@example.com"], bcc=["rep@example.org"], extra=header)
        other_comm = Communication.objects.create(
            company=self.other, kind="email", direction="outbound", provider="golden", email_log=log,
            occurred_at=mapping.occurred_at(sent))
        self.assertTrue(self.ingest(sent).created, "another company's log hides nothing here")
        Communication.objects.filter(pk=other_comm.pk).update(company=self.company)
        sent2 = message(gid="s2", mid="<sent2@example.org>", frm="sales@example.org", to=["ada@example.com"],
                        extra=header)
        self.assertEqual(self.ingest(sent2).reason, "golden_send")

    def test_push_needs_a_notification_body_and_a_service_account(self):
        adapter = self.adapter()
        self.assertEqual(adapter.verify_webhook({}, b"{}").reason, "service_account_unset")
        IntegrationAccount.objects.filter(pk=self.account.pk).update(
            settings={"pubsub_service_account": "push@example.com"})
        self.account.refresh_from_db()
        body = json.dumps({"mailbox": "rep@example.org", "message": message()}).encode()
        self.assertEqual(self.adapter().verify_webhook({"authorization": "Bearer x.y.z"}, body).reason,
                         "not_a_notification")
        with self.assertRaises(ValueError):
            self.adapter().webhook_envelope({}, body)

    def test_thread_engagement_needs_a_shared_party(self):
        from communications.ingest import record
        from crm import engagements

        eng = engagements.create(company=self.company, party=self.grace)
        root = message(mid="<g-root@example.net>", frm="grace@example.net")
        record(self.company, "email", direction="inbound", occurred_at=mapping.occurred_at(root), provider="manual",
               thread_key=mapping.thread_key(root, "rep@example.org"), engagement=eng, party=self.grace)
        hijack = message(gid="h2", mid="<ada-h2@example.com>", refs="<g-root@example.net>")  # from Ada
        self.ingest(hijack)
        self.assertIsNone(Communication.objects.get(provider="gmail").engagement_id)

    def test_resync_reaches_back_to_the_last_good_history_read(self):
        from datetime import timedelta

        from django.utils import timezone

        adapter = self.adapter()
        synced = timezone.now() - timedelta(days=20)
        since = datetime_from(adapter._resync_since({"synced_at": synced.isoformat()}))
        self.assertLessEqual(since, synced - timedelta(days=1))
        floor = datetime_from(adapter._resync_since({"synced_at": "2000-01-01T00:00:00+00:00"}))
        self.assertGreaterEqual(floor, timezone.now() - timedelta(days=91))


def datetime_from(value):
    from datetime import datetime

    return datetime.fromisoformat(value)
