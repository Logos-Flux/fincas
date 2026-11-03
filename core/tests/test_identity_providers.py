"""Identity step 4: the provider registry, token verification and the dual-role user.

- ``GOLDEN_IDENTITY_PROVIDERS`` validation (``core.identity.providers``):
  reserved names, the built-in ``password``, backends, entry points, roles,
  options, issuer dispatch; no database access while building.
- ``SupabaseJWT`` verification: clock-skew leeway, issuer check and
  dispatch, the JWKS path.
- A staff user who also has a customer identity: the portal token never
  reaches the staff API, a staff credential never reaches the portal.
- ``customers.0006`` re-runs the step-3 backfill.
"""

import contextlib
import importlib
import io
import json
import secrets
import time
import uuid
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIClient

from core.identity import providers as registry
from core.identity.models import UserIdentity
from core.identity.providers import (
    PROVIDERS,
    Password,
    SupabaseJWT,
    build_providers,
    get_provider,
    provider_for_issuer,
)
from core.identity.resolve import CODE_COLLISION
from core.tests.helpers import make_portal_customer, make_staff_user, portal_tenant
from core.tests.legacy_schema import LegacyColumnsTestCase
from core.tests.test_portal_auth import (
    SECRET,
    _authenticate,
    make_token,
    provider_confirms,
    supabase_provider,
)
from customers.models import Customer

ORDERS_URL = "/api/v1/portal/customer/orders/"
STAFF_URL = "/api/v1/purchasing/suppliers/"
ISSUER = "https://project-a.supabase.co/auth/v1"


def _build(entries):
    with override_settings(GOLDEN_IDENTITY_PROVIDERS=entries):
        return build_providers()


class _Confirmed(TestCase):
    """Creating/linking needs the provider to confirm the address; stand it in."""

    def setUp(self):
        super().setUp()
        portal_tenant()  # Phase 2 step 7: the portal resolves inside one tenant
        patcher = provider_confirms(True)
        patcher.start()
        self.addCleanup(patcher.stop)


