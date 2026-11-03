"""Release hardening 6.2: auth flows, run as one regression set.

What this module adds; the rest of 6.2 is already pinned and only cited here:

- staff login throttle shape: the form never consumes budget, the key is
  per IP + username, a 429 never deactivates the account
  (`core/tests/test_m3_ratelimit.py` pins the burst itself);
- logout invalidates the server-side session, not just the cookie
  (`test_m2d_auth_ux.py` checks the client is signed out);
- password reset: an expired link is refused and cannot set a password
  (`test_m2d_auth_ux.py` covers request, set, reused and garbage links);
- portal refusal codes over HTTP: 503 `portal_not_configured`, 403
  `portal_account_closed` and `portal_account_collision`
  (`test_portal_auth.py` drives 401/409 over the API and every code at
  the resolver; `test_portal_party.py` pins the 503 at the resolver);
- no API caller resolves to a human: every principal the two API
  authenticators return is a service account with no usable password and
  no staff flag, except the dedicated `api-service` superuser
  (`test_authentication.py` pins the resolver, revoked and expired keys,
  the unpinned write refusal; `test_storefront_identity.py` the storefront
  key's add-only contract).

Cited, not repeated: OIDC start/callback with the stub issuer (good token,
bad signature, wrong `iss`/`aud`, expired, replayed `state`, nonce, PKCE,
HS256) in `core/tests/test_oidc.py` `SessionLoginTests`; the staff gate on
every `/erp/` route in `core/tests/test_erp_staff_only.py`, extended to
plugin routes and every principal by the access matrix (PR #133).
"""

from __future__ import annotations

import datetime
import secrets
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import PasswordResetTokenGenerator, default_token_generator
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APIClient, APIRequestFactory

from core.authentication import APIKeyAuthentication, BearerTokenAuthentication
from core.identity.resolve import CODE_CLOSED, CODE_COLLISION, CODE_NOT_CONFIGURED
from core.models import Company
from core.service_accounts import ensure_service_account, is_service_account, issue_api_key
from core.tests.helpers import make_portal_customer, make_staff_user, portal_tenant
from core.throttling import LOGIN_ATTEMPTS_PER_MINUTE

User = get_user_model()
LOGIN = "/erp/login/"


class LoginThrottleShapeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        make_staff_user("throttled", password="right-pw-1")

    def _bad(self, username, n):
        last = None
        for _ in range(n):
            last = self.client.post(LOGIN, {"username": username, "password": "wrong"})
        return last

    def test_rendering_the_form_never_consumes_budget(self):
        for _ in range(LOGIN_ATTEMPTS_PER_MINUTE + 5):
            self.assertEqual(self.client.get(LOGIN).status_code, 200)
        response = self.client.post(LOGIN, {"username": "throttled", "password": "right-pw-1"})
        self.assertEqual(response.status_code, 302)

    def test_key_is_per_username_so_one_target_does_not_lock_another(self):
        self.assertEqual(self._bad("throttled", LOGIN_ATTEMPTS_PER_MINUTE + 1).status_code, 429)
        other = self.client.post(LOGIN, {"username": "someone-else", "password": "wrong"})
        self.assertEqual(other.status_code, 200)  # a normal re-render, not 429

    def test_throttle_never_deactivates_the_account_and_a_429_carries_no_form(self):
        response = self._bad("throttled", LOGIN_ATTEMPTS_PER_MINUTE + 1)
        self.assertEqual(response.status_code, 429)
        self.assertNotIn(b"csrfmiddlewaretoken", response.content)
        self.assertTrue(User.objects.get(username="throttled").is_active)


class LogoutInvalidatesSessionTests(TestCase):
    def test_a_replayed_session_cookie_is_dead_after_logout(self):
        user = make_staff_user("leaver", password="pw-leaver-1")
        self.client.force_login(user)
        key = self.client.session.session_key
        self.assertTrue(Session.objects.filter(session_key=key).exists())
        self.assertEqual(self.client.get(reverse("erp-dashboard")).status_code, 200)

        self.assertEqual(self.client.post(reverse("erp-logout")).status_code, 302)
        self.assertFalse(Session.objects.filter(session_key=key).exists())

        # Someone who copied the cookie before logout gets the login page.
        self.client.cookies[settings.SESSION_COOKIE_NAME] = key
        response = self.client.get(reverse("erp-dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/erp/login/", response["Location"])

    def test_logout_is_post_only(self):
        self.client.force_login(make_staff_user("stay", password="pw-stay-1"))
        self.assertEqual(self.client.get(reverse("erp-logout")).status_code, 405)
        self.assertEqual(self.client.get(reverse("erp-dashboard")).status_code, 200)


class PasswordResetExpiryTests(TestCase):
    def setUp(self):
        self.user = make_staff_user("resetter", email="resetter@example.com", password="pw-old-1")

    def _link(self, token):
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        return reverse("erp-password-reset-confirm", kwargs={"uidb64": uid, "token": token})

    def test_an_expired_link_is_refused_and_sets_nothing(self):
        stale = datetime.datetime.now() - datetime.timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT + 60)
        with mock.patch.object(PasswordResetTokenGenerator, "_now", return_value=stale):
            token = default_token_generator.make_token(self.user)
        self.assertFalse(default_token_generator.check_token(self.user, token))

        response = self.client.get(self._link(token))
        self.assertEqual(response.status_code, 200)  # the "link is invalid" page, no redirect
        self.assertNotContains(response, "new_password1")

        response = self.client.post(self._link("set-password"),
                                    {"new_password1": "Fresh-pass-9!", "new_password2": "Fresh-pass-9!"})
        self.assertLess(response.status_code, 500)
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password("Fresh-pass-9!"))
        self.assertTrue(self.user.check_password("pw-old-1"))

    def test_a_token_from_before_a_password_change_is_dead(self):
        token = default_token_generator.make_token(self.user)
        self.user.set_password("pw-new-2")
        self.user.save()
        self.assertFalse(default_token_generator.check_token(self.user, token))
        response = self.client.get(self._link(token))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "new_password1")


