"""Identity step 6: the generic OIDC provider (``core.identity.oidc``).

Against a stub issuer: a local RSA key whose JWKS is served through a
patched ``PyJWKClient.fetch_data``, a mocked discovery document
(``httpx.get``) and a mocked token endpoint (``httpx.post``).

- Registry: trust rules (D5) are refused at build time — an untrusted
  provider cannot serve ``erp``, cannot carry a ``role_map``, and no
  ``role_map`` reaches a service role.
- ``/erp/`` session login: Authorization Code + PKCE, state and nonce in the
  session; state/nonce/iss/aud/exp failures; linking by verified email only
  for a trusted provider and never to a superuser; ``role_map`` sync adds and
  removes roles with provenance; the start endpoint is throttled.
- Portal bearer path: issuer dispatch, the shared resolver, and the D5
  collision for an untrusted provider.
"""

import base64
import hashlib
import json
import time
import uuid
from unittest import mock
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.exceptions import AuthenticationFailed

from core.identity import oidc
from core.identity import providers as registry
from core.identity.models import UserIdentity
from core.identity.oidc import GRANTED_ROLES_KEY, OIDC, SESSION_KEY
from core.identity.providers import PROVIDERS, build_providers, provider_for_issuer
from core.identity.resolve import CODE_COLLISION, CODE_VERIFY_EMAIL, PortalAuthFailed, PortalRefused
from core.oidc_views import START_ATTEMPTS_PER_MINUTE
from core.tests.helpers import make_portal_customer, make_staff_user, portal_tenant
from core.tests.test_portal_auth import _authenticate

ISSUER = "https://idp.example.test/application/o/golden/"
CLIENT_ID = "golden-client"
AUTHORIZE = "https://idp.example.test/application/o/authorize/"
TOKEN = "https://idp.example.test/application/o/token/"
JWKS = "https://idp.example.test/application/o/golden/jwks/"
DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": AUTHORIZE,
    "token_endpoint": TOKEN,
    "jwks_uri": JWKS,
}
MANAGER = {"name": "manager", "label": "Manager", "staff": True}
VIP = {"name": "vip_customer", "label": "VIP customer", "portal": True}


def oidc_provider(name="idp", options=None, **overrides):
    """An OIDC entry for both entry points (§6.2's shape), with overrides."""
    entry = {
        "name": name,
        "backend": "core.identity.providers.OIDC",
        "entry_points": ["erp", "portal"],
        "trusted": True,
        "self_register": True,
        "roles_on_create": ["customer"],
        "link_by_verified_email": True,
        "options": {
            "issuer": ISSUER,
            "client_id": CLIENT_ID,
            "client_secret_setting": "OIDC_TEST_CLIENT_SECRET",
            "scopes": ["openid", "email", "profile", "groups"],
            "role_map": {"golden-staff": "staff", "golden-managers": "manager"},
            "label": "Example SSO",
            "erp_link_by_email": True,
        },
    }
    entry["options"].update(options or {})
    entry.update(overrides)
    return entry


def _build(entries, roles=()):
    with override_settings(GOLDEN_ROLES=list(roles)), \
            override_settings(GOLDEN_IDENTITY_PROVIDERS=entries):
        return build_providers()


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class StubIssuer:
    """A local OpenID provider: RSA key + JWKS, discovery, token endpoint."""

    def __init__(self, testcase):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({"kid": "stub-1", "alg": "RS256", "use": "sig"})
        self.jwks = {"keys": [jwk]}
        self.discovery = dict(DISCOVERY)
        self.next_id_token = None
        self.token_status = 200
        self.token_requests = []
        self.issued_codes = {}
        registry._jwks_clients.clear()
        oidc.clear_caches()
        jwks = self.jwks

        def fetch_data(client):
            client.jwk_set_cache.put(jwks)
            return jwks

        def get(url, **kwargs):
            self.discovery_calls = getattr(self, "discovery_calls", 0) + 1
            assert url == ISSUER.rstrip("/") + "/.well-known/openid-configuration", url
            return mock.Mock(status_code=200, json=mock.Mock(return_value=self.discovery))

        def post(url, data=None, auth=None, **kwargs):
            assert url == TOKEN, url
            data = dict(data or {})
            self.token_requests.append({"data": data, "auth": auth})
            status = self.token_status
            grant = self.issued_codes.pop(data.get("code"), None)
            # A real issuer binds the code to the authorization request: the
            # PKCE verifier must hash to its challenge, redirect_uri must match.
            if grant is None or data.get("redirect_uri") != grant["redirect_uri"] or \
                    _s256(data.get("code_verifier", "")) != grant["challenge"]:
                status = 400
            body = ({"access_token": "opaque", "token_type": "Bearer",
                     "id_token": self.next_id_token} if status == 200
                    else {"error": "invalid_grant"})
            return mock.Mock(status_code=status, text=json.dumps(body),
                             json=mock.Mock(return_value=body))

        for patcher in (
            mock.patch.object(jwt.PyJWKClient, "fetch_data", autospec=True,
                              side_effect=fetch_data),
            mock.patch("httpx.get", side_effect=get),
            mock.patch("httpx.post", side_effect=post),
        ):
            patcher.start()
            testcase.addCleanup(patcher.stop)
        testcase.addCleanup(registry._jwks_clients.clear)
        testcase.addCleanup(oidc.clear_caches)

    def authorize(self, query, code="the-code"):
        """What the issuer's authorize endpoint does: bind a code to the request."""
        self.issued_codes[code] = {"challenge": query["code_challenge"],
                                   "redirect_uri": query["redirect_uri"]}
        return code

    def token(self, *, key=None, kid="stub-1", alg="RS256", **claims):
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300,
                   "sub": str(uuid.uuid4()), **claims}
        payload = {k: v for k, v in payload.items() if v is not None}
        return jwt.encode(payload, key or self.key, algorithm=alg, headers={"kid": kid})


