"""Connected accounts on /erp/integrations/ and the OAuth grant flow
(docs/crm/phase6-adapters.md 2.6-2.7; step 1 "done when").

Tenancy: every view works inside the caller's company, and another tenant's
account is a 404. OAuth: ``state`` is single use and bound to the session,
user and account; the grant is owned by the caller's profile;
``invalid_grant`` revokes one user's grant only.
"""

from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import Permission
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from core.integrations import accounts as services
from core.integrations.transport import use_transport
from core.models import IntegrationCheck, IntegrationCredential, UserProfile
from core.plugins.crm_adapter import GrantRevoked
from core.testing.integration_fixtures import FixtureTransport, fixture_secret
from core.tests.helpers import make_staff_user
from golden_erp.addons.fixture_crm.adapter import FixtureOAuthAdapter

from .test_integration_secrets import PLUGINS, IntegrationTestCase

SECRET = "fixture-view-secret-Zq9-never-shown"


def token_response(access="fixture-oauth-access-NEW", refresh="fixture-oauth-refresh-NEW", status=200,
                   payload=None):
    return {"schema_version": 1, "provider": "fixture_oauth",
            "request": {"method": "POST", "path": "/oauth/token"},
            "response": {"status": status},
            "payload": payload if payload is not None else
            {"access_token": access, "refresh_token": refresh, "expires_in": 3600, "scope": "messages.read"}}


def me_response(email="rep@example.com"):
    return {"schema_version": 1, "provider": "fixture_oauth", "request": {"method": "GET", "path": "/v1/me"},
            "payload": {"email": email}}


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class ViewTestCase(IntegrationTestCase):
    def staff(self, name, company=None, *, change=False):
        user = make_staff_user(name, password="pw")
        UserProfile.objects.create(user=user, company=company or self.company, display_name=name)
        if change:
            user.user_permissions.add(Permission.objects.get(codename="change_integrationaccount"))
        return user

    def login(self, user):
        self.client.force_login(user)


