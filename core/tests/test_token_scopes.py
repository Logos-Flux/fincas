"""Pluggable bearer-token scopes (BasePlugin.get_token_scopes, docs/plugins.md).

Core ships one scope, "api" (full). A plugin adds narrowed scopes, each bound to
a path_prefix; core refuses any bearer whose scope is not registered, and
enforces each scope's path_prefix, check and aud rules.
"""

import secrets
import time

import jwt
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory

from core.authentication import (
    FULL_API_SCOPE,
    UNVERIFIED_SCOPE,
    BearerTokenAuthentication,
    get_jwt_signing_key,
    get_token_scopes,
    issue_api_jwt,
    token_scope,
)
from core.plugins import BasePlugin, TokenScope, plugin_registry
from core.service_accounts import legacy_key_fingerprint

API_KEY = "test-" + secrets.token_hex(16)


def _refuse_flagged(request, payload):
    if payload.get("flag") == "refuse":
        raise AuthenticationFailed("refused by scope check")
    if payload.get("flag") == "false":
        return False
    if "aud" in payload and payload["aud"] != "here":
        raise AuthenticationFailed("wrong destination")
    return True


def _returns_none(request, payload):
    return None


class AgentScopePlugin(BasePlugin):
    name = "agent_scope_plugin"

    def get_token_scopes(self):
        return [
            TokenScope(name="agent", path_prefix="/api/v1/agent/", description="Narrowed."),
            TokenScope(
                name="intake",
                path_prefix="/api/v1/intake/",
                max_lifetime_s=7200,
                claims=lambda: {"aud": "here"},
                check=_refuse_flagged,
                verifies_aud=True,
            ),
            TokenScope(name="checked", path_prefix="/api/v1/checked/", check=_refuse_flagged),
            TokenScope(name="sloppy", path_prefix="/api/v1/sloppy/", check=_returns_none),
            TokenScope(name="short", path_prefix="/api/v1/short/", max_lifetime_s=300),
            TokenScope(
                name="paired", path_prefix="/api/v1/paired/", max_lifetime_s=30 * 86400,
                check=_refuse_flagged,
            ),
        ]


class OtherAgentPlugin(BasePlugin):
    name = "other_agent_plugin"

    def get_token_scopes(self):
        return [TokenScope(name="agent", path_prefix="/api/v1/other/")]


class ShadowsCorePlugin(BasePlugin):
    name = "shadows_core_plugin"

    def get_token_scopes(self):
        return [TokenScope(name=FULL_API_SCOPE)]


def _plugin_returning(*scopes):
    class _P(BasePlugin):
        name = "bad_scope_plugin"

        def get_token_scopes(self):
            return list(scopes)

    return _P


def _bearer(path, token):
    return APIRequestFactory().get(path, HTTP_AUTHORIZATION=f"Bearer {token}")


def _signed(**claims):
    now = int(time.time())
    # akh: api-service bearers are bound to the current API_KEY (identity step 5).
    payload = {"iss": "golden-erp", "iat": now, "exp": now + 60, "sub": "api-service",
               "akh": legacy_key_fingerprint(), **claims}
    return jwt.encode(payload, get_jwt_signing_key(), algorithm="HS256")