@override_settings(OIDC_TEST_CLIENT_SECRET="s3cret-for-tests")
class _OIDCBase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        portal_tenant()  # Phase 2 step 7: the portal resolves inside one tenant
        self.issuer = StubIssuer(self)

    def start(self, next_url=None):
        url = "/erp/oidc/idp/start/"
        response = self.client.get(url, {"next": next_url} if next_url else {})
        self.assertEqual(response.status_code, 302, getattr(response, "content", b"")[:300])
        query = parse_qs(urlparse(response["Location"]).query)
        return response, {k: v[0] for k, v in query.items()}

    def flow(self, state):
        return self.client.session[SESSION_KEY][state]

    def login(self, *, next_url=None, nonce=None, state=None, **claims):
        """Start, have the stub issue an ID token for the flow, call back."""
        _, query = self.start(next_url)
        flow = self.flow(query["state"])
        self.issuer.next_id_token = self.issuer.token(
            nonce=flow["nonce"] if nonce is None else nonce, **claims,
        )
        code = self.issuer.authorize(query)
        return self.client.get("/erp/oidc/idp/callback/",
                               {"state": state or query["state"], "code": code})

    def assertLoggedInAs(self, response, user, location="/erp/"):
        self.assertEqual(response.status_code, 302, response.content[:500])
        self.assertEqual(response["Location"], location)
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def assertRefused(self, response, status, fragment=""):
        self.assertEqual(response.status_code, status, response.content[:500])
        self.assertNotIn("_auth_user_id", self.client.session)
        if fragment:
            self.assertContains(response, fragment, status_code=status)


# ── registry ────────────────────────────────────────────────────────────────


