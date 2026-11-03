"""Portal invites on ``UserIdentity`` (identity step 4) and the F-032 resend.

POST /api/v1/portal/invite/ creates the customer's ``User`` if it has none,
asks the provider to invite (Supabase Admin, mocked here) and stores the
returned subject as ``UserIdentity`` (nothing is dual-written since identity
step 8 dropped ``Customer.supabase_user_id``). POST /api/v1/portal/invite/resend/ needs that identity and is
rate-limited. These tests pin the view contract, not the provider.
"""

import uuid
from unittest import mock, skipUnless

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext, override_settings
from rest_framework.test import APIClient

from core.identity.models import UserIdentity
from core.models import Company
from core.tests.helpers import (
    LOGIN_LOCK_ORDER,
    login_lock_order,
    make_portal_customer,
    make_staff_user,
)
from customers.models import Customer

RESEND_URL = "/api/v1/portal/invite/resend/"
INVITE_URL = "/api/v1/portal/invite/"
SUPABASE_OVERRIDES = {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "svc-role-key",
}


def _supabase_ok(uid):
    response = mock.Mock()
    response.status_code = 200
    response.json.return_value = {"id": str(uid)}
    response.text = ""
    return response


@override_settings(**SUPABASE_OVERRIDES)
class _Base(TestCase):
    def setUp(self):
        # Throttle budget is per admin user and the cache survives across
        # tests in-process — reset so each test starts clean.
        cache.clear()
        self.company = Company.objects.create(name="Example Co")
        self.admin = make_staff_user("invite-admin", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def _plain_customer(self, **overrides):
        data = {"first_name": "A", "last_name": "B", "email": "portal@example.com",
                "company": self.company}
        data.update(overrides)
        return Customer.objects.create(**data)

    def _portal_customer(self, email="portal@example.com"):
        return make_portal_customer(email, company=self.company)


class PortalInviteTests(_Base):
    def test_invite_creates_user_identity_and_role(self):
        customer = self._plain_customer()
        new_uid = uuid.uuid4()
        with mock.patch("httpx.post", return_value=_supabase_ok(new_uid)) as post:
            response = self.client.post(INVITE_URL, {"customer_id": customer.pk}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertIn("/auth/v1/admin/invite", post.call_args.args[0])
        customer.refresh_from_db()
        user = customer.user
        self.assertIsNotNone(user)
        self.assertTrue(user.username.startswith("u-"))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.has_usable_password())
        self.assertTrue(user.groups.filter(name="customer").exists())
        identity = UserIdentity.objects.get(user=user)
        self.assertEqual((identity.provider, identity.subject), ("supabase", str(new_uid)))
        self.assertIsNone(identity.last_login_at)  # invited, not signed in yet
        self.assertFalse(hasattr(customer, "supabase_user_id"))
        # The response key survives for API compatibility: it is the subject.
        self.assertEqual(response.data["supabase_user_id"], str(new_uid))
        self.assertEqual(response.data["subject"], str(new_uid))
        self.assertEqual(response.data["user_id"], user.pk)

    def test_invite_attaches_to_existing_non_staff_user(self):
        user = User.objects.create_user("existing-shopper")
        customer = self._plain_customer(user=user)
        new_uid = uuid.uuid4()
        with mock.patch("httpx.post", return_value=_supabase_ok(new_uid)):
            response = self.client.post(INVITE_URL, {"customer_id": customer.pk}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(UserIdentity.objects.get(subject=str(new_uid)).user, user)
        self.assertTrue(user.groups.filter(name="customer").exists())

    @skipUnless(connection.features.has_select_for_update, "FOR UPDATE is a no-op on this backend")
    def test_invite_locks_customer_then_user_without_a_join(self):
        """resolve.lock_users: a joined user row would predate the lock wait."""
        user = User.objects.create_user("lock-invitee")
        customer = self._plain_customer(user=user)
        with mock.patch("httpx.post", return_value=_supabase_ok(uuid.uuid4())), \
                CaptureQueriesContext(connection) as queries:
            response = self.client.post(INVITE_URL, {"customer_id": customer.pk}, format="json")
        self.assertEqual(response.status_code, 201)
        locks = [q["sql"] for q in queries if "FOR UPDATE" in q["sql"]]
        self.assertTrue(any('FROM "customers_customer"' in sql for sql in locks))
        self.assertTrue(any('FROM "auth_user"' in sql for sql in locks))
        for sql in locks:
            self.assertNotIn(" JOIN ", sql)
        self.assertEqual(login_lock_order(queries), list(LOGIN_LOCK_ORDER))

    def test_invite_409_when_identity_exists(self):
        customer, sub = self._portal_customer()
        with mock.patch("httpx.post") as post:
            response = self.client.post(INVITE_URL, {"customer_id": customer.pk}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["supabase_user_id"], sub)
        post.assert_not_called()

    def test_invite_refused_for_staff_held_customer(self):
        staff = make_staff_user("clerk", email="clerk@example.com")
        customer = self._plain_customer(email="clerk@example.com", user=staff)
        with mock.patch("httpx.post") as post:
            response = self.client.post(INVITE_URL, {"customer_id": customer.pk}, format="json")
        self.assertEqual(response.status_code, 409)
        post.assert_not_called()
        self.assertFalse(staff.identities.exists())

    def test_invite_refuses_a_subject_owned_by_another_user(self):
        _other, other_sub = self._portal_customer("other@example.com")
        customer = self._plain_customer()
        with mock.patch("httpx.post", return_value=_supabase_ok(other_sub)):
            response = self.client.post(INVITE_URL, {"customer_id": customer.pk}, format="json")
        self.assertEqual(response.status_code, 409)
        customer.refresh_from_db()
        self.assertIsNone(customer.user)

    def test_identity_attached_concurrently_is_never_replaced(self):
        """The 409 pre-check is repeated under the row lock (review P3)."""
        customer, sub = self._portal_customer()
        with mock.patch("core.portal_views._portal_identity", return_value=None), \
                mock.patch("httpx.post", return_value=_supabase_ok(uuid.uuid4())):
            response = self.client.post(INVITE_URL, {"customer_id": customer.pk}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(list(customer.user.identities.values_list("subject", flat=True)), [sub])

    def test_provider_failure_creates_nothing(self):
        customer = self._plain_customer()
        failed = mock.Mock(status_code=422, text="email exists")
        with mock.patch("httpx.post", return_value=failed):
            response = self.client.post(INVITE_URL, {"customer_id": customer.pk}, format="json")
        self.assertEqual(response.status_code, 502)
        customer.refresh_from_db()
        self.assertIsNone(customer.user)

    @override_settings(SUPABASE_SERVICE_ROLE_KEY="")
    def test_missing_service_key_is_500(self):
        customer = self._plain_customer()
        with mock.patch("httpx.post") as post:
            response = self.client.post(INVITE_URL, {"customer_id": customer.pk}, format="json")
        self.assertEqual(response.status_code, 500)
        post.assert_not_called()

    @override_settings(GOLDEN_IDENTITY_PROVIDERS=[])
    def test_no_invite_provider_is_500(self):
        customer = self._plain_customer()
        response = self.client.post(INVITE_URL, {"customer_id": customer.pk}, format="json")
        self.assertEqual(response.status_code, 500)

    def test_invite_rejects_non_staff(self):
        api = APIClient()
        api.force_authenticate(user=User.objects.create_user(username="pleb"))
        customer = self._plain_customer()
        with mock.patch("httpx.post") as post:
            response = api.post(INVITE_URL, {"customer_id": customer.pk}, format="json")
        self.assertEqual(response.status_code, 403)
        post.assert_not_called()


class PortalInviteResendTests(_Base):
    def test_resend_ok_returns_200_with_resent_flag(self):
        customer, sub = self._portal_customer()
        with mock.patch("httpx.post", return_value=_supabase_ok(sub)) as post:
            response = self.client.post(RESEND_URL, {"customer_id": customer.pk}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["resent"])
        self.assertEqual(response.data["supabase_user_id"], sub)
        post.assert_called_once()
        self.assertIn("/auth/v1/admin/invite", post.call_args.args[0])

    def test_resend_requires_existing_identity(self):
        customer = self._plain_customer(user=User.objects.create_user("no-identity"))
        with mock.patch("httpx.post") as post:
            response = self.client.post(RESEND_URL, {"customer_id": customer.pk}, format="json")
        self.assertEqual(response.status_code, 409)
        post.assert_not_called()

    def test_resend_never_relinks(self):
        customer, sub = self._portal_customer()
        with mock.patch("httpx.post", return_value=_supabase_ok(uuid.uuid4())):
            response = self.client.post(RESEND_URL, {"customer_id": customer.pk}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(customer.user.identities.values_list("subject", flat=True)), [sub])

    def test_resend_unknown_customer_404(self):
        with mock.patch("httpx.post") as post:
            response = self.client.post(RESEND_URL, {"customer_id": str(uuid.uuid4())},
                                        format="json")
        self.assertEqual(response.status_code, 404)
        post.assert_not_called()

    def test_resend_customer_without_email_400(self):
        customer = self._plain_customer(email="")
        with mock.patch("httpx.post") as post:
            response = self.client.post(RESEND_URL, {"customer_id": customer.pk}, format="json")
        self.assertEqual(response.status_code, 400)
        post.assert_not_called()

    def test_resend_is_rate_limited(self):
        customer, sub = self._portal_customer()
        with mock.patch("httpx.post", return_value=_supabase_ok(sub)):
            codes = [
                self.client.post(RESEND_URL, {"customer_id": customer.pk},
                                 format="json").status_code
                for _ in range(11)
            ]
        self.assertEqual(codes[:10], [200] * 10)
        self.assertEqual(codes[10], 429)

    def test_resend_rejects_non_staff(self):
        api = APIClient()
        api.force_authenticate(user=User.objects.create_user(username="pleb"))
        customer, _sub = self._portal_customer()
        with mock.patch("httpx.post") as post:
            response = api.post(RESEND_URL, {"customer_id": customer.pk}, format="json")
        self.assertEqual(response.status_code, 403)
        post.assert_not_called()
