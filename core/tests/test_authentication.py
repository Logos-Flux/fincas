"""
Tests for DRF authentication backends (BearerTokenAuth, APIKeyAuth) and
the token-obtain endpoint.

Post CF-exit these are the primary auth mechanism for external API clients:
- Chrome extension (uses Api-Key)
- Fly worker (uses Api-Key or Bearer JWT)
- CLI tools (POST /api/v1/core/auth/token/ → Bearer JWT)
"""

import secrets
import time

import jwt
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIRequestFactory

from core.authentication import (
    APIKeyAuthentication,
    BearerTokenAuthentication,
    _get_service_user,
    get_jwt_signing_key,
    issue_api_jwt,
)

# ── helpers ─────────────────────────────────────────────────────────────────

# Fake API key generated fresh per test run — never a real credential, and kept
# out of the source as a literal so the secret scanner has nothing to flag.
# All tests inject it via override_settings(API_KEY=API_KEY).
API_KEY = "test-" + secrets.token_hex(16)
# A configured API_JWT_SIGNING_KEY, built at runtime like API_KEY.
DEDICATED_SIGNING = secrets.token_hex(24)


def _rf() -> APIRequestFactory:
    return APIRequestFactory()


# ── _get_service_user ──────────────────────────────────────────────────────


class GetServiceUserTests(TestCase):
    def test_provisioned_account_shape(self):
        # The test DB has it via the post_migrate stand-in for core.0009.
        user = _get_service_user()
        self.assertEqual(user.username, "api-service")
        self.assertTrue(user.is_superuser)
        # is_staff too, so IsAdminUser endpoints the old first-superuser
        # reached keep working; the unusable password keeps it out of /erp/.
        self.assertTrue(user.is_staff)
        self.assertFalse(user.has_usable_password())
        # second call returns same user
        self.assertEqual(_get_service_user().pk, user.pk)

    def test_never_returns_a_human_superuser(self):
        # Spec §2.6: the legacy key no longer acts as "the first superuser".
        human = User.objects.create_superuser("aaa-admin", "a@b.com", "pass")
        user = _get_service_user()
        self.assertNotEqual(user.pk, human.pk)
        self.assertEqual(user.username, "api-service")

    @override_settings(API_KEY=API_KEY)
    def test_api_service_with_a_password_is_refused_not_wiped(self):
        user = User.objects.get(username="api-service")
        user.set_password("pass")
        user.save()
        request = _rf().get("/", HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")
        from rest_framework.exceptions import AuthenticationFailed

        with self.assertRaises(AuthenticationFailed):
            APIKeyAuthentication().authenticate(request)
        self.assertTrue(User.objects.get(username="api-service").has_usable_password())

    @override_settings(API_KEY=API_KEY, GOLDEN_LEGACY_API_KEY_USER="aaron")
    def test_legacy_user_setting_cannot_name_a_human(self):
        User.objects.create_superuser("aaron", "a@b.com", "pass")
        request = _rf().get("/", HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")
        from rest_framework.exceptions import AuthenticationFailed

        with self.assertRaises(AuthenticationFailed):
            APIKeyAuthentication().authenticate(request)
        self.assertTrue(User.objects.get(username="aaron").has_usable_password())

    @override_settings(GOLDEN_LEGACY_API_KEY_USER=None)
    def test_legacy_path_can_be_disabled(self):
        self.assertIsNone(_get_service_user())

    @override_settings(API_KEY=API_KEY)
    def test_deleted_account_is_not_recreated_by_a_request(self):
        from rest_framework.exceptions import AuthenticationFailed

        User.objects.filter(username="api-service").delete()
        for header in (f"Api-Key {API_KEY}", f"Bearer {issue_api_jwt()}"):
            with self.subTest(header=header[:7]):
                request = _rf().get("/", HTTP_AUTHORIZATION=header)
                auth = APIKeyAuthentication() if header.startswith("Api") else BearerTokenAuthentication()
                with self.assertRaises(AuthenticationFailed):
                    auth.authenticate(request)
        self.assertFalse(User.objects.filter(username="api-service").exists())
        out = StringIO()
        call_command("ensure_legacy_api_user", stdout=out)
        self.assertIn("usable", out.getvalue())
        self.assertTrue(User.objects.get(username="api-service").is_superuser)

    @override_settings(API_KEY=API_KEY)
    def test_backfill_identity_rows_do_not_make_it_a_human(self):
        from core.identity import RESERVED_PROVIDER_NAMES
        from core.identity.models import UserIdentity

        user = User.objects.get(username="api-service")
        for provider in sorted(RESERVED_PROVIDER_NAMES):
            UserIdentity.objects.create(user=user, provider=provider, subject=f"{provider}:{user.pk}")
        request = _rf().get("/", HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")
        self.assertEqual(APIKeyAuthentication().authenticate(request)[0], user)
        UserIdentity.objects.create(user=user, provider="supabase", subject="real-login")
        from rest_framework.exceptions import AuthenticationFailed

        with self.assertRaises(AuthenticationFailed):
            APIKeyAuthentication().authenticate(request)


# ── issue_api_jwt ──────────────────────────────────────────────────────────


@override_settings()
class IssueJwtTests(TestCase):
    @override_settings(API_KEY=API_KEY)
    def test_issue_valid_token_structure(self):
        token = issue_api_jwt(lifetime_s=60)
        payload = jwt.decode(token, get_jwt_signing_key(), algorithms=["HS256"], options={"require": ["exp", "iss", "sub"]})
        self.assertEqual(payload["iss"], "golden-erp")
        self.assertEqual(payload["sub"], "api-service")
        self.assertAlmostEqual(payload["exp"], int(time.time()) + 60, delta=2)

    @override_settings(API_KEY="")
    def test_raises_when_api_key_not_set(self):
        # api-service tokens die with the legacy key.
        with self.assertRaises(ValueError):
            issue_api_jwt()

    @override_settings(API_KEY=API_KEY)
    def test_signed_with_the_signing_key_not_api_key(self):
        token = issue_api_jwt()
        with self.assertRaises(jwt.InvalidSignatureError):
            jwt.decode(token, API_KEY, algorithms=["HS256"])
        jwt.decode(token, get_jwt_signing_key(), algorithms=["HS256"])

    @override_settings(API_KEY=API_KEY, API_JWT_SIGNING_KEY=DEDICATED_SIGNING)
    def test_configured_signing_key_is_used(self):
        token = issue_api_jwt()
        jwt.decode(token, DEDICATED_SIGNING, algorithms=["HS256"])

    @override_settings(API_KEY=API_KEY)
    def test_default_lifetime(self):
        token = issue_api_jwt()
        payload = jwt.decode(token, get_jwt_signing_key(), algorithms=["HS256"], options={"verify_exp": False})
        self.assertAlmostEqual(payload["exp"], int(time.time()) + 3600, delta=2)

    @override_settings(API_KEY=API_KEY)
    def test_explicit_lifetime(self):
        token = issue_api_jwt(lifetime_s=120)
        payload = jwt.decode(token, get_jwt_signing_key(), algorithms=["HS256"], options={"verify_exp": False})
        self.assertAlmostEqual(payload["exp"], int(time.time()) + 120, delta=2)


# ── BearerTokenAuthentication ──────────────────────────────────────────────


@override_settings()
class BearerAuthTests(TestCase):
    def setUp(self):
        User.objects.exclude(username="api-service").delete()  # keep the migrated account

    def test_returns_none_when_no_auth_header(self):
        with override_settings(API_KEY=API_KEY):
            auth = BearerTokenAuthentication()
            request = _rf().get("/")
            self.assertIsNone(auth.authenticate(request))

    def test_api_service_token_dies_with_api_key(self):
        from rest_framework.exceptions import AuthenticationFailed

        with override_settings(API_KEY=API_KEY):
            token = issue_api_jwt()
        with override_settings(API_KEY=""):
            request = _rf().get("/", HTTP_AUTHORIZATION=f"Bearer {token}")
            with self.assertRaises(AuthenticationFailed):
                BearerTokenAuthentication().authenticate(request)

    def test_garbage_bearer_is_refused(self):
        from rest_framework.exceptions import AuthenticationFailed

        request = _rf().get("/", HTTP_AUTHORIZATION="Bearer some-token")
        with self.assertRaises(AuthenticationFailed):
            BearerTokenAuthentication().authenticate(request)

    def test_returns_none_when_not_bearer_prefix(self):
        with override_settings(API_KEY=API_KEY):
            auth = BearerTokenAuthentication()
            request = _rf().get("/", HTTP_AUTHORIZATION="Basic dXNlcjpwYXNz")
            self.assertIsNone(auth.authenticate(request))

    def test_returns_none_when_empty_token(self):
        with override_settings(API_KEY=API_KEY):
            auth = BearerTokenAuthentication()
            request = _rf().get("/", HTTP_AUTHORIZATION="Bearer ")
            self.assertIsNone(auth.authenticate(request))

    def test_authenticates_valid_token(self):
        with override_settings(API_KEY=API_KEY):
            token = issue_api_jwt()
            auth = BearerTokenAuthentication()
            request = _rf().get("/", HTTP_AUTHORIZATION=f"Bearer {token}")
            user, returned_token = auth.authenticate(request)
            self.assertEqual(user.username, "api-service")
            self.assertEqual(returned_token, token)

    def test_expired_token_raises_authentication_failed(self):
        with override_settings(API_KEY=API_KEY):
            # issue a token that expires 1 second ago
            now = int(time.time())
            payload = {"iss": "golden-erp", "iat": now - 3600, "exp": now - 1, "jti": "test", "sub": "api-service"}
            token = jwt.encode(payload, get_jwt_signing_key(), algorithm="HS256")

            auth = BearerTokenAuthentication()
            request = _rf().get("/", HTTP_AUTHORIZATION=f"Bearer {token}")
            from rest_framework.exceptions import AuthenticationFailed
            with self.assertRaises(AuthenticationFailed) as ctx:
                auth.authenticate(request)
            self.assertIn("expired", str(ctx.exception).lower())

    def test_wrong_issuer_raises_authentication_failed(self):
        with override_settings(API_KEY=API_KEY):
            now = int(time.time())
            payload = {"iss": "wrong-issuer", "iat": now, "exp": now + 60, "jti": "test", "sub": "api-service"}
            token = jwt.encode(payload, get_jwt_signing_key(), algorithm="HS256")

            auth = BearerTokenAuthentication()
            request = _rf().get("/", HTTP_AUTHORIZATION=f"Bearer {token}")
            from rest_framework.exceptions import AuthenticationFailed
            with self.assertRaises(AuthenticationFailed) as ctx:
                auth.authenticate(request)
            self.assertIn("issuer", str(ctx.exception).lower())

    def test_wrong_signing_key_raises_authentication_failed(self):
        with override_settings(API_KEY=API_KEY):
            token = jwt.encode({"iss": "golden-erp", "iat": int(time.time()), "exp": int(time.time()) + 60, "sub": "api-service"}, "wrong-key", algorithm="HS256")
            auth = BearerTokenAuthentication()
            request = _rf().get("/", HTTP_AUTHORIZATION=f"Bearer {token}")
            from rest_framework.exceptions import AuthenticationFailed
            with self.assertRaises(AuthenticationFailed):
                auth.authenticate(request)

    def test_authenticate_header(self):
        auth = BearerTokenAuthentication()
        self.assertEqual(auth.authenticate_header(_rf().get("/")), "Bearer")


# ── APIKeyAuthentication ───────────────────────────────────────────────────


@override_settings()
class APIKeyAuthTests(TestCase):
    def setUp(self):
        User.objects.exclude(username="api-service").delete()  # keep the migrated account

    def test_returns_none_when_no_auth_header(self):
        with override_settings(API_KEY=API_KEY):
            auth = APIKeyAuthentication()
            request = _rf().get("/")
            self.assertIsNone(auth.authenticate(request))

    def test_returns_none_when_api_key_not_configured(self):
        with override_settings(API_KEY=""):
            auth = APIKeyAuthentication()
            request = _rf().get("/", HTTP_AUTHORIZATION="Api-Key some-key")
            self.assertIsNone(auth.authenticate(request))

    def test_returns_none_when_not_api_key_prefix(self):
        with override_settings(API_KEY=API_KEY):
            auth = APIKeyAuthentication()
            request = _rf().get("/", HTTP_AUTHORIZATION="Bearer some-token")
            self.assertIsNone(auth.authenticate(request))

    def test_returns_none_when_wrong_key(self):
        with override_settings(API_KEY=API_KEY):
            auth = APIKeyAuthentication()
            request = _rf().get("/", HTTP_AUTHORIZATION="Api-Key wrong-key")
            self.assertIsNone(auth.authenticate(request))

    def test_authenticates_valid_key(self):
        with override_settings(API_KEY=API_KEY):
            auth = APIKeyAuthentication()
            request = _rf().get("/", HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")
            user, auth_info = auth.authenticate(request)
            self.assertEqual(user.username, "api-service")
            self.assertEqual(auth_info, "api-key")


# ── TokenObtainView ────────────────────────────────────────────────────────


@override_settings(API_KEY=API_KEY)
class TokenObtainViewTests(TestCase):
    def test_returns_401_without_api_key(self):
        response = self.client.post("/api/v1/core/auth/token/", {}, content_type="application/json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("Api-Key", response.json()["error"])

    def test_returns_401_with_wrong_key(self):
        response = self.client.post(
            "/api/v1/core/auth/token/",
            {},
            content_type="application/json",
            HTTP_AUTHORIZATION="Api-Key wrong-key",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_returns_200_and_jwt_with_valid_key(self):
        response = self.client.post(
            "/api/v1/core/auth/token/",
            {},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Api-Key {API_KEY}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("token", data)
        self.assertEqual(data["expires_in_s"], 3600)
        self.assertIn("expires_at", data)

        # Verify we can use the returned token for Bearer auth
        request = _rf().get("/", HTTP_AUTHORIZATION=f"Bearer {data['token']}")
        auth = BearerTokenAuthentication()
        user, _ = auth.authenticate(request)
        self.assertEqual(user.username, "api-service")

    def test_custom_lifetime(self):
        response = self.client.post(
            "/api/v1/core/auth/token/",
            {"lifetime_s": 120},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Api-Key {API_KEY}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["expires_in_s"], 120)

    def test_lifetime_clamped_to_86400_max(self):
        response = self.client.post(
            "/api/v1/core/auth/token/",
            {"lifetime_s": 999999},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Api-Key {API_KEY}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["expires_in_s"], 86400)

    def test_lifetime_clamped_to_60_min(self):
        response = self.client.post(
            "/api/v1/core/auth/token/",
            {"lifetime_s": 5},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Api-Key {API_KEY}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["expires_in_s"], 60)


# ── Service accounts and per-key auth (identity step 5, spec §2.6) ─────────

from datetime import timedelta  # noqa: E402
from io import StringIO  # noqa: E402

from django.core.cache import cache  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.core.management.base import CommandError  # noqa: E402
from django.test import RequestFactory  # noqa: E402
from django.utils import timezone  # noqa: E402
from rest_framework.exceptions import AuthenticationFailed  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from core.models import ApiKey, Company, UserProfile  # noqa: E402
from core.service_accounts import (  # noqa: E402
    LAST_USED_RESOLUTION,
    ensure_service_account,
    is_service_account,
    issue_api_key,
    revoke,
)
from core.tests.helpers import make_staff_user  # noqa: E402

SERVICE_ROLES = [
    {"name": "crm-sync", "label": "CRM sync", "kind": "service",
     "permissions": ["purchasing.view_supplier"]},
    {"name": "manager", "label": "Manager", "staff": True},
]
SUPPLIERS = "/api/v1/purchasing/suppliers/"
TOKEN_URL = "/api/v1/core/auth/token/"


def _signed(payload: dict, key: str | None = None) -> str:
    now = int(time.time())
    body = {"iss": "golden-erp", "iat": now, "exp": now + 60, "jti": "t", **payload}
    return jwt.encode(body, key or get_jwt_signing_key(), algorithm="HS256")


@override_settings(API_KEY=API_KEY, GOLDEN_ROLES=SERVICE_ROLES)
class LegacyApiKeyTests(TestCase):
    """The legacy API_KEY never resolves to a human (spec §2.6)."""

    def setUp(self):
        # Sorts before "api-service": the old code would have picked it.
        self.human = User.objects.create_superuser("aaron", "a@example.com", "pw")

    def _whoami(self, **headers):
        request = _rf().get(SUPPLIERS, **headers)
        result = APIKeyAuthentication().authenticate(request)
        return None if result is None else result[0]

    def test_legacy_key_acts_as_api_service_not_a_human(self):
        user = self._whoami(HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")
        self.assertEqual(user.username, "api-service")
        self.assertNotEqual(user.pk, self.human.pk)
        self.assertFalse(user.has_usable_password())

    def test_legacy_key_use_is_logged_as_deprecated(self):
        with self.assertLogs("core.service_accounts", "WARNING") as logs:
            self._whoami(HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")
        self.assertIn("Deprecated", logs.output[0])

    def test_legacy_bearer_resolves_to_api_service(self):
        request = _rf().get(SUPPLIERS, HTTP_AUTHORIZATION=f"Bearer {issue_api_jwt()}")
        user, _ = BearerTokenAuthentication().authenticate(request)
        self.assertEqual(user.username, "api-service")

    def test_legacy_key_keeps_admin_only_endpoints(self):
        client = APIClient(HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")
        self.assertEqual(client.get("/api/v1/core/companies/").status_code, 200)

    def test_rotating_api_key_retires_api_service_bearers(self):
        token = issue_api_jwt()
        with override_settings(API_KEY="rotated-" + secrets.token_hex(8)):
            with self.assertRaises(AuthenticationFailed):
                BearerTokenAuthentication().authenticate(
                    _rf().get(SUPPLIERS, HTTP_AUTHORIZATION=f"Bearer {token}"))
        forged = _signed({"sub": "api-service"})  # signing key, but no akh
        with self.assertRaises(AuthenticationFailed):
            BearerTokenAuthentication().authenticate(
                _rf().get(SUPPLIERS, HTTP_AUTHORIZATION=f"Bearer {forged}"))

    def test_gk_shaped_legacy_key_still_works(self):
        shaped = "gk_deadbeef_" + secrets.token_hex(8)
        with override_settings(API_KEY=shaped):
            user = self._whoami(HTTP_AUTHORIZATION=f"Api-Key {shaped}")
        self.assertEqual(user.username, "api-service")

    def test_inactive_api_service_is_refused(self):
        _get_service_user()
        User.objects.filter(username="api-service").update(is_active=False)
        with self.assertRaises(AuthenticationFailed):
            self._whoami(HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")

    @override_settings(GOLDEN_LEGACY_API_KEY_USER=None, API_JWT_ACCEPT_API_KEY_SIGNATURE=True)
    def test_disabled_legacy_path_refuses_the_key(self):
        with self.assertRaises(AuthenticationFailed):
            self._whoami(HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")
        # ...and the API_KEY-signed bearer fallback, even opted in: a
        # long-lived storefront token minted with the old key must not
        # outlive the legacy path being switched off.
        far = int(time.time()) + 10 * 365 * 86400
        for sub in ("api-service", "storefront"):
            old = _signed({"sub": sub, "exp": far}, key=API_KEY)
            with self.subTest(sub=sub), self.assertRaises(AuthenticationFailed):
                BearerTokenAuthentication().authenticate(
                    _rf().get(SUPPLIERS, HTTP_AUTHORIZATION=f"Bearer {old}"))

    def test_bearer_signed_with_api_key_is_off_by_default(self):
        for sub in ("api-service", "storefront"):
            old = _signed({"sub": sub}, key=API_KEY)
            with self.subTest(sub=sub), self.assertRaises(AuthenticationFailed):
                BearerTokenAuthentication().authenticate(
                    _rf().get(SUPPLIERS, HTTP_AUTHORIZATION=f"Bearer {old}"))

    @override_settings(API_JWT_ACCEPT_API_KEY_SIGNATURE=True)
    def test_bearer_signed_with_api_key_accepted_only_for_legacy_subjects_when_opted_in(self):
        old = _signed({"sub": "api-service"}, key=API_KEY)
        user, _ = BearerTokenAuthentication().authenticate(
            _rf().get(SUPPLIERS, HTTP_AUTHORIZATION=f"Bearer {old}"))
        self.assertEqual(user.username, "api-service")

    def test_bearer_sub_must_be_a_service(self):
        for sub in ("aaron", "admin", "storefront-api", "svc-nobody"):
            with self.subTest(sub=sub):
                token = _signed({"sub": sub})
                with self.assertRaises(AuthenticationFailed):
                    BearerTokenAuthentication().authenticate(
                        _rf().get(SUPPLIERS, HTTP_AUTHORIZATION=f"Bearer {token}"))

    def test_issue_refuses_human_subjects(self):
        for sub in ("aaron", "svc-nobody"):
            with self.subTest(sub=sub), self.assertRaises(ValueError):
                issue_api_jwt(sub=sub)


@override_settings(API_KEY="", GOLDEN_ROLES=SERVICE_ROLES)
class PerKeyAuthTests(TestCase):
    def setUp(self):
        # The token endpoint is throttled per IP in the shared cache.
        cache.clear()
        self.addCleanup(cache.clear)
        self.svc = ensure_service_account("crm", ["crm-sync"])
        self.key, self.raw = issue_api_key(self.svc, name="crm")

    def _client(self, raw=None):
        return APIClient(HTTP_AUTHORIZATION=f"Api-Key {raw or self.raw}")

    def _token(self, **body):
        response = self._client().post(TOKEN_URL, body, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    # provisioning
    def test_account_is_a_service_not_a_human(self):
        self.assertEqual(self.svc.username, "svc-crm")
        self.assertFalse(self.svc.has_usable_password())
        self.assertFalse(self.svc.is_staff)
        self.assertFalse(self.svc.is_superuser)
        self.assertTrue(is_service_account(self.svc))
        self.assertTrue(self.raw.startswith(f"gk_{self.key.prefix}_"))
        self.assertNotIn(self.raw, self.key.key_hash)

    def test_cannot_key_a_human_or_take_a_human_role(self):
        human = make_staff_user("bea", password="pw")
        with self.assertRaises(ValueError):
            issue_api_key(human, name="bea")
        with self.assertRaises(ValueError):
            ensure_service_account("ops", ["staff"])
        User.objects.create_user("svc-taken", password="pw")
        with self.assertRaises(ValueError):
            ensure_service_account("taken", ["crm-sync"])

    # contract
    def test_key_reaches_exactly_its_contract(self):
        client = self._client()
        self.assertEqual(client.get(SUPPLIERS).status_code, 200)
        self.assertEqual(client.post(SUPPLIERS, {}, format="json").status_code, 403)
        self.assertEqual(client.get("/api/v1/orders/orders/").status_code, 403)
        self.assertEqual(client.get("/api/v1/core/companies/").status_code, 403)

    def test_company_pin_scopes_rows(self):
        from purchasing.models import Supplier

        a, b = Company.objects.create(name="A"), Company.objects.create(name="B")
        Supplier.objects.create(supplier_code="SA", company_name="Sup A", company=a)
        Supplier.objects.create(supplier_code="SB", company_name="Sup B", company=b)
        self.assertEqual(self._client().get(SUPPLIERS).json()["count"], 0)  # fail closed
        ensure_service_account("crm", company=a)
        body = self._client().get(SUPPLIERS).json()
        self.assertEqual([r["supplier_code"] for r in body["results"]], ["SA"])

    def test_unpinned_account_cannot_write_into_any_company(self):
        from purchasing.models import Supplier

        writer_roles = SERVICE_ROLES + [
            {"name": "sup-writer", "label": "Supplier writer", "kind": "service",
             "permissions": ["purchasing.view_supplier", "purchasing.add_supplier"]},
        ]
        with override_settings(GOLDEN_ROLES=writer_roles):
            a, b = Company.objects.create(name="A"), Company.objects.create(name="B")
            svc = ensure_service_account("writer", ["sup-writer"])
            _, raw = issue_api_key(svc, name="writer")
            body = {"supplier_code": "X1", "company_name": "Evil", "supplier_type": "distributor",
                    "company": str(b.pk)}
            response = self._client(raw).post(SUPPLIERS, body, format="json")
            self.assertEqual(response.status_code, 403, response.content)
            self.assertFalse(Supplier.objects.filter(supplier_code="X1").exists())
            # Pinned to A: writing into B is a 400, into A succeeds.
            ensure_service_account("writer", company=a)
            response = self._client(raw).post(SUPPLIERS, body, format="json")
            self.assertEqual(response.status_code, 400, response.content)
            body["company"] = str(a.pk)
            response = self._client(raw).post(SUPPLIERS, body, format="json")
            self.assertEqual(response.status_code, 201, response.content)
            self.assertEqual(Supplier.objects.get(supplier_code="X1").company, a)

    def test_escalated_account_is_refused_not_promoted(self):
        # Made staff (role), superuser, given a password, or deactivated:
        # its keys stop working rather than acting with the new power.
        from core.roles import grant_role

        def reset():
            User.objects.filter(pk=self.svc.pk).update(
                is_staff=False, is_superuser=False, is_active=True, password="!")
            self.svc.groups.remove(*self.svc.groups.filter(name__in=["staff", "manager"]))

        for escalate in (
            lambda u: grant_role(u, "manager"),
            lambda u: User.objects.filter(pk=u.pk).update(is_superuser=True),
            lambda u: (u.set_password("pw"), u.save()),
            lambda u: User.objects.filter(pk=u.pk).update(is_active=False),
        ):
            with self.subTest(escalate=escalate):
                user = User.objects.get(pk=self.svc.pk)
                escalate(user)
                self.assertEqual(self._client().get(SUPPLIERS).status_code, 401)
                reset()
        self.assertEqual(self._client().get(SUPPLIERS).status_code, 200)

    def test_service_in_a_staff_group_is_still_held_to_its_contract(self):
        # Even past the resolver (e.g. a session), PrincipalPermission checks
        # service accounts before superuser/staff.
        from rest_framework.test import APIRequestFactory

        from core.permissions import PrincipalPermission
        from orders.views import OrderViewSet

        self.svc.is_superuser = True
        request = APIRequestFactory().get("/api/v1/orders/orders/")
        request.user = self.svc
        view = OrderViewSet()
        view.action = "list"
        self.assertFalse(PrincipalPermission().has_permission(request, view))

    # revocation / expiry
    def test_revoked_key_and_its_bearers_are_refused(self):
        token = self._token()["token"]
        bearer = APIClient(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(bearer.get(SUPPLIERS).status_code, 200)
        revoke(ApiKey.objects.filter(pk=self.key.pk))
        self.assertEqual(self._client().get(SUPPLIERS).status_code, 401)
        self.assertEqual(bearer.get(SUPPLIERS).status_code, 401)
        self.assertEqual(self._client().post(TOKEN_URL, {}, format="json").status_code, 401)

    def test_expired_key_and_its_bearers_are_refused(self):
        token = self._token()["token"]
        ApiKey.objects.filter(pk=self.key.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self._client().get(SUPPLIERS).status_code, 401)
        self.assertEqual(
            APIClient(HTTP_AUTHORIZATION=f"Bearer {token}").get(SUPPLIERS).status_code, 401)

    def test_wrong_secret_or_unknown_prefix_is_401(self):
        self.assertEqual(self._client(self.raw[:-2] + "xx").get(SUPPLIERS).status_code, 401)
        self.assertEqual(self._client("gk_00000000_nope").get(SUPPLIERS).status_code, 401)

    def test_last_used_is_recorded_cheaply(self):
        from core.service_accounts import resolve_api_key

        request = _rf().get(SUPPLIERS)
        with self.assertNumQueries(2):  # key lookup (with user) + one UPDATE
            resolve_api_key(self.raw, request)
        self.key.refresh_from_db()
        first = self.key.last_used_at
        self.assertIsNotNone(first)
        with self.assertNumQueries(1):  # within the window: lookup only, no write
            resolve_api_key(self.raw, request)
        self._client().get(SUPPLIERS)
        self.key.refresh_from_db()
        self.assertEqual(self.key.last_used_at, first)  # within LAST_USED_RESOLUTION
        ApiKey.objects.filter(pk=self.key.pk).update(last_used_at=first - LAST_USED_RESOLUTION * 2)
        self._client().get(SUPPLIERS)
        self.key.refresh_from_db()
        self.assertGreater(self.key.last_used_at, first - LAST_USED_RESOLUTION)

    # bearers
    def test_token_carries_the_service_subject_and_key(self):
        body = self._token()
        payload = jwt.decode(body["token"], get_jwt_signing_key(), algorithms=["HS256"])
        self.assertEqual((payload["sub"], payload["akp"]), ("svc-crm", self.key.prefix))
        self.assertEqual(body["subject"], "svc-crm")

    def test_key_cannot_mint_another_subject(self):
        for subject in ("api-service", "storefront", "svc-other"):
            with self.subTest(subject=subject):
                response = self._client().post(TOKEN_URL, {"subject": subject}, format="json")
                self.assertEqual(response.status_code, 400)

    def test_bearer_needs_its_own_accounts_key(self):
        other = ensure_service_account("other", ["crm-sync"])
        other_key, _ = issue_api_key(other, name="other")
        for payload in (
            {"sub": "svc-crm"},                                  # no akp
            {"sub": "svc-crm", "akp": other_key.prefix},         # someone else's key
            {"sub": "svc-crm", "akp": "zzzzzzzz"},
        ):
            with self.subTest(payload=payload):
                token = _signed(payload)
                self.assertEqual(
                    APIClient(HTTP_AUTHORIZATION=f"Bearer {token}").get(SUPPLIERS).status_code, 401)

    def test_svc_token_signed_with_api_key_is_refused(self):
        with override_settings(API_KEY=API_KEY):
            token = _signed({"sub": "svc-crm", "akp": self.key.prefix}, key=API_KEY)
            self.assertEqual(
                APIClient(HTTP_AUTHORIZATION=f"Bearer {token}").get(SUPPLIERS).status_code, 401)

    def test_token_never_outlives_its_key(self):
        ApiKey.objects.filter(pk=self.key.pk).update(expires_at=timezone.now() + timedelta(seconds=300))
        body = self._token(lifetime_s=3600)
        self.assertLessEqual(body["expires_in_s"], 300)
        self.key.refresh_from_db()
        with self.assertRaises(ValueError):
            issue_api_jwt(sub="svc-crm", key=self.key, lifetime_s=3600)

    # middleware
    def test_middleware_shares_the_resolver(self):
        from django.contrib.auth.models import AnonymousUser

        from core.middleware import APIKeyMiddleware

        seen = {}

        def view(request):
            seen["user"] = request.user
            return None

        middleware = APIKeyMiddleware(view)
        for raw, expected in ((self.raw, "svc-crm"), ("gk_00000000_nope", None)):
            request = RequestFactory().get(SUPPLIERS, HTTP_AUTHORIZATION=f"Api-Key {raw}")
            request.user = AnonymousUser()
            middleware(request)
            self.assertEqual(getattr(seen["user"], "username", None) or None, expected)
        revoke(ApiKey.objects.filter(pk=self.key.pk))
        request = RequestFactory().get(SUPPLIERS, HTTP_AUTHORIZATION=f"Api-Key {self.raw}")
        request.user = AnonymousUser()
        middleware(request)
        self.assertFalse(seen["user"].is_authenticated)


def _agent_plugin():
    from core.plugins import BasePlugin, TokenScope

    def _check(request, payload):
        return True

    class P(BasePlugin):
        name = "svc_scope_plugin"

        def get_token_scopes(self):
            return [
                TokenScope(name="agent", path_prefix="/api/v1/agent/"),
                TokenScope(name="checked", path_prefix="/api/v1/checked/", check=_check),
            ]

    return P


@override_settings(API_KEY="", GOLDEN_ROLES=SERVICE_ROLES, GOLDEN_PLUGINS=[_agent_plugin()])
class ScopedKeyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.svc = ensure_service_account("agent", ["crm-sync"])
        self.key, self.raw = issue_api_key(self.svc, name="agent", scope="agent")

    def _auth(self, path, raw=None):
        request = _rf().get(path, HTTP_AUTHORIZATION=f"Api-Key {raw or self.raw}")
        return APIKeyAuthentication().authenticate(request)

    def test_scoped_key_is_path_bound(self):
        self.assertEqual(self._auth("/api/v1/agent/x/")[0], self.svc)
        for path in ("/api/v1/purchasing/suppliers/", "/api/v1/agent/../purchasing/"):
            with self.subTest(path=path), self.assertRaises(AuthenticationFailed):
                self._auth(path)

    def test_scoped_key_mints_only_its_scope(self):
        client = APIClient(HTTP_AUTHORIZATION=f"Api-Key {self.raw}")
        ok = client.post(TOKEN_URL, {}, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(ok.json()["scope"], "agent")
        self.assertEqual(client.post(TOKEN_URL, {"scope": "api"}, format="json").status_code, 400)

    def test_checked_scope_key_must_use_a_bearer(self):
        _, raw = issue_api_key(self.svc, name="checked", scope="checked")
        with self.assertRaises(AuthenticationFailed):
            self._auth("/api/v1/checked/x/", raw=raw)

    def test_full_scope_key_cannot_hide_behind_bearer_scope(self):
        # A bearer whose scope differs from a scoped key's scope is refused.
        token = _signed({"sub": "svc-agent", "akp": self.key.prefix, "scope": "api"})
        with self.assertRaises(AuthenticationFailed):
            BearerTokenAuthentication().authenticate(
                _rf().get("/api/v1/purchasing/suppliers/", HTTP_AUTHORIZATION=f"Bearer {token}"))


@override_settings(API_KEY="", GOLDEN_ROLES=SERVICE_ROLES)
class ApiKeyCommandTests(TestCase):
    def test_create_prints_the_key_once_and_stores_its_hash(self):
        out, err = StringIO(), StringIO()
        call_command("create_api_key", "--name", "crm", "--role", "crm-sync",
                     "--expires-in", "90d", stdout=out, stderr=err)
        raw = out.getvalue().strip()
        key = ApiKey.objects.get(name="crm")
        self.assertTrue(raw.startswith(f"gk_{key.prefix}_"))
        self.assertNotIn(raw, err.getvalue())
        self.assertNotEqual(key.key_hash, raw)
        self.assertAlmostEqual(
            (key.expires_at - timezone.now()).total_seconds(), 90 * 86400, delta=60)
        self.assertEqual(key.user.username, "svc-crm")
        self.assertEqual(APIClient(HTTP_AUTHORIZATION=f"Api-Key {raw}").get(SUPPLIERS).status_code, 200)

    def test_new_account_needs_a_role_and_roles_must_be_service(self):
        with self.assertRaises(CommandError):
            call_command("create_api_key", "--name", "x", stdout=StringIO(), stderr=StringIO())
        with self.assertRaises(CommandError):
            call_command("create_api_key", "--name", "x", "--role", "manager",
                         stdout=StringIO(), stderr=StringIO())
        self.assertFalse(User.objects.filter(username="svc-x").exists())

    def test_company_pin(self):
        company = Company.objects.create(name="Acme")
        call_command("create_api_key", "--name", "crm", "--role", "crm-sync",
                     "--company", "Acme", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(UserProfile.objects.get(user__username="svc-crm").company, company)

    def test_company_required_once_several_exist(self):
        Company.objects.create(name="One")
        Company.objects.create(name="Two")
        with self.assertRaisesMessage(CommandError, "--company"):
            call_command("create_api_key", "--name", "crm", "--role", "crm-sync",
                         stdout=StringIO(), stderr=StringIO())
        self.assertFalse(User.objects.filter(username="svc-crm").exists())
        call_command("create_api_key", "--name", "crm", "--role", "crm-sync",
                     "--company", "One", stdout=StringIO(), stderr=StringIO())
        # A second key for the already-pinned account needs no --company.
        call_command("create_api_key", "--name", "crm", "--key-name", "crm-2",
                     stdout=StringIO(), stderr=StringIO())
        self.assertEqual(ApiKey.objects.filter(user__username="svc-crm").count(), 2)

    def test_revoke_by_prefix_name_and_service(self):
        call_command("create_api_key", "--name", "crm", "--role", "crm-sync",
                     stdout=StringIO(), stderr=StringIO())
        call_command("create_api_key", "--name", "crm", "--key-name", "crm-2",
                     stdout=StringIO(), stderr=StringIO())
        first = ApiKey.objects.get(name="crm")
        call_command("revoke_api_key", first.prefix, stdout=StringIO())
        first.refresh_from_db()
        self.assertIsNotNone(first.revoked_at)
        self.assertIsNone(ApiKey.objects.get(name="crm-2").revoked_at)
        call_command("revoke_api_key", "--service", "crm", stdout=StringIO())
        self.assertFalse(ApiKey.objects.filter(revoked_at__isnull=True).exists())
        with self.assertRaises(CommandError):
            call_command("revoke_api_key", "--name", "nope", stdout=StringIO())


@override_settings(API_KEY="", GOLDEN_ROLES=SERVICE_ROLES)
class BackfillTenancyServiceAccountTests(TestCase):
    def test_service_accounts_get_no_profile(self):
        from core.storefront import ensure_storefront_identity

        company = Company.objects.create(name="Solo")
        svc = ensure_service_account("crm", ["crm-sync"])
        storefront = ensure_storefront_identity()
        api_service = User.objects.get(username="api-service")
        human = make_staff_user("clerk", password="pw")
        call_command("backfill_tenancy", "--company", str(company.pk), stdout=StringIO())
        for user in (svc, storefront, api_service):
            with self.subTest(user=user.username):
                self.assertFalse(UserProfile.objects.filter(user=user).exists())
        self.assertTrue(UserProfile.objects.filter(user=human, company=company).exists())