class RegistryTests(SimpleTestCase):
    def assertRefused(self, entries, fragment, roles=()):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            _build(entries, roles)
        self.assertIn(fragment, str(ctx.exception))

    def test_valid_entry_builds_under_both_dotted_paths(self):
        built = _build([oidc_provider()], [MANAGER])
        self.assertIsInstance(built["idp"], OIDC)
        built = _build([oidc_provider(backend="core.identity.oidc.OIDC")], [MANAGER])
        self.assertIsInstance(built["idp"], OIDC)

    def test_untrusted_provider_cannot_map_to_staff(self):
        self.assertRefused(
            [oidc_provider(trusted=False, entry_points=["portal"],
                           options={"role_map": {"golden-staff": "staff"}})],
            "role_map needs trusted=True",
        )

    def test_untrusted_provider_cannot_map_any_role(self):
        self.assertRefused(
            [oidc_provider(trusted=False, entry_points=["portal"],
                           options={"role_map": {"vips": "vip_customer"}})],
            "role_map needs trusted=True", roles=[VIP],
        )

    def test_untrusted_provider_cannot_serve_erp(self):
        self.assertRefused(
            [oidc_provider(trusted=False, options={"role_map": {}})],
            "'erp' entry point needs trusted=True",
        )

    def test_role_map_never_reaches_a_service_role(self):
        self.assertRefused([oidc_provider(options={"role_map": {"bots": "storefront"}})],
                           "service role")

    def test_staff_role_map_needs_the_erp_entry_point(self):
        self.assertRefused([oidc_provider(entry_points=["portal"])],
                           "only with the 'erp' entry point", roles=[MANAGER])
        # A portal-only trusted provider may still map portal roles.
        built = _build([oidc_provider(entry_points=["portal"],
                                      options={"role_map": {"vips": "vip_customer"}})], [VIP])
        self.assertIsInstance(built["idp"], OIDC)

    def test_duplicate_portal_issuer_refused(self):
        self.assertRefused(
            [oidc_provider(), oidc_provider(name="idp2", entry_points=["portal"],
                                            trusted=False, options={"role_map": {}})],
            "share the issuer", roles=[MANAGER],
        )

    def test_role_map_unknown_role_refused(self):
        self.assertRefused([oidc_provider()], "unknown role 'manager'")

    def test_client_secret_inline_refused(self):
        entry = oidc_provider()
        entry["options"].pop("client_secret_setting")
        entry["options"]["client_secret"] = "inline"
        self.assertRefused([entry], "client_secret is a secret")

    def test_issuer_and_client_id_required(self):
        for missing in ("issuer", "client_id"):
            entry = oidc_provider()
            entry["options"].pop(missing)
            self.assertRefused([entry], f"needs the {missing!r} option", roles=[MANAGER])

    def test_scopes_must_include_openid(self):
        self.assertRefused([oidc_provider(options={"scopes": ["email"]})], "'openid'",
                           roles=[MANAGER])

    def test_structured_options_cannot_be_indirect(self):
        entry = oidc_provider()
        entry["options"].pop("role_map")
        entry["options"]["role_map_setting"] = "SOME_SETTING"
        self.assertRefused([entry], "must be written inline")

    def test_unknown_claim_key_refused(self):
        self.assertRefused([oidc_provider(options={"claims": {"roles": "x"}})],
                           "claims must map", roles=[MANAGER])

    def test_portal_dispatch_on_issuer_beside_supabase(self):
        built = _build([*_supabase(), oidc_provider()], [MANAGER])
        with mock.patch.dict(PROVIDERS, built, clear=True):
            self.assertIs(provider_for_issuer(ISSUER), built["idp"])
            self.assertIsNone(provider_for_issuer("https://elsewhere.example/"))


def _without_client_id(entry):
    entry["options"].pop("client_id")
    return entry


def _supabase():
    from core.tests.test_portal_auth import supabase_provider

    return [supabase_provider()]


# ── /erp/ session login ─────────────────────────────────────────────────────

PROVIDERS_SETTING = {"GOLDEN_ROLES": [MANAGER], "GOLDEN_IDENTITY_PROVIDERS": [oidc_provider()]}