SECRET = "test-supabase-" + secrets.token_hex(16)
ORDERS_URL = "/api/v1/portal/customer/orders/"


def _token(**claims):
    from core.tests.test_portal_auth import make_token

    return make_token(secret=SECRET, **claims)


@override_settings(SUPABASE_JWT_SECRET=SECRET)
class PortalRefusalHttpTests(TestCase):
    """The 503 and the two 403s, each reaching the client with its stable
    ``code`` (the storefront branches on the code, never the message)."""

    def _get(self, token):
        return APIClient(HTTP_AUTHORIZATION=f"Bearer {token}").get(ORDERS_URL)

    def test_not_configured_is_503_with_code(self):
        Company.objects.create(name="A")
        Company.objects.create(name="B")
        from core.tests.test_portal_auth import provider_confirms

        with provider_confirms(True):
            response = self._get(_token(email="who@example.com", email_verified=True))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], CODE_NOT_CONFIGURED)

    def test_closed_account_is_403_with_code(self):
        portal_tenant()
        customer, subject = make_portal_customer("gone@example.com")
        User.objects.filter(pk=customer.user_id).update(is_active=False)
        response = self._get(_token(sub=subject))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], CODE_CLOSED)

    def test_staff_held_identity_is_403_collision(self):
        portal_tenant()
        customer, subject = make_portal_customer("promoted@example.com")
        user = User.objects.get(pk=customer.user_id)
        user.is_staff = True
        user.save()
        response = self._get(_token(sub=subject))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], CODE_COLLISION)


SERVICE_ROLES = [
    {"name": "crm-sync", "label": "CRM sync", "kind": "service",
     "permissions": ["purchasing.view_supplier"]},
]
API_KEY = "test-legacy-" + secrets.token_hex(16)
SUPPLIERS = "/api/v1/purchasing/suppliers/"
TOKEN_URL = "/api/v1/core/auth/token/"


@override_settings(API_KEY=API_KEY, GOLDEN_ROLES=SERVICE_ROLES)
class NoApiCallerIsHumanTests(TestCase):
    """Every principal an API authenticator returns is a service account:
    unusable password, not staff, not a superuser. The one exception is the
    legacy key's dedicated `api-service` superuser, which is still never a
    human account and never one with a password."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.human = make_staff_user("a-human", password="pw-human-1")
        User.objects.create_superuser("root-human", "root@example.com", "pw-root-1")
        self.svc = ensure_service_account("crm", ["crm-sync"])
        self.key, self.raw = issue_api_key(self.svc, name="crm")

    def _principal(self, authenticator, **headers):
        request = APIRequestFactory().get(SUPPLIERS, **headers)
        result = authenticator().authenticate(request)
        self.assertIsNotNone(result)
        return result[0]

    def assert_not_human(self, user):
        self.assertTrue(is_service_account(user), user.username)
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertNotIn(user.pk, {self.human.pk})

    def test_per_key_principal_is_a_service(self):
        self.assert_not_human(self._principal(APIKeyAuthentication, HTTP_AUTHORIZATION=f"Api-Key {self.raw}"))

    def test_bearer_minted_from_a_key_is_the_same_service(self):
        response = APIClient(HTTP_AUTHORIZATION=f"Api-Key {self.raw}").post(TOKEN_URL, {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        token = response.json()["token"]
        user = self._principal(BearerTokenAuthentication, HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assert_not_human(user)
        self.assertEqual(user.pk, self.svc.pk)

    def test_legacy_key_is_the_dedicated_api_service_never_a_human_superuser(self):
        user = self._principal(APIKeyAuthentication, HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")
        self.assertEqual(user.username, "api-service")
        self.assertFalse(user.has_usable_password())
        self.assertNotEqual(user.username, "root-human")
        self.assertFalse(User.objects.filter(username="root-human", pk=user.pk).exists())

    def test_a_human_cannot_be_keyed(self):
        with self.assertRaises(ValueError):
            issue_api_key(self.human, name="nope")