class RegistryTests(SimpleTestCase):
    def assertRefused(self, entries, fragment):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            _build(entries)
        self.assertIn(fragment, str(ctx.exception))

    def test_default_config_has_password_and_supabase(self):
        self.assertIsInstance(PROVIDERS["password"], Password)
        self.assertEqual(PROVIDERS["password"].config.entry_points, ("erp",))
        self.assertTrue(PROVIDERS["password"].config.trusted)
        supabase = PROVIDERS["supabase"]
        self.assertIsInstance(supabase, SupabaseJWT)
        self.assertFalse(supabase.config.trusted)
        self.assertEqual(supabase.config.roles_on_create, ("customer",))

    def test_password_is_built_in_even_with_an_empty_list(self):
        self.assertEqual(list(_build([])), ["password"])

    def test_reserved_names_refused(self):
        for name in ("local", "legacy_supabase"):
            with self.subTest(name=name):
                self.assertRefused([supabase_provider(name=name)], "reserved")

    def test_builtin_password_cannot_be_redefined(self):
        self.assertRefused([supabase_provider(name="password")], "built-in")

    def test_duplicate_names_refused(self):
        self.assertRefused(
            [supabase_provider(), supabase_provider(options={"jwt_secret_setting": "X",
                                                             "issuer": "b"})],
            "reuses",
        )

    def test_bad_shapes(self):
        cases = [
            ("not a list", "must be a list"),
            (["x"], "must be a dict"),
            ([supabase_provider(name="Bad Name")], "slug"),
            ([{**supabase_provider(), "colour": "red"}], "not a valid ProviderConfig"),
            ([supabase_provider(entry_points="portal")], "list of strings"),
            ([supabase_provider(trusted="yes")], "True or False"),
            ([supabase_provider(options=["x"])], "options must be a dict"),
        ]
        for entries, fragment in cases:
            with self.subTest(fragment=fragment):
                self.assertRefused(entries, fragment)

    def test_backend_must_be_an_importable_provider(self):
        self.assertRefused([supabase_provider(backend="core.identity.providers.Nope")],
                           "cannot be imported")
        self.assertRefused([supabase_provider(backend="core.identity.providers.ProviderConfig")],
                           "not a Provider class")

    def test_entry_points(self):
        self.assertRefused([supabase_provider(entry_points=[])], "no entry_points")
        self.assertRefused([supabase_provider(entry_points=["shop"])], "must be one of")
        self.assertRefused([supabase_provider(entry_points=["erp"])], "cannot serve")

    def test_roles_on_create(self):
        self.assertRefused([supabase_provider(roles_on_create=["nobody"])], "unknown role")
        self.assertRefused([supabase_provider(roles_on_create=["customer", "staff"])],
                           "may not grant 'staff'")
        self.assertRefused([supabase_provider(roles_on_create=["storefront"])],
                           "may not grant 'storefront'")
        self.assertRefused([supabase_provider(roles_on_create=[])], "portal role")
        # Even a trusted provider never hands out staff on account creation.
        self.assertRefused([supabase_provider(trusted=True, roles_on_create=["customer", "staff"])],
                           "may not grant 'staff'")

    @override_settings(GOLDEN_ROLES=[{"name": "vip", "label": "VIP", "portal": True}])
    def test_appended_portal_role_accepted(self):
        providers = _build([supabase_provider(roles_on_create=["vip"])])
        self.assertEqual(providers["supabase"].config.roles_on_create, ("vip",))

    def test_options(self):
        self.assertRefused([supabase_provider(options={"jwt_secret": "inline"})], "is a secret")
        self.assertRefused([supabase_provider(options={"jwt_secret_setting": "S",
                                                       "colour": "red"})], "unknown option")
        self.assertRefused([supabase_provider(options={"audience": "authenticated"})],
                           "jwt_secret_setting/_env or jwks_url")
        self.assertRefused([supabase_provider(options={"jwt_secret_setting": "A",
                                                       "jwt_secret_env": "B"})],
                           "more than once")
        self.assertRefused([supabase_provider(options={"jwt_secret_setting": ""})],
                           "must name a setting")

    def test_several_portal_providers_need_issuers(self):
        second = supabase_provider(name="supabase-b", options={
            "jwt_secret_setting": "B", "url_setting": "U", "service_role_key_setting": "K"})
        self.assertRefused([supabase_provider(), second], "supabase-b")
        second["options"]["issuer"] = "https://b.example/auth/v1"
        providers = _build([supabase_provider(), second])
        self.assertEqual(set(providers), {"password", "supabase", "supabase-b"})

    def test_email_linking_and_self_register_need_a_confirmable_provider(self):
        """Fail closed at config time: no fallback to the user-writable token flag."""
        bare = {"jwt_secret_setting": "SUPABASE_JWT_SECRET"}
        self.assertRefused([supabase_provider(options=bare)], "can ask whether an email")
        self.assertRefused([supabase_provider(options=bare, link_by_verified_email=False)],
                           "can ask whether an email")
        self.assertRefused([supabase_provider(options=bare, self_register=False)],
                           "can ask whether an email")
        # Linking only through existing identities (invites) needs no Admin API.
        providers = _build([supabase_provider(options=bare, self_register=False,
                                              link_by_verified_email=False)])
        self.assertIn("supabase", providers)

    def test_building_touches_no_database(self):
        # A SimpleTestCase refuses every query (connect, cursor) with
        # DatabaseOperationForbidden, so building under it is the assertion.
        # Not CaptureQueriesContext: its own ensure_connection() is refused
        # whenever no connection happens to be open, which made this test pass
        # or fail by test order (it failed on Postgres, whose connections are
        # closed after each request).
        self.assertEqual(self.databases, set())
        build_providers()

    def test_registry_follows_override_settings(self):
        with override_settings(GOLDEN_IDENTITY_PROVIDERS=[]):
            self.assertIsNone(get_provider("supabase"))
        self.assertIsNotNone(get_provider("supabase"))