@override_settings(**PROVIDERS_SETTING)
class SessionLoginTests(_OIDCBase):
    def test_login_page_shows_a_button_per_erp_provider(self):
        response = self.client.get("/erp/login/")
        self.assertContains(response, "Sign in with Example SSO")
        self.assertContains(response, "/erp/oidc/idp/start/")

    @override_settings(GOLDEN_IDENTITY_PROVIDERS=[])
    def test_no_button_without_an_erp_provider(self):
        self.assertNotContains(self.client.get("/erp/login/"), "Sign in with")

    def test_start_redirects_with_pkce_state_and_nonce(self):
        _, query = self.start()
        self.assertEqual(query["response_type"], "code")
        self.assertEqual(query["client_id"], CLIENT_ID)
        self.assertEqual(query["scope"], "openid email profile groups")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertTrue(query["redirect_uri"].endswith("/erp/oidc/idp/callback/"))
        flow = self.flow(query["state"])
        self.assertEqual(flow["nonce"], query["nonce"])
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(flow["verifier"].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(query["code_challenge"], challenge)

    def test_happy_path_provisions_staff_from_role_map(self):
        sub = str(uuid.uuid4())
        response = self.login(sub=sub, email="Ada@Example.com", email_verified=True,
                              name="Ada Lovelace", groups=["golden-staff", "other"],
                              next_url="/erp/orders/")
        identity = UserIdentity.objects.get(provider="idp", subject=sub)
        user = identity.user
        self.assertLoggedInAs(response, user, "/erp/orders/")
        self.assertTrue(user.username.startswith("u-"))
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.email, "ada@example.com")
        self.assertEqual((user.first_name, user.last_name), ("Ada", "Lovelace"))
        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(identity.metadata[GRANTED_ROLES_KEY], ["staff"])
        self.assertIsNotNone(identity.last_login_at)
        # The token request carried the PKCE verifier and client credentials.
        sent = self.issuer.token_requests[-1]
        self.assertEqual(sent["data"]["grant_type"], "authorization_code")
        self.assertEqual(sent["data"]["code"], "the-code")
        self.assertTrue(sent["data"]["code_verifier"])
        self.assertEqual(sent["auth"], (CLIENT_ID, "s3cret-for-tests"))
        self.assertEqual(self.client.get("/erp/").status_code, 200)

    def test_second_login_reuses_the_identity(self):
        sub = str(uuid.uuid4())
        self.login(sub=sub, email="b@example.com", email_verified=True, groups=["golden-staff"])
        self.client.logout()
        response = self.login(sub=sub, email="b@example.com", email_verified=True,
                              groups=["golden-staff"])
        self.assertLoggedInAs(response, UserIdentity.objects.get(subject=sub).user)
        self.assertEqual(UserIdentity.objects.filter(provider="idp").count(), 1)

    def test_state_mismatch_refused(self):
        response = self.login(state="forged-state", email_verified=True,
                              groups=["golden-staff"])
        self.assertRefused(response, 400, "expired or was started elsewhere")
        self.assertEqual(self.issuer.token_requests, [])
        self.assertFalse(UserIdentity.objects.exists())

    def test_callback_without_start_refused(self):
        response = self.client.get("/erp/oidc/idp/callback/", {"state": "x", "code": "c"})
        self.assertRefused(response, 400)

    def test_state_is_single_use(self):
        _, query = self.start()
        flow = self.flow(query["state"])
        self.issuer.next_id_token = self.issuer.token(nonce=flow["nonce"], groups=["golden-staff"])
        params = {"state": query["state"], "code": self.issuer.authorize(query, "c")}
        self.client.get("/erp/oidc/idp/callback/", params)
        self.client.logout()
        self.assertRefused(self.client.get("/erp/oidc/idp/callback/", params), 400)

    def test_nonce_mismatch_refused(self):
        response = self.login(nonce="another-nonce", groups=["golden-staff"])
        self.assertRefused(response, 403, "could not be verified")
        self.assertFalse(UserIdentity.objects.exists())

    def test_expired_id_token_refused(self):
        now = int(time.time())
        response = self.login(iat=now - 7200, exp=now - 3600, groups=["golden-staff"])
        self.assertRefused(response, 403, "could not be verified")

    def test_issuer_mismatch_refused(self):
        response = self.login(iss="https://evil.example/", groups=["golden-staff"])
        self.assertRefused(response, 403, "could not be verified")

    def test_audience_mismatch_refused(self):
        response = self.login(aud="another-client", groups=["golden-staff"])
        self.assertRefused(response, 403, "could not be verified")

    def test_token_signed_by_another_key_refused(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _, query = self.start()
        flow = self.flow(query["state"])
        self.issuer.next_id_token = self.issuer.token(key=other, nonce=flow["nonce"],
                                                      groups=["golden-staff"])
        response = self.client.get("/erp/oidc/idp/callback/",
                                   {"state": query["state"],
                                    "code": self.issuer.authorize(query, "c")})
        self.assertRefused(response, 403)

    def test_hs256_token_refused(self):
        _, query = self.start()
        flow = self.flow(query["state"])
        self.issuer.next_id_token = jwt.encode(
            {"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "iat": int(time.time()),
             "exp": int(time.time()) + 60, "nonce": flow["nonce"]},
            "s3cret-for-tests-and-long-enough-for-hmac", algorithm="HS256")
        response = self.client.get("/erp/oidc/idp/callback/",
                                   {"state": query["state"],
                                    "code": self.issuer.authorize(query, "c")})
        self.assertRefused(response, 403)

    def test_provider_error_refused(self):
        _, query = self.start()
        response = self.client.get("/erp/oidc/idp/callback/",
                                   {"state": query["state"], "error": "access_denied"})
        self.assertRefused(response, 403, "canceled")

    def test_token_endpoint_failure_refused(self):
        self.issuer.token_status = 400
        response = self.login(groups=["golden-staff"])
        self.assertRefused(response, 403, "could not be reached")

    def test_discovery_issuer_mismatch_refused(self):
        self.issuer.discovery["issuer"] = "https://evil.example/"
        response = self.client.get("/erp/oidc/idp/start/")
        self.assertRefused(response, 502)

    def test_failed_discovery_fails_fast(self):
        self.issuer.discovery["issuer"] = "https://evil.example/"
        self.client.get("/erp/oidc/idp/start/")
        self.client.get("/erp/oidc/idp/start/")
        self.assertEqual(self.issuer.discovery_calls, 1)

    def test_next_must_stay_on_this_site(self):
        response = self.login(next_url="https://evil.example/", email_verified=True,
                              groups=["golden-staff"])
        self.assertEqual(response["Location"], "/erp/")
        self.client.logout()
        response = self.login(next_url="//evil.example/x", groups=["golden-staff"])
        self.assertEqual(response["Location"], "/erp/")
        self.client.logout()
        response = self.login(next_url="/erp/customers/?page=2", groups=["golden-staff"])
        self.assertEqual(response["Location"], "/erp/customers/?page=2")

    def test_wrong_pkce_verifier_refused(self):
        _, query = self.start()
        session = self.client.session
        flows = session[SESSION_KEY]
        flows[query["state"]]["verifier"] = "not-the-verifier"
        session[SESSION_KEY] = flows
        session.save()
        self.issuer.next_id_token = self.issuer.token(
            nonce=flows[query["state"]]["nonce"], groups=["golden-staff"])
        response = self.client.get("/erp/oidc/idp/callback/",
                                   {"state": query["state"],
                                    "code": self.issuer.authorize(query)})
        self.assertRefused(response, 403, "could not be reached")

    def test_code_from_another_request_refused(self):
        response = self.client.get("/erp/oidc/idp/start/")
        query = {k: v[0] for k, v in parse_qs(urlparse(response["Location"]).query).items()}
        flow = self.flow(query["state"])
        self.issuer.next_id_token = self.issuer.token(nonce=flow["nonce"],
                                                      groups=["golden-staff"])
        self.issuer.authorize(query, "other-code")
        response = self.client.get("/erp/oidc/idp/callback/",
                                   {"state": query["state"], "code": "forged-code"})
        self.assertRefused(response, 403)

    def test_no_mapped_staff_role_creates_nothing(self):
        response = self.login(email="nobody@example.com", email_verified=True,
                              groups=["unrelated"])
        self.assertRefused(response, 403, "no back-office account")
        self.assertFalse(User.objects.filter(email="nobody@example.com").exists())

    def test_unknown_provider_404(self):
        self.assertEqual(self.client.get("/erp/oidc/nope/start/").status_code, 404)
        self.assertEqual(self.client.get("/erp/oidc/password/start/").status_code, 404)

    @override_settings(GOLDEN_IDENTITY_PROVIDERS=[oidc_provider(
        entry_points=["portal"], options={"role_map": {}})])
    def test_portal_only_provider_has_no_session_login(self):
        self.assertEqual(self.client.get("/erp/oidc/idp/start/").status_code, 404)

    def test_start_is_throttled(self):
        for _ in range(START_ATTEMPTS_PER_MINUTE):
            self.assertEqual(self.client.get("/erp/oidc/idp/start/").status_code, 302)
        self.assertEqual(self.client.get("/erp/oidc/idp/start/").status_code, 429)

    def test_signed_in_customer_can_still_start(self):
        customer, _ = make_portal_customer("c@example.com")
        self.client.force_login(customer.user)
        self.assertEqual(self.client.get("/erp/oidc/idp/start/").status_code, 302)


@override_settings(**PROVIDERS_SETTING)
class LinkByEmailTests(_OIDCBase):
    def test_trusted_provider_links_existing_staff_by_verified_email(self):
        staff = make_staff_user("grace", "grace@example.com", "pw")
        sub = str(uuid.uuid4())
        response = self.login(sub=sub, email="GRACE@example.com", email_verified=True)
        self.assertLoggedInAs(response, staff)
        self.assertEqual(UserIdentity.objects.get(provider="idp", subject=sub).user, staff)
        staff.refresh_from_db()
        self.assertTrue(staff.has_usable_password())  # the password login still works

    @override_settings(GOLDEN_IDENTITY_PROVIDERS=[oidc_provider(
        options={"erp_link_by_email": False})])
    def test_email_link_is_opt_in(self):
        make_staff_user("grace", "grace@example.com", "pw")
        response = self.login(email="grace@example.com", email_verified=True,
                              groups=["golden-staff"])
        self.assertRefused(response, 403, "Ask an administrator to link")
        self.assertFalse(UserIdentity.objects.exists())

    def test_service_account_never_linked(self):
        from core.roles import grant_role

        bot = User.objects.create_user("bot", "bot@example.com")
        grant_role(bot, "storefront")
        response = self.login(email="bot@example.com", email_verified=True,
                              groups=["golden-staff"])
        self.assertRefused(response, 403, "cannot sign in with this provider")
        bot.refresh_from_db()
        self.assertFalse(bot.is_staff)
        self.assertFalse(UserIdentity.objects.exists())

    def test_superuser_with_an_identity_still_refused(self):
        # The portal resolver can link a trusted provider's subject to a
        # superuser's Customer; that row must not open an /erp/ session.
        root = User.objects.create_superuser("root", "root@example.com", "pw")
        UserIdentity.objects.create(user=root, provider="idp", subject="s-root")
        response = self.login(sub="s-root", groups=["golden-staff"])
        self.assertRefused(response, 403, "cannot sign in with this provider")

    def test_inactive_user_never_linked_by_email(self):
        make_staff_user("old", "old@example.com", is_active=False)
        response = self.login(email="old@example.com", email_verified=True)
        self.assertRefused(response, 403, "disabled")
        self.assertFalse(UserIdentity.objects.exists())

    def test_unverified_email_never_links(self):
        make_staff_user("grace", "grace@example.com", "pw")
        for verified in (False, None, "true"):
            response = self.login(email="grace@example.com", email_verified=verified,
                                  groups=["golden-staff"])
            self.assertRefused(response, 403, "has not verified")
        self.assertFalse(UserIdentity.objects.exists())
        self.assertEqual(User.objects.filter(email__iexact="grace@example.com").count(), 1)

    def test_superuser_never_linked_by_email(self):
        User.objects.create_superuser("root", "root@example.com", "pw")
        response = self.login(email="root@example.com", email_verified=True,
                              groups=["golden-staff"])
        self.assertRefused(response, 403, "cannot sign in with this provider")
        self.assertFalse(UserIdentity.objects.exists())

    def test_two_users_with_the_email_refused(self):
        make_staff_user("a1", "dup@example.com")
        make_staff_user("a2", "DUP@example.com")
        response = self.login(email="dup@example.com", email_verified=True)
        self.assertRefused(response, 403, "Several accounts")

    def test_user_holding_another_subject_refused(self):
        staff = make_staff_user("grace", "grace@example.com")
        UserIdentity.objects.create(user=staff, provider="idp", subject="older")
        response = self.login(email="grace@example.com", email_verified=True)
        self.assertRefused(response, 403, "different sign-in")

    def test_linked_customer_without_staff_role_gets_no_session(self):
        customer, _ = make_portal_customer("shopper@example.com")
        response = self.login(email="shopper@example.com", email_verified=True,
                              groups=["unrelated"])
        self.assertRefused(response, 403, "no back-office access")
        self.assertFalse(customer.user.is_staff)

    def test_inactive_user_refused(self):
        staff = make_staff_user("gone", "gone@example.com", is_active=False)
        UserIdentity.objects.create(user=staff, provider="idp", subject="s-gone")
        response = self.login(sub="s-gone", groups=["golden-staff"])
        self.assertRefused(response, 403, "disabled")


@override_settings(**PROVIDERS_SETTING)
class RoleSyncTests(_OIDCBase):
    def _identity(self, sub):
        return UserIdentity.objects.select_related("user").get(provider="idp", subject=sub)

    def test_roles_added_and_removed_with_the_groups(self):
        sub = str(uuid.uuid4())
        self.login(sub=sub, groups=["golden-staff", "golden-managers"])
        identity = self._identity(sub)
        user = identity.user
        self.assertEqual(set(user.groups.values_list("name", flat=True)), {"staff", "manager"})
        self.assertEqual(identity.metadata[GRANTED_ROLES_KEY], ["manager", "staff"])

        self.client.logout()
        response = self.login(sub=sub, groups=["golden-staff"])
        self.assertLoggedInAs(response, user)
        self.assertEqual(set(user.groups.values_list("name", flat=True)), {"staff"})
        self.assertEqual(self._identity(sub).metadata[GRANTED_ROLES_KEY], ["staff"])

        self.client.logout()
        response = self.login(sub=sub, groups=[])
        self.assertRefused(response, 403, "no back-office access")
        user.refresh_from_db()
        self.assertFalse(user.is_staff)
        self.assertFalse(user.groups.exists())

    def test_hand_granted_role_is_never_removed(self):
        from core.roles import grant_role

        staff = make_staff_user("grace", "grace@example.com")
        grant_role(staff, "manager")
        sub = str(uuid.uuid4())
        self.login(sub=sub, email="grace@example.com", email_verified=True,
                   groups=["golden-managers"])
        # Held already: not recorded as granted by the provider.
        self.assertEqual(self._identity(sub).metadata.get(GRANTED_ROLES_KEY, []), [])
        self.client.logout()
        response = self.login(sub=sub, groups=[])
        self.assertLoggedInAs(response, staff)
        self.assertEqual(set(staff.groups.values_list("name", flat=True)),
                         {"staff", "manager"})

    def test_role_another_identity_still_maps_is_kept(self):
        from core.identity.oidc import MAPPED_ROLES_KEY

        sub = str(uuid.uuid4())
        self.login(sub=sub, groups=["golden-managers", "golden-staff"])
        user = self._identity(sub).user
        UserIdentity.objects.create(user=user, provider="other-idp", subject="o",
                                    metadata={MAPPED_ROLES_KEY: ["manager"]})
        self.client.logout()
        self.login(sub=sub, groups=["golden-staff"])
        self.assertTrue(user.groups.filter(name="manager").exists())

    def test_missing_groups_claim_changes_nothing(self):
        sub = str(uuid.uuid4())
        self.login(sub=sub, groups=["golden-staff"])
        self.client.logout()
        response = self.login(sub=sub)  # no groups claim at all
        user = self._identity(sub).user
        self.assertLoggedInAs(response, user)
        self.assertTrue(user.groups.filter(name="staff").exists())

    @override_settings(GOLDEN_IDENTITY_PROVIDERS=[oidc_provider(
        options={"claims": {"groups": "roles"}, "role_map": {"admins": "staff"}})])
    def test_claims_mapping_names_the_groups_claim(self):
        sub = str(uuid.uuid4())
        response = self.login(sub=sub, roles=["admins"], groups=["golden-staff"])
        self.assertLoggedInAs(response, self._identity(sub).user)


# ── portal bearer path ─────────────────────────────────────────────────────


@override_settings(OIDC_TEST_CLIENT_SECRET="s3cret-for-tests", GOLDEN_ROLES=[MANAGER, VIP],
                   GOLDEN_IDENTITY_PROVIDERS=[*_supabase(), oidc_provider(
                       options={"role_map": {"golden-staff": "staff",
                                             "vips": "vip_customer"}})])
class PortalBearerTests(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        portal_tenant()  # Phase 2 step 7: the portal resolves inside one tenant
        self.issuer = StubIssuer(self)

    def test_verified_email_self_registers_a_customer(self):
        sub = str(uuid.uuid4())
        token = self.issuer.token(sub=sub, email="new@example.com", email_verified=True,
                                  name="New Person")
        user, _ = _authenticate(token)
        self.assertEqual(user.customer.email, "new@example.com")
        self.assertTrue(user.groups.filter(name="customer").exists())
        self.assertFalse(user.is_staff)
        identity = UserIdentity.objects.get(provider="idp", subject=sub)
        self.assertEqual(identity.user, user)
        _authenticate(token)  # linked on the next request
        self.assertEqual(UserIdentity.objects.filter(provider="idp").count(), 1)

    def test_unverified_email_refused(self):
        token = self.issuer.token(email="new@example.com", email_verified=False)
        with self.assertRaises(PortalAuthFailed) as ctx:
            _authenticate(token)
        self.assertEqual(ctx.exception.detail["code"], CODE_VERIFY_EMAIL)

    def test_nested_user_metadata_never_counts_as_verified(self):
        token = self.issuer.token(email="new@example.com",
                                  user_metadata={"email_verified": True})
        with self.assertRaises(PortalAuthFailed):
            _authenticate(token)

    def test_audience_mismatch_refused(self):
        with self.assertRaises(AuthenticationFailed):
            _authenticate(self.issuer.token(aud="someone-else", email_verified=True))

    @override_settings(GOLDEN_IDENTITY_PROVIDERS=[_without_client_id(oidc_provider(
        options={"client_id_env": "GOLDEN_TEST_UNSET_CLIENT_ID",
                 "role_map": {"golden-staff": "staff"}}))])
    def test_unset_client_id_is_a_401_not_a_500(self):
        with self.assertRaises(AuthenticationFailed) as ctx:
            _authenticate(self.issuer.token(email="c@example.com", email_verified=True))
        self.assertIn("unavailable", str(ctx.exception.detail))

    def test_expired_refused(self):
        now = int(time.time())
        with self.assertRaises(AuthenticationFailed):
            _authenticate(self.issuer.token(iat=now - 7200, exp=now - 3600))

    def test_portal_role_sync_adds_and_removes(self):
        sub = str(uuid.uuid4())
        user, _ = _authenticate(self.issuer.token(
            sub=sub, email="v@example.com", email_verified=True, groups=["vips"]))
        self.assertTrue(user.groups.filter(name="vip_customer").exists())
        _authenticate(self.issuer.token(sub=sub, email="v@example.com",
                                        email_verified=True, groups=[]))
        self.assertFalse(user.groups.filter(name="vip_customer").exists())
        self.assertTrue(user.groups.filter(name="customer").exists())


@override_settings(OIDC_TEST_CLIENT_SECRET="s3cret-for-tests",
                   GOLDEN_IDENTITY_PROVIDERS=[oidc_provider(
                       trusted=False, entry_points=["portal"], options={"role_map": {}})])
class UntrustedPortalTests(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        portal_tenant()  # Phase 2 step 7: the portal resolves inside one tenant
        self.issuer = StubIssuer(self)

    def test_untrusted_provider_cannot_link_a_staff_account_by_email(self):
        staff = make_staff_user("grace", "grace@example.com")
        customer, _ = make_portal_customer("grace@example.com", provider="supabase")
        customer.user.delete()
        customer.refresh_from_db()
        customer.user = staff
        customer.save()
        token = self.issuer.token(email="grace@example.com", email_verified=True,
                                  groups=["golden-staff"])
        with self.assertRaises(PortalRefused) as ctx:
            _authenticate(token)
        self.assertEqual(ctx.exception.detail["code"], CODE_COLLISION)
        self.assertFalse(UserIdentity.objects.filter(provider="idp").exists())
        staff.refresh_from_db()
        self.assertEqual(set(staff.groups.values_list("name", flat=True)), {"staff"})

    def test_untrusted_provider_still_serves_customers(self):
        user, _ = _authenticate(self.issuer.token(email="c@example.com", email_verified=True,
                                                  groups=["golden-staff"]))
        self.assertFalse(user.is_staff)
        self.assertEqual(set(user.groups.values_list("name", flat=True)), {"customer"})


@override_settings(GOLDEN_ROLES=[MANAGER], GOLDEN_IDENTITY_PROVIDERS=[oidc_provider(
    options={"erp_link_by_email": False})])
class PortalLinkCannotOpenTheBackOfficeTests(_OIDCBase):
    """erp_link_by_email=False must hold even when the portal links by email.

    Without the fix: (1) the /erp/ login is refused, (2) one portal bearer
    request links UserIdentity(idp, sub) to the staff user through
    _match_1, (3) the next /erp/ login finds that row and opens a session.
    """

    def _staff_with_customer(self, email):
        staff = make_staff_user("grace", email, "pw")
        customer, _ = make_portal_customer(email, provider="supabase")
        customer.user.delete()
        customer.refresh_from_db()
        customer.user = staff
        customer.save()
        return staff

    def test_three_steps_never_reach_a_staff_session(self):
        staff = self._staff_with_customer("grace@example.com")
        sub = str(uuid.uuid4())
        self.assertRefused(self.login(sub=sub, email="grace@example.com",
                                      email_verified=True), 403)
        with self.assertRaises(PortalRefused) as ctx:
            _authenticate(self.issuer.token(sub=sub, email="grace@example.com",
                                            email_verified=True))
        self.assertEqual(ctx.exception.detail["code"], CODE_COLLISION)
        self.assertFalse(UserIdentity.objects.filter(provider="idp").exists())
        self.assertRefused(self.login(sub=sub, email="grace@example.com",
                                      email_verified=True), 403)
        staff.refresh_from_db()
        self.assertTrue(staff.is_staff)

    def test_row_linked_by_email_on_the_portal_is_refused_at_erp(self):
        # A row the portal linked by email (e.g. before the provider turned
        # erp_link_by_email off) does not open the back office either.
        staff = make_staff_user("grace", "grace@example.com", "pw")
        UserIdentity.objects.create(user=staff, provider="idp", subject="s-1",
                                    metadata={"created_via": "portal", "linked_by": "email"})
        self.assertRefused(self.login(sub="s-1", groups=["golden-staff"]), 403,
                           "Ask an administrator to link")

    def test_customer_the_portal_created_is_unaffected(self):
        sub = str(uuid.uuid4())
        user, _ = _authenticate(self.issuer.token(sub=sub, email="new@example.com",
                                                  email_verified=True))
        identity = UserIdentity.objects.get(provider="idp", subject=sub)
        self.assertEqual(identity.metadata.get("created_via"), "portal")
        self.assertNotIn("linked_by", identity.metadata)
        response = self.login(sub=sub, email="new@example.com", email_verified=True,
                              groups=["golden-staff"])
        self.assertLoggedInAs(response, user)


@override_settings(GOLDEN_ROLES=[MANAGER], GOLDEN_IDENTITY_PROVIDERS=[oidc_provider()])
class PortalLinkWithErpLinkingOnTests(_OIDCBase):
    def test_trusted_provider_with_erp_linking_may_link_staff_on_the_portal(self):
        staff = PortalLinkCannotOpenTheBackOfficeTests._staff_with_customer(
            self, "grace@example.com")
        sub = str(uuid.uuid4())
        user, _ = _authenticate(self.issuer.token(sub=sub, email="grace@example.com",
                                                  email_verified=True))
        self.assertEqual(user, staff)
        identity = UserIdentity.objects.get(provider="idp", subject=sub)
        self.assertEqual(identity.metadata.get("linked_by"), "email")
        self.assertLoggedInAs(self.login(sub=sub), staff)