class ScopeRegistrationTests(SimpleTestCase):
    def test_core_provides_only_the_full_scope(self):
        with override_settings(GOLDEN_PLUGINS=[]):
            self.assertEqual(set(get_token_scopes()), {FULL_API_SCOPE})

    def test_plugin_scopes_are_registered(self):
        with override_settings(GOLDEN_PLUGINS=[AgentScopePlugin]):
            self.assertEqual(
                set(get_token_scopes()),
                {FULL_API_SCOPE, "agent", "intake", "checked", "sloppy", "short", "paired"},
            )
            self.assertEqual(get_token_scopes()["intake"].path_prefix, "/api/v1/intake/")

    def test_duplicate_between_plugins_raises_naming_both(self):
        with override_settings(GOLDEN_PLUGINS=[AgentScopePlugin, OtherAgentPlugin]):
            with self.assertRaisesMessage(ImproperlyConfigured, "Duplicate token scope 'agent'") as ctx:
                plugin_registry.get_token_scopes()
            self.assertIn("agent_scope_plugin", str(ctx.exception))
            self.assertIn("other_agent_plugin", str(ctx.exception))

    def test_plugin_cannot_redefine_a_core_scope(self):
        with override_settings(GOLDEN_PLUGINS=[ShadowsCorePlugin]):
            with self.assertRaisesMessage(ImproperlyConfigured, "core and shadows_core_plugin"):
                plugin_registry.get_token_scopes()

    def test_malformed_scopes_raise(self):
        for bad in (
            "agent",  # not a TokenScope
            TokenScope(name="Has Space", path_prefix="/p/"),
            TokenScope(name="", path_prefix="/p/"),
            TokenScope(name="x"),  # no path_prefix: would be full scope everywhere
            TokenScope(name="x", path_prefix="api/v1/"),
            TokenScope(name="x", path_prefix="/"),
            TokenScope(name="x", path_prefix="/p/", max_lifetime_s=0),
            TokenScope(name="x", path_prefix="/p/", max_lifetime_s=30 * 86400 + 1),
            # Over 24 h: needs a check and a three-segment prefix.
            TokenScope(name="x", path_prefix="/api/v1/x/", max_lifetime_s=86401),
            TokenScope(name="x", path_prefix="/api/v1/", max_lifetime_s=86401, check=_refuse_flagged),
            TokenScope(name="x", path_prefix="/p/", check="not callable"),
            TokenScope(name="x", path_prefix="/p/", verifies_aud=True),  # no check
        ):
            with self.subTest(bad=bad):
                with override_settings(GOLDEN_PLUGINS=[_plugin_returning(bad)]):
                    with self.assertRaises(ImproperlyConfigured):
                        plugin_registry.get_token_scopes()