@override_settings(SUPABASE_JWT_SECRET=SECRET)
class LeewayTests(_Confirmed):
    """GOLDEN_IDENTITY_JWT_LEEWAY_S (default 30) absorbs clock skew, no more."""

    def test_recently_expired_token_within_leeway_accepted(self):
        user, _ = _authenticate(make_token(email_verified=True, email="skew@example.com", exp_delta_s=-10))
        self.assertEqual(user.customer.email, "skew@example.com")

    def test_expired_beyond_leeway_rejected(self):
        with self.assertRaises(AuthenticationFailed) as ctx:
            _authenticate(make_token(email="skew@example.com", exp_delta_s=-60))
        self.assertIn("expired", str(ctx.exception).lower())

    @override_settings(GOLDEN_IDENTITY_JWT_LEEWAY_S=0)
    def test_zero_leeway(self):
        with self.assertRaises(AuthenticationFailed):
            _authenticate(make_token(email="skew@example.com", exp_delta_s=-10))

    def test_iat_slightly_in_the_future_accepted(self):
        user, _ = _authenticate(make_token(email_verified=True, email="fast@example.com", iat=int(time.time()) + 20))
        self.assertEqual(user.customer.email, "fast@example.com")

    def test_iat_far_in_the_future_rejected(self):
        with self.assertRaises(AuthenticationFailed):
            _authenticate(make_token(email="future@example.com", iat=int(time.time()) + 600))

    def test_exp_is_required(self):
        payload = {"sub": str(uuid.uuid4()), "aud": "authenticated", "email": "e@example.com"}
        with self.assertRaises(AuthenticationFailed):
            _authenticate(jwt.encode(payload, SECRET, algorithm="HS256"))


@override_settings(SUPABASE_JWT_SECRET=SECRET, SUPABASE_ISSUER=ISSUER)
class IssuerTests(_Confirmed):
    def test_matching_issuer_accepted(self):
        user, _ = _authenticate(make_token(email_verified=True, email="iss@example.com", iss=ISSUER))
        self.assertEqual(user.customer.email, "iss@example.com")

    def test_issuer_mismatch_rejected(self):
        with self.assertRaises(AuthenticationFailed) as ctx:
            _authenticate(make_token(email="iss@example.com", iss="https://evil.example/auth/v1"))
        self.assertIn("issuer", str(ctx.exception))
        self.assertFalse(Customer.objects.exists())

    def test_missing_issuer_rejected_once_one_is_configured(self):
        with self.assertRaises(AuthenticationFailed):
            _authenticate(make_token(email="iss@example.com"))

    @override_settings(SUPABASE_ISSUER="")
    def test_no_issuer_configured_accepts_any_iss(self):
        """Back-compat: golden's default sets no issuer, as before step 4."""
        user, _ = _authenticate(make_token(email_verified=True, email="any@example.com", iss="https://x/auth/v1"))
        self.assertEqual(user.customer.email, "any@example.com")


SECRET_B = "test-supabase-b-" + secrets.token_hex(16)
ISSUER_B = "https://project-b.supabase.co/auth/v1"


@override_settings(
    SUPABASE_JWT_SECRET=SECRET, SUPABASE_ISSUER=ISSUER, SECRET_B=SECRET_B,
    GOLDEN_IDENTITY_PROVIDERS=[
        supabase_provider(),
        supabase_provider(name="shop-b", options={
            "jwt_secret_setting": "SECRET_B", "issuer": ISSUER_B, "audience": "authenticated",
            "url_setting": "SUPABASE_URL", "service_role_key_setting": "SUPABASE_SERVICE_ROLE_KEY",
        }),
    ],
)
class IssuerDispatchTests(_Confirmed):
    """Several portal providers share the Bearer header; ``iss`` picks one."""

    def test_each_issuer_is_verified_with_its_own_key(self):
        self.assertEqual(provider_for_issuer(ISSUER).config.name, "supabase")
        self.assertEqual(provider_for_issuer(ISSUER_B).config.name, "shop-b")
        self.assertIsNone(provider_for_issuer("https://other/auth/v1"))
        self.assertIsNone(provider_for_issuer(None))
        sub = str(uuid.uuid4())
        user, _ = _authenticate(make_token(email_verified=True, sub=sub, email="b@example.com", iss=ISSUER_B,
                                           secret=SECRET_B))
        self.assertEqual(UserIdentity.objects.get(user=user).provider, "shop-b")
        self.assertEqual(UserIdentity.objects.get(user=user).subject, sub)

    def test_token_is_not_tried_against_the_other_key(self):
        # Claims issuer A but is signed with B's key: verified with A's only.
        with self.assertRaises(AuthenticationFailed):
            _authenticate(make_token(email="x@example.com", iss=ISSUER, secret=SECRET_B))

    def test_same_subject_at_two_providers_is_two_identities(self):
        sub = str(uuid.uuid4())
        user_a, _ = _authenticate(make_token(email_verified=True, sub=sub, email="a@example.com", iss=ISSUER))
        user_b, _ = _authenticate(make_token(email_verified=True, sub=sub, email="b2@example.com", iss=ISSUER_B,
                                             secret=SECRET_B))
        self.assertNotEqual(user_a, user_b)