class ConnectedAccountsPageTests(ViewTestCase):
    def test_page_lists_only_the_callers_tenant_and_never_a_secret(self):
        mine = self.account(enabled=True)
        theirs = self.account(company=self.other)
        cred = services.set_credential(mine, "api_key", SECRET, subject="ops")
        services.set_credential(theirs, "api_key", SECRET + "-b")
        self.login(self.staff("ann", change=True))
        html = self.client.get(reverse("erp-integrations")).content.decode()
        self.assertIn(f'id="account-{mine.pk}"', html)
        self.assertNotIn(str(theirs.pk), html)
        self.assertIn('data-credential-kind="api_key"', html)
        self.assertNotIn(SECRET, html)
        self.assertNotIn(cred.ciphertext, html)
        self.assertNotIn(cred.key_id, html)

    def test_a_user_without_a_company_sees_no_accounts(self):
        self.account()
        user = make_staff_user("loner", password="pw")
        self.login(user)
        html = self.client.get(reverse("erp-integrations")).content.decode()
        self.assertIn("no accounts to show", html)

    def test_create_needs_the_change_permission_and_starts_disabled(self):
        url = reverse("erp-integration-account-create")
        self.login(self.staff("viewer"))
        self.assertEqual(self.client.post(url, {"provider": "fixture"}).status_code, 403)
        self.login(self.staff("admin", change=True))
        self.client.post(url, {"provider": "fixture"})
        account = self.company_accounts().get()
        self.assertEqual((account.provider, account.enabled), ("fixture", False))

    def company_accounts(self):
        from core.models import IntegrationAccount

        return IntegrationAccount.objects.filter(company=self.company)

    def test_credential_is_write_only_and_encrypted(self):
        account = self.account()
        self.login(self.staff("admin", change=True))
        response = self.client.post(reverse("erp-integration-account-credential", args=[account.pk]),
                                    {"kind": "api_key", "secret": SECRET}, follow=True)
        self.assertNotIn(SECRET, response.content.decode())
        cred = IntegrationCredential.objects.get(account=account)
        self.assertEqual(cred.reveal().get(), SECRET)
        # Grants are never typed in.
        self.client.post(reverse("erp-integration-account-credential", args=[account.pk]),
                         {"kind": "oauth_refresh", "secret": SECRET})
        self.assertFalse(IntegrationCredential.objects.filter(kind="oauth_refresh").exists())

    def test_another_tenants_account_is_404_on_every_write_view(self):
        theirs = self.account(company=self.other)
        self.login(self.staff("admin", change=True))
        for name, data in (("erp-integration-account-update", {"enabled": "1"}),
                           ("erp-integration-account-credential", {"kind": "api_key", "secret": SECRET}),
                           ("erp-integration-account-test", {}),
                           ("erp-integration-oauth-disconnect", {})):
            self.assertEqual(self.client.post(reverse(name, args=[theirs.pk]), data).status_code, 404, name)
        for name in ("erp-integration-oauth-connect", "erp-integration-oauth-callback"):
            self.assertEqual(self.client.get(reverse(name, args=[theirs.pk])).status_code, 404, name)
        theirs.refresh_from_db()
        self.assertFalse(theirs.enabled)
        self.assertFalse(IntegrationCredential.objects.filter(account=theirs).exists())

    def test_settings_are_validated_by_the_adapter(self):
        account = self.account()
        self.login(self.staff("admin", change=True))
        url = reverse("erp-integration-account-update", args=[account.pk])
        self.client.post(url, {"settings": json.dumps({"mailbox": "inbox", "nope": 1})})
        account.refresh_from_db()
        self.assertEqual(account.settings, {})
        self.client.post(url, {"settings": json.dumps({"mailbox": "inbox"}), "enabled": "1"})
        account.refresh_from_db()
        self.assertEqual((account.settings, account.enabled), ({"mailbox": "inbox"}, True))

    def test_connection_test_is_redacted_per_account_and_per_tenant(self):
        account = self.account(enabled=True)
        services.set_credential(account, "api_key", fixture_secret("api_key"))
        echo = {"schema_version": 1, "provider": "fixture", "request": {"method": "GET", "path": "/v1/me"},
                "payload": {"workspace": f"ws {fixture_secret('api_key')}"}}
        self.login(self.staff("ann", change=True))
        with use_transport(FixtureTransport(responses=[echo])):
            response = self.client.post(reverse("erp-integration-account-test", args=[account.pk]),
                                        HTTP_ACCEPT="application/json")
        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(fixture_secret("api_key"), body)
        self.assertIn("[redacted]", body)
        row = IntegrationCheck.objects.get()
        self.assertEqual((row.company_id, row.target), (self.company.pk, f"fixture:{account.pk}"))
        self.assertNotIn(fixture_secret("api_key"), json.dumps(row.observed) + row.what)
        # The other tenant's page shows no trace of it.
        self.login(self.staff("bob", company=self.other))
        self.assertNotIn(str(account.pk), self.client.get(reverse("erp-integrations")).content.decode())

    def test_running_a_connection_test_needs_the_change_permission(self):
        account = self.account(enabled=True)
        self.login(self.staff("viewer"))
        response = self.client.post(reverse("erp-integration-account-test", args=[account.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(IntegrationCheck.objects.exists())

    def test_a_failing_connection_test_never_echoes_the_secret(self):
        account = self.account(enabled=True)
        services.set_credential(account, "api_key", fixture_secret("api_key"))
        refused = {"schema_version": 1, "provider": "fixture", "request": {"method": "GET", "path": "/v1/me"},
                   "response": {"status": 401}, "payload": {"error": f"bad key {fixture_secret('api_key')}"}}
        self.login(self.staff("ann", change=True))
        with use_transport(FixtureTransport(responses=[refused])), self.assertLogs("core", "DEBUG") as logs:
            body = self.client.post(reverse("erp-integration-account-test", args=[account.pk]),
                                    HTTP_ACCEPT="application/json").content.decode()
        self.assertIn("credentials rejected", body)
        self.assertNotIn(fixture_secret("api_key"), body + "\n".join(logs.output))


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class OAuthFlowTests(ViewTestCase):
    def setUp(self):
        self.acct = self.account(provider="fixture_oauth", enabled=True)
        services.set_credential(self.acct, "oauth_client", fixture_secret("oauth_client"))
        self.user = self.staff("rep")
        self.login(self.user)

    def connect(self):
        response = self.client.get(reverse("erp-integration-oauth-connect", args=[self.acct.pk]))
        self.assertEqual(response.status_code, 302)
        return response["Location"]

    def callback(self, state, code="fixture-code-1", responses=None):
        transport = FixtureTransport(responses=responses if responses is not None else
                                     [token_response(), me_response()])
        with use_transport(transport):
            response = self.client.get(reverse("erp-integration-oauth-callback", args=[self.acct.pk]),
                                       {"state": state, "code": code})
        return response, transport

    def test_connect_starts_pkce_and_never_puts_the_client_secret_in_the_url(self):
        location = self.connect()
        query = parse_qs(urlsplit(location).query)
        self.assertTrue(location.startswith("https://auth.fixture.example.com/oauth/authorize?"))
        self.assertEqual(query["client_id"], ["fixture-client-id"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertTrue(query["state"][0] and query["code_challenge"][0])
        self.assertNotIn("fixture-client-secret-1", location)

    def test_callback_stores_a_grant_owned_by_the_caller(self):
        state = parse_qs(urlsplit(self.connect()).query)["state"][0]
        response, transport = self.callback(state)
        self.assertEqual(response.status_code, 302)
        profile = self.user.userprofile
        grants = IntegrationCredential.objects.filter(account=self.acct, owner=profile, revoked_at=None)
        self.assertEqual(sorted(grants.values_list("kind", flat=True)), ["oauth_access", "oauth_refresh"])
        self.assertEqual(set(grants.values_list("subject", flat=True)), {"rep@example.com"})
        self.assertEqual(grants.get(kind="oauth_refresh").reveal().get(), "fixture-oauth-refresh-NEW")
        token_call = transport.recorded[0]
        self.assertEqual(token_call["form"]["client_secret"], "<redacted:oauth_client>")
        self.assertEqual(token_call["form"]["grant_type"], "authorization_code")
        self.assertTrue(token_call["form"]["code_verifier"])
        self.assertEqual(transport.recorded[1]["headers"]["authorization"], "Bearer <redacted:oauth_access>")

    def test_state_reuse_is_refused(self):
        state = parse_qs(urlsplit(self.connect()).query)["state"][0]
        self.callback(state)
        before = IntegrationCredential.objects.count()
        response, transport = self.callback(state)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(transport.recorded, [], "a reused state never reaches the provider")
        self.assertEqual(IntegrationCredential.objects.count(), before)

    def test_unknown_or_crossed_state_is_refused(self):
        self.connect()
        _, transport = self.callback("forged-state")
        self.assertEqual(transport.recorded, [])
        state = parse_qs(urlsplit(self.connect()).query)["state"][0]
        self.login(self.staff("other_rep"))  # another user's session cannot complete it
        _, transport = self.callback(state)
        self.assertEqual(transport.recorded, [])
        self.assertFalse(IntegrationCredential.objects.filter(kind="oauth_refresh").exists())

    def test_a_stale_state_is_refused(self):
        state = parse_qs(urlsplit(self.connect()).query)["state"][0]
        session = self.client.session
        flows = session["golden_integration_oauth"]
        flows[state]["created"] -= 601
        session["golden_integration_oauth"] = flows
        session.save()
        _, transport = self.callback(state)
        self.assertEqual(transport.recorded, [])

    def test_an_other_tenant_profile_is_refused(self):
        state = parse_qs(urlsplit(self.connect()).query)["state"][0]
        UserProfile.objects.filter(user=self.user).update(company=self.other)
        response, transport = self.callback(state)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(transport.recorded, [])
        self.assertFalse(IntegrationCredential.objects.filter(kind="oauth_refresh").exists())

    def test_a_refused_code_exchange_stores_nothing_and_shows_no_secret(self):
        state = parse_qs(urlsplit(self.connect()).query)["state"][0]
        response, _ = self.callback(state, responses=[token_response(status=400, payload={"error": "invalid_grant"})])
        page = self.client.get(response["Location"]).content.decode()
        self.assertNotIn("fixture-client-secret-1", page)
        self.assertFalse(IntegrationCredential.objects.filter(kind="oauth_refresh").exists())

    def test_a_401_on_the_code_exchange_does_not_pause_the_account(self):
        state = parse_qs(urlsplit(self.connect()).query)["state"][0]
        self.callback(state, responses=[token_response(status=401, payload={"error": "invalid_client"})])
        self.acct.refresh_from_db()
        self.assertFalse(self.acct.is_paused())
        self.assertEqual(self.acct.last_error, "")

    def test_a_malformed_owner_on_disconnect_is_404(self):
        response = self.client.post(reverse("erp-integration-oauth-disconnect", args=[self.acct.pk]),
                                    {"owner": "not-a-uuid"})
        self.assertEqual(response.status_code, 404)

    def test_token_set_repr_hides_the_tokens(self):
        from core.plugins.crm_adapter import TokenSet

        text = repr(TokenSet(access_token="fixture-at-x", refresh_token="fixture-rt-x", raw={"a": "fixture-at-x"}))
        self.assertNotIn("fixture-at-x", text)
        self.assertNotIn("fixture-rt-x", text)

    def test_connect_needs_an_enabled_account(self):
        self.acct.enabled = False
        self.acct.save()
        response = self.client.get(reverse("erp-integration-oauth-connect", args=[self.acct.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("auth.fixture.example.com", response["Location"])


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class GrantRefreshTests(ViewTestCase):
    def setUp(self):
        self.acct = self.account(provider="fixture_oauth", enabled=True)
        services.set_credential(self.acct, "oauth_client", fixture_secret("oauth_client"))
        self.ann = self.staff("ann").userprofile
        self.bob = self.staff("bob").userprofile
        past = timezone.now() - timedelta(minutes=5)
        for who in (self.ann, self.bob):
            services.set_credential(self.acct, "oauth_refresh", f"fixture-rt-{who.display_name}", owner=who)
            services.set_credential(self.acct, "oauth_access", f"fixture-at-{who.display_name}", owner=who,
                                    expires_at=past)

    def test_a_live_access_token_is_used_as_is(self):
        IntegrationCredential.objects.filter(owner=self.ann, kind="oauth_access").update(
            expires_at=timezone.now() + timedelta(hours=1))
        transport = FixtureTransport(responses=[])
        token = FixtureOAuthAdapter(self.acct, transport).access_token(self.ann)
        self.assertEqual(token.get(), "fixture-at-ann")
        self.assertEqual(transport.recorded, [])

    def test_an_expired_token_is_refreshed_and_rotated(self):
        transport = FixtureTransport(responses=[token_response(access="fixture-at-2", refresh="")])
        token = FixtureOAuthAdapter(self.acct, transport).access_token(self.ann)
        self.assertEqual(token.get(), "fixture-at-2")
        form = transport.recorded[0]["form"]
        self.assertEqual((form["grant_type"], form["refresh_token"]), ("refresh_token", "<redacted:oauth_refresh>"))
        active = IntegrationCredential.objects.filter(owner=self.ann, revoked_at=None)
        self.assertEqual(active.get(kind="oauth_refresh").reveal().get(), "fixture-rt-ann", "kept when not rotated")
        self.assertEqual(active.get(kind="oauth_access").reveal().get(), "fixture-at-2")

    def test_a_refresh_keeps_the_grant_row_its_cursor_and_its_pause(self):
        grant = IntegrationCredential.objects.get(owner=self.ann, kind="oauth_refresh", revoked_at=None)
        IntegrationCredential.objects.filter(pk=grant.pk).update(sync_state={"cursor": {"after": "m9"}})
        transport = FixtureTransport(responses=[token_response(access="fixture-at-2", refresh="fixture-rt-ann")])
        FixtureOAuthAdapter(self.acct, transport).access_token(self.ann)
        same = IntegrationCredential.objects.get(owner=self.ann, kind="oauth_refresh", revoked_at=None)
        self.assertEqual(same.pk, grant.pk)
        self.assertEqual(same.sync_state, {"cursor": {"after": "m9"}})

    def test_a_rotated_refresh_token_carries_the_cursor_to_the_new_row(self):
        later = timezone.now() + timedelta(minutes=5)
        IntegrationCredential.objects.filter(owner=self.ann, kind="oauth_refresh").update(
            sync_state={"cursor": {"after": "m9"}, "initial_sync_done": True}, paused_until=later)
        transport = FixtureTransport(responses=[token_response(access="fixture-at-3", refresh="fixture-rt-3")])
        FixtureOAuthAdapter(self.acct, transport).access_token(self.ann)
        fresh = IntegrationCredential.objects.get(owner=self.ann, kind="oauth_refresh", revoked_at=None)
        self.assertEqual(fresh.reveal().get(), "fixture-rt-3")
        self.assertEqual(fresh.sync_state, {"cursor": {"after": "m9"}, "initial_sync_done": True})
        self.assertEqual(fresh.paused_until, later)

    def test_invalid_grant_revokes_that_users_grant_only(self):
        transport = FixtureTransport(responses=[token_response(status=400, payload={"error": "invalid_grant"})])
        with self.assertRaises(GrantRevoked), self.assertLogs("golden_erp.integrations", "WARNING") as logs:
            FixtureOAuthAdapter(self.acct, transport).access_token(self.ann)
        self.assertFalse(IntegrationCredential.objects.filter(owner=self.ann, revoked_at=None).exists())
        self.assertEqual(IntegrationCredential.objects.filter(owner=self.bob, revoked_at=None).count(), 2)
        self.assertNotIn("fixture-rt-ann", "\n".join(logs.output))
        self.acct.refresh_from_db()
        self.assertFalse(self.acct.is_paused(), "one user's revoked grant does not pause the account")