@override_settings(API_KEY=API_KEY, GOLDEN_PLUGINS=[AgentScopePlugin])
class ScopedBearerAuthTests(TestCase):
    def setUp(self):
        User.objects.exclude(username="api-service").delete()  # keep the migrated account
        self.auth = BearerTokenAuthentication()

    def test_default_token_carries_full_scope(self):
        token = issue_api_jwt()
        self.assertEqual(jwt.decode(token, get_jwt_signing_key(), algorithms=["HS256"])["scope"], FULL_API_SCOPE)
        user, _ = self.auth.authenticate(_bearer("/api/v1/products/", token))
        self.assertEqual(user.username, "api-service")

    def test_accepts_a_plugin_registered_scope(self):
        token = issue_api_jwt(scope="agent")
        request = _bearer("/api/v1/agent/packages/", token)
        user, returned = self.auth.authenticate(request)
        self.assertEqual(user.username, "api-service")
        self.assertEqual(returned, token)
        self.assertEqual(token_scope(request), "agent")

    def test_rejects_an_unknown_scope(self):
        token = _signed(scope="nonexistent")
        with self.assertRaisesMessage(AuthenticationFailed, "Unknown token scope"):
            self.auth.authenticate(_bearer("/api/v1/products/", token))

    def test_rejects_a_non_string_scope(self):
        token = _signed(scope=["api"])
        with self.assertRaisesMessage(AuthenticationFailed, "Unknown token scope"):
            self.auth.authenticate(_bearer("/api/v1/products/", token))
        self.assertEqual(token_scope(_bearer("/", token)), UNVERIFIED_SCOPE)

    def test_scope_of_an_uninstalled_plugin_stops_authenticating(self):
        token = issue_api_jwt(scope="agent")
        with override_settings(GOLDEN_PLUGINS=[]):
            with self.assertRaisesMessage(AuthenticationFailed, "Unknown token scope"):
                self.auth.authenticate(_bearer("/api/v1/agent/packages/", token))

    def test_a_non_full_scope_is_refused_on_core_endpoints(self):
        token = issue_api_jwt(scope="agent")
        for path in ("/api/v1/products/", "/api/v1/core/auth/token/", "/erp/", "/api/v1/agentx/"):
            with self.subTest(path=path):
                with self.assertRaisesMessage(AuthenticationFailed, "not valid on this path"):
                    self.auth.authenticate(_bearer(path, token))

    def test_a_thirty_day_token_is_still_bound_to_its_path(self):
        token = issue_api_jwt(scope="paired", lifetime_s=30 * 86400)
        request = _bearer("/api/v1/paired/intake/", token)
        self.assertEqual(self.auth.authenticate(request)[0].username, "api-service")
        self.assertEqual(token_scope(request), "paired")
        with self.assertRaisesMessage(AuthenticationFailed, "not valid on this path"):
            self.auth.authenticate(_bearer("/api/v1/products/", token))

    def test_query_string_slash_does_not_break_a_valid_path(self):
        token = issue_api_jwt(scope="agent")
        request = _bearer("/api/v1/agent/x/?next=%2Fa%2Fb", token)
        self.assertIsNotNone(self.auth.authenticate(request))
        self.assertEqual(token_scope(request), "agent")

    def test_legacy_token_without_scope_claim_is_full_scope(self):
        token = _signed()
        request = _bearer("/api/v1/products/", token)
        self.assertEqual(self.auth.authenticate(request)[0].username, "api-service")
        self.assertEqual(token_scope(request), FULL_API_SCOPE)

    def test_path_bound_scope_only_authenticates_under_its_prefix(self):
        token = issue_api_jwt(scope="intake")
        self.assertIsNotNone(self.auth.authenticate(_bearer("/api/v1/intake/captures/", token)))
        for path in (
            "/api/v1/products/",
            "/api/v1/intake/../products/",
            "/api/v1/intake/%2e%2e/products/",
            "/api/v1/intake/x%2F..%2F..%2Fproducts/",
        ):
            with self.subTest(path=path):
                with self.assertRaises(AuthenticationFailed):
                    self.auth.authenticate(_bearer(path, token))

    def test_scope_claims_and_check_run(self):
        token = issue_api_jwt(scope="intake")
        self.assertEqual(jwt.decode(token, get_jwt_signing_key(), algorithms=["HS256"], audience="here")["aud"], "here")
        refused = issue_api_jwt(scope="intake", extra_claims={"flag": "refuse"})
        with self.assertRaisesMessage(AuthenticationFailed, "refused by scope check"):
            self.auth.authenticate(_bearer("/api/v1/intake/x/", refused))
        elsewhere = _signed(scope="intake", aud="elsewhere")
        with self.assertRaisesMessage(AuthenticationFailed, "wrong destination"):
            self.auth.authenticate(_bearer("/api/v1/intake/x/", elsewhere))

    def test_audience_without_a_scope_check_is_refused(self):
        token = _signed(scope="agent", aud="here")
        with self.assertRaisesMessage(AuthenticationFailed, "audience"):
            self.auth.authenticate(_bearer("/api/v1/agent/x/", token))

    def test_a_check_that_does_not_declare_verifies_aud_does_not_admit_aud(self):
        token = _signed(scope="checked", aud="here")
        with self.assertRaisesMessage(AuthenticationFailed, "audience"):
            self.auth.authenticate(_bearer("/api/v1/checked/x/", token))
        self.assertIsNotNone(
            self.auth.authenticate(_bearer("/api/v1/checked/x/", _signed(scope="checked")))
        )

    def test_check_returning_false_or_none_refuses(self):
        refused = _signed(scope="checked", flag="false")
        with self.assertRaisesMessage(AuthenticationFailed, "refused by scope 'checked'"):
            self.auth.authenticate(_bearer("/api/v1/checked/x/", refused))
        with self.assertRaisesMessage(AuthenticationFailed, "refused by scope 'sloppy'"):
            self.auth.authenticate(_bearer("/api/v1/sloppy/x/", _signed(scope="sloppy")))

    def test_token_scope_fails_closed_for_unaccepted_bearers(self):
        garbage = _bearer("/api/v1/agent/x/", "garbage")
        self.assertEqual(token_scope(garbage), UNVERIFIED_SCOPE)
        unknown = _bearer("/api/v1/agent/x/", _signed(scope="nonexistent"))
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(unknown)
        self.assertEqual(token_scope(unknown), UNVERIFIED_SCOPE)
        # A valid bearer authentication never ran on is unverified too.
        self.assertEqual(token_scope(_bearer("/x", issue_api_jwt())), UNVERIFIED_SCOPE)
        self.assertEqual(token_scope(APIRequestFactory().get("/x")), FULL_API_SCOPE)

    def test_storefront_subject_cannot_carry_a_narrowed_scope(self):
        token = _signed(sub="storefront", scope="agent")
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(_bearer("/api/v1/agent/x/", token))