JWKS_URL = "https://project-a.supabase.co/auth/v1/.well-known/jwks.json"


@override_settings(SUPABASE_JWT_SECRET=SECRET, SUPABASE_JWKS_URL=JWKS_URL)
class JWKSTests(_Confirmed):
    """Supabase's asymmetric signing keys; JWKS wins over the HS256 secret."""

    def setUp(self):
        super().setUp()
        self.key = ec.generate_private_key(ec.SECP256R1())
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({"kid": "k1", "alg": "ES256", "use": "sig"})
        registry._jwks_clients.clear()
        jwks = {"keys": [jwk]}

        def fetch_data(client):  # the real one, minus the network: it fills the cache
            client.jwk_set_cache.put(jwks)
            return jwks

        patcher = mock.patch.object(jwt.PyJWKClient, "fetch_data", autospec=True,
                                    side_effect=fetch_data)
        self.fetch = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(registry._jwks_clients.clear)

    def _es256(self, **claims):
        now = int(time.time())
        payload = {"sub": str(uuid.uuid4()), "aud": "authenticated", "iat": now,
                   "exp": now + 3600, "email": "jwks@example.com",
                   "user_metadata": {"email_verified": True}, **claims}
        return jwt.encode(payload, self.key, algorithm="ES256", headers={"kid": "k1"})

    def test_es256_token_verified_through_jwks(self):
        sub = str(uuid.uuid4())
        user, _ = _authenticate(self._es256(sub=sub))
        self.assertEqual(user.customer.email, "jwks@example.com")
        self.fetch.assert_called_once()
        _authenticate(self._es256(sub=sub))  # the key set is cached
        self.fetch.assert_called_once()

    def test_hs256_token_still_verifies_with_the_secret_during_key_migration(self):
        user, _ = _authenticate(make_token(email="hs@example.com", email_verified=True))
        self.assertEqual(user.customer.email, "hs@example.com")
        self.fetch.assert_not_called()

    @override_settings(SUPABASE_JWT_SECRET=None)
    def test_hs256_token_refused_when_only_jwks_is_configured(self):
        with self.assertRaises(AuthenticationFailed):
            _authenticate(make_token(email="hs@example.com", email_verified=True))

    @override_settings(SUPABASE_JWT_SECRET=None)
    def test_hs256_forged_with_the_public_key_refused(self):
        from cryptography.hazmat.primitives import serialization

        pem = self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        # PyJWT will not sign HS256 with a PEM, so forge the signature by hand.
        import base64
        import hashlib
        import hmac

        def b64(raw):
            return base64.urlsafe_b64encode(raw).rstrip(b"=")

        head = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
        body = b64(json.dumps({"sub": "x", "aud": "authenticated", "email": "f@example.com",
                               "exp": int(time.time()) + 60,
                               "user_metadata": {"email_verified": True}}).encode())
        signature = b64(hmac.new(pem, head + b"." + body, hashlib.sha256).digest())
        forged = (head + b"." + body + b"." + signature).decode()
        with self.assertRaises(AuthenticationFailed):
            _authenticate(forged)

    def test_alg_that_does_not_match_the_key_is_401_not_500(self):
        """Header says RS256 but kid names the EC key: the key's own alg is enforced."""
        from cryptography.hazmat.primitives.asymmetric import rsa

        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode({"sub": "x", "aud": "authenticated", "exp": int(time.time()) + 60},
                           rsa_key, algorithm="RS256", headers={"kid": "k1"})
        with self.assertRaises(AuthenticationFailed):
            _authenticate(token)

    def test_unknown_kids_do_not_refetch_on_every_request(self):
        _authenticate(self._es256())  # first fetch
        for _ in range(5):
            token = jwt.encode({"sub": "x", "aud": "authenticated",
                                "exp": int(time.time()) + 60},
                               self.key, algorithm="ES256", headers={"kid": uuid.uuid4().hex})
            with self.assertRaises(AuthenticationFailed):
                _authenticate(token)
        self.assertLessEqual(self.fetch.call_count, 2)

    def test_unknown_kid_refused(self):
        token = jwt.encode({"sub": "x", "aud": "authenticated", "exp": int(time.time()) + 60},
                           self.key, algorithm="ES256", headers={"kid": "other"})
        with self.assertRaises(AuthenticationFailed):
            _authenticate(token)

    def test_other_key_refused(self):
        other = ec.generate_private_key(ec.SECP256R1())
        token = jwt.encode({"sub": "x", "aud": "authenticated", "exp": int(time.time()) + 60},
                           other, algorithm="ES256", headers={"kid": "k1"})
        with self.assertRaises(AuthenticationFailed):
            _authenticate(token)