@override_settings(API_KEY=API_KEY, GOLDEN_PLUGINS=[AgentScopePlugin])
class IssueScopedJwtTests(SimpleTestCase):
    def test_unknown_scope_is_refused(self):
        with self.assertRaisesMessage(ValueError, "Unknown token scope"):
            issue_api_jwt(scope="nonexistent")

    def test_lifetime_is_capped_by_the_scope(self):
        with self.assertRaises(ValueError):
            issue_api_jwt(scope="intake", lifetime_s=7201)
        issue_api_jwt(scope="intake", lifetime_s=7200)

    def test_reserved_claims_cannot_be_overridden(self):
        with self.assertRaisesMessage(ValueError, "Reserved JWT claims"):
            issue_api_jwt(extra_claims={"sub": "admin"})

    def test_storefront_subject_cannot_take_a_narrowed_scope(self):
        with self.assertRaises(ValueError):
            issue_api_jwt(sub="storefront", scope="agent")


@override_settings(API_KEY=API_KEY, GOLDEN_PLUGINS=[AgentScopePlugin])
class TokenObtainScopeTests(TestCase):
    URL = "/api/v1/core/auth/token/"

    def _post(self, body):
        return self.client.post(
            self.URL, body, content_type="application/json", HTTP_AUTHORIZATION=f"Api-Key {API_KEY}"
        )

    def test_issues_a_registered_scope(self):
        response = self._post({"scope": "agent"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["scope"], "agent")
        payload = jwt.decode(response.json()["token"], get_jwt_signing_key(), algorithms=["HS256"])
        self.assertEqual(payload["scope"], "agent")

    def test_default_is_full_scope(self):
        self.assertEqual(self._post({}).json()["scope"], FULL_API_SCOPE)

    def test_unknown_scope_is_400_listing_the_registered_ones(self):
        response = self._post({"scope": "nonexistent"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("agent", response.json()["next_action"])

    def test_lifetime_clamps_to_the_scope_max(self):
        self.assertEqual(self._post({"scope": "intake", "lifetime_s": 10**9}).json()["expires_in_s"], 7200)
        self.assertEqual(self._post({"scope": "agent", "lifetime_s": 10**9}).json()["expires_in_s"], 86400)

    def test_a_path_bound_scope_may_allow_thirty_days(self):
        response = self._post({"scope": "paired", "lifetime_s": 30 * 86400})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["expires_in_s"], 30 * 86400)
        payload = jwt.decode(response.json()["token"], get_jwt_signing_key(), algorithms=["HS256"])
        self.assertEqual(payload["exp"] - payload["iat"], 30 * 86400)
        # Clamped to its own cap, never beyond.
        self.assertEqual(self._post({"scope": "paired", "lifetime_s": 10**9}).json()["expires_in_s"], 30 * 86400)

    def test_thirty_day_scope_keeps_the_one_hour_default(self):
        self.assertEqual(self._post({"scope": "paired"}).json()["expires_in_s"], 3600)

    def test_full_scope_stays_capped_at_a_day(self):
        self.assertEqual(self._post({"lifetime_s": 30 * 86400}).json()["expires_in_s"], 86400)
        with self.assertRaises(ValueError):
            issue_api_jwt(lifetime_s=86401)

    def test_default_lifetime_respects_a_short_scope_max(self):
        response = self._post({"scope": "short"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["expires_in_s"], 300)

    def test_non_string_scope_is_400(self):
        for bad in (["agent"], {"a": 1}, 7):
            with self.subTest(bad=bad):
                self.assertEqual(self._post({"scope": bad}).status_code, 400)

    def test_storefront_subject_with_a_narrowed_scope_is_400(self):
        self.assertEqual(self._post({"subject": "storefront", "scope": "agent"}).status_code, 400)