@override_settings(SUPABASE_JWT_SECRET=SECRET)
class DualRoleTests(_Confirmed):
    """A staff user who also holds a customer identity.

    The portal token class is registered only on portal views and the staff
    API classes never accept a provider token, so neither credential crosses
    over. With the default untrusted provider the portal also refuses the
    staff account outright (D5).
    """

    def setUp(self):
        super().setUp()
        self.staff = make_staff_user("dual", email="dual@example.com", password="pw")
        self.customer = Customer.objects.create(first_name="D", last_name="R",
                                                email="dual@example.com", user=self.staff)
        self.sub = str(uuid.uuid4())
        UserIdentity.objects.create(user=self.staff, provider="supabase", subject=self.sub)
        from core.roles import grant_role

        grant_role(self.staff, "customer")

    def _bearer(self):
        return APIClient(HTTP_AUTHORIZATION=f"Bearer {make_token(sub=self.sub)}")

    def test_untrusted_portal_token_refused_for_staff_account(self):
        response = self._bearer().get(ORDERS_URL)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], CODE_COLLISION)

    def test_portal_token_never_reaches_staff_api(self):
        self.assertIn(self._bearer().get(STAFF_URL).status_code, (401, 403))

    def test_staff_session_never_reaches_portal(self):
        client = APIClient()
        client.force_login(self.staff)
        self.assertEqual(client.get(STAFF_URL).status_code, 200)
        self.assertIn(client.get(ORDERS_URL).status_code, (401, 403))

    def test_staff_bearer_principal_never_reaches_portal(self):
        client = APIClient()
        client.force_authenticate(user=self.staff)
        self.assertIn(client.get(ORDERS_URL).status_code, (401, 403))

    @override_settings(GOLDEN_IDENTITY_PROVIDERS=[supabase_provider(trusted=True)])
    def test_trusted_provider_reaches_portal_but_still_not_staff_api(self):
        response = self._bearer().get(ORDERS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertIn(self._bearer().get(STAFF_URL).status_code, (401, 403))

    def test_customer_only_user_session_is_403_on_staff_api(self):
        customer, _sub = make_portal_customer("only@example.com")
        client = APIClient()
        client.force_login(customer.user)
        self.assertEqual(client.get(STAFF_URL).status_code, 403)


class RerunBackfillMigrationTests(LegacyColumnsTestCase, _Confirmed):
    """``customers.0006`` links customers the old resolver linked by uid only."""

    MIGRATION = importlib.import_module("customers.migrations.0006_rerun_identity_backfill")

    def _forwards(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.MIGRATION.forwards(self.H, None)

    def test_links_uid_only_customers_and_is_idempotent(self):
        already, sub = make_portal_customer("done@example.com")
        uid = uuid.uuid4()
        late = Customer.objects.create(first_name="L", last_name="T", email="late@example.com")
        self.set_customer_uid(late, uid)
        self._forwards()
        late.refresh_from_db()
        self.assertIsNotNone(late.user)
        self.assertEqual(UserIdentity.objects.get(subject=str(uid)).user, late.user)
        self.assertEqual(UserIdentity.objects.filter(subject=sub).count(), 1)
        count = UserIdentity.objects.count()
        self._forwards()
        self.assertEqual(UserIdentity.objects.count(), count)

    def test_linked_customer_then_logs_in_through_the_identity(self):
        uid = uuid.uuid4()
        late = Customer.objects.create(first_name="L", last_name="T", email="late@example.com")
        self.set_customer_uid(late, uid)
        self._forwards()
        with override_settings(SUPABASE_JWT_SECRET=SECRET):
            user, _ = _authenticate(make_token(sub=str(uid)))
        late.refresh_from_db()
        self.assertEqual(user, late.user)
