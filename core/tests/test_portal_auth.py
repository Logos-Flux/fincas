"""Portal token authentication and the step-4 resolver (identity §3.4).

One class per state of the portal-auth table (``docs/crm/phase2-party.md``
§2.6, as built on Customer in identity step 4):

- linked     ``UserIdentity(provider, sub)`` → its user and Customer
- match-0    no candidate → User + Customer + identity + ``customer`` role
- match-1    one candidate, verified email → linked (User created if needed)
- unverified a candidate exists, email not verified → 401 portal_verify_email
- match-N    several candidates, verified → 409 portal_account_ambiguous
- collision  the candidate's user is staff and the provider untrusted, or the
             provider may not link by email → 403 portal_account_collision
- closed     user or Customer inactive → 403 portal_account_closed

``request.user`` is the real ``auth.User``; there is no proxy class.
"""

import secrets
import time
import uuid
from unittest import mock

import jwt
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIClient, APIRequestFactory

from core.identity.models import UserIdentity
from core.identity.resolve import (
    CODE_AMBIGUOUS,
    CODE_CLOSED,
    CODE_COLLISION,
    CODE_NO_ACCOUNT,
    CODE_VERIFY_EMAIL,
    PortalAuthFailed,
    PortalRefused,
)
from core.models import AuditLog
from core.portal_auth import PortalTokenAuthentication
from core.tests.helpers import make_portal_customer, make_staff_user, portal_tenant
from customers.models import Customer
from parties.models import Party, PartyEmail

# Fake secret generated fresh per test run — never a real credential.
SECRET = "test-supabase-" + secrets.token_hex(16)
ORDERS_URL = "/api/v1/portal/customer/orders/"


def supabase_provider(**overrides):
    """The base.py ``supabase`` entry, with overrides (for override_settings)."""
    entry = {
        "name": "supabase",
        "backend": "core.identity.providers.SupabaseJWT",
        "entry_points": ["portal"],
        "self_register": True,
        "roles_on_create": ["customer"],
        "link_by_verified_email": True,
        "options": {
            "jwt_secret_setting": "SUPABASE_JWT_SECRET",
            "jwks_url_setting": "SUPABASE_JWKS_URL",
            "issuer_setting": "SUPABASE_ISSUER",
            "audience": "authenticated",
            "url_setting": "SUPABASE_URL",
            "service_role_key_setting": "SUPABASE_SERVICE_ROLE_KEY",
        },
    }
    entry.update(overrides)
    return entry


def provider_confirms(value=True):
    """Stand in for the Supabase Admin API's answer to "is this address confirmed?".

    Creating or linking an account needs it (``resolve._require_confirmed``).
    ``ProviderConfirmationTests`` drives the real call through a mocked HTTP API.
    """
    from core.identity.providers import SupabaseJWT

    return mock.patch.object(SupabaseJWT, "confirms_email", return_value=value)


def supabase_admin(by_subject=None, default=(404, None)):
    """Mock ``GET /auth/v1/admin/users/<id>``: subject → ``(status, body)``."""
    by_subject = by_subject or {}

    def get(url, **kwargs):
        status_code, body = by_subject.get(url.rsplit("/", 1)[-1], default)
        response = mock.Mock(status_code=status_code)
        response.json.return_value = body or {}
        return response

    return mock.patch("httpx.get", side_effect=get)


def confirmed(email):
    return (200, {"email": email, "email_confirmed_at": "2026-01-01T00:00:00Z"})


ADMIN_API = {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "k"}


def make_token(
    sub=None,
    email=None,
    email_verified=None,
    user_metadata=None,
    aud="authenticated",
    secret=SECRET,
    exp_delta_s=3600,
    **extra,
):
    """Build a Supabase-shaped access token.

    `email_verified` lands in user_metadata (where GoTrue puts it);
    None omits the claim entirely (older/looser token shape).
    """
    payload = {
        "sub": sub or str(uuid.uuid4()),
        "aud": aud,
        "iat": int(time.time()),
        "exp": int(time.time()) + exp_delta_s,
        "role": "authenticated",
    }
    metadata = dict(user_metadata or {})
    if email_verified is not None:
        metadata["email_verified"] = email_verified
    if email is not None:
        payload["email"] = email
    payload["user_metadata"] = metadata
    payload.update(extra)
    return jwt.encode(payload, secret, algorithm="HS256")


def _authenticate(token):
    request = APIRequestFactory().get("/", HTTP_AUTHORIZATION=f"Bearer {token}")
    return PortalTokenAuthentication().authenticate(request)


def _code(exc):
    return exc.detail["code"]


class _Base(TestCase):
    #: The provider's answer to "is the address confirmed?" (None: not patched).
    CONFIRMS = True

    def setUp(self):
        super().setUp()
        portal_tenant()  # Phase 2 step 7: the portal resolves inside one tenant
        if self.CONFIRMS is not None:
            patcher = provider_confirms(self.CONFIRMS)
            patcher.start()
            self.addCleanup(patcher.stop)

    def assertNothingWritten(self, *customers):
        self.assertFalse(UserIdentity.objects.exclude(pk__in=self._identities).exists())
        self.assertFalse(User.objects.filter(username__startswith="u-").exclude(
            pk__in=self._users).exists())
        for customer in customers:
            before = customer.user_id
            customer.refresh_from_db()
            self.assertEqual(customer.user_id, before)

    def snapshot(self):
        """Remember existing identities/users so assertNothingWritten ignores them."""
        self._identities = list(UserIdentity.objects.values_list("pk", flat=True))
        self._users = list(User.objects.values_list("pk", flat=True))
        self._identity_state = list(UserIdentity.objects.order_by("pk").values_list(
            "pk", "last_login_at", "email", "email_verified"))

    def assertIdentitiesUnchanged(self):
        self.assertEqual(
            list(UserIdentity.objects.order_by("pk").values_list(
                "pk", "last_login_at", "email", "email_verified")),
            self._identity_state,
        )

    def assertRefused(self, token, code, exc_type=PortalRefused):
        with self.assertRaises(exc_type) as ctx:
            _authenticate(token)
        self.assertEqual(_code(ctx.exception), code)
        return ctx.exception

    def assertPortalCustomer(self, user):
        self.assertIsInstance(user, User)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.has_usable_password())
        self.assertTrue(user.username.startswith("u-"))
        self.assertTrue(user.groups.filter(name="customer").exists())


@override_settings(SUPABASE_JWT_SECRET=SECRET)
class LinkedTests(_Base):
    """State linked: the identity table is the only link."""

    def test_identity_returns_its_user_and_customer(self):
        customer, sub = make_portal_customer("linked@example.com")
        user, token = _authenticate(make_token(sub=sub))
        self.assertEqual(user, customer.user)
        self.assertEqual(user.customer, customer)
        identity = UserIdentity.objects.get(provider="supabase", subject=sub)
        self.assertIsNotNone(identity.last_login_at)

    def test_identity_wins_over_email_of_other_customer(self):
        linked, sub = make_portal_customer("linked@example.com")
        Customer.objects.create(first_name="O", last_name="C", email="other@example.com")
        user, _ = _authenticate(make_token(sub=sub, email="other@example.com", email_verified=True))
        self.assertEqual(user.customer, linked)

    def test_no_bearer_header_returns_none(self):
        request = APIRequestFactory().get("/")
        self.assertIsNone(PortalTokenAuthentication().authenticate(request))

    def test_missing_sub_raises(self):
        token = jwt.encode(
            {"aud": "authenticated", "exp": int(time.time()) + 3600, "email": "x@example.com"},
            SECRET, algorithm="HS256",
        )
        with self.assertRaises(AuthenticationFailed):
            _authenticate(token)

    def test_wrong_signature_raises(self):
        with self.assertRaises(AuthenticationFailed):
            _authenticate(make_token(secret="wrong-" + secrets.token_hex(8)))

    def test_wrong_audience_raises(self):
        with self.assertRaises(AuthenticationFailed):
            _authenticate(make_token(aud="anon"))

    def test_expired_token_raises(self):
        with self.assertRaises(AuthenticationFailed) as ctx:
            _authenticate(make_token(exp_delta_s=-120))
        self.assertIn("expired", str(ctx.exception).lower())

    @override_settings(SUPABASE_JWT_SECRET=None)
    def test_unconfigured_secret_raises(self):
        with self.assertRaises(AuthenticationFailed) as ctx:
            _authenticate(make_token())
        self.assertIn("not configured", str(ctx.exception))

    @override_settings(GOLDEN_IDENTITY_PROVIDERS=[])
    def test_no_portal_provider_raises(self):
        with self.assertRaises(AuthenticationFailed):
            _authenticate(make_token())

    def test_overlong_subject_is_refused_not_truncated(self):
        with self.assertRaises(AuthenticationFailed):
            _authenticate(make_token(sub="x" * 256, email="long@example.com"))
        self.assertFalse(UserIdentity.objects.exists())


@override_settings(SUPABASE_JWT_SECRET=SECRET)
class Match0Tests(_Base):
    """State match-0: no candidate → User + Customer + identity + role."""

    def test_creates_user_customer_identity_and_role(self):
        sub = str(uuid.uuid4())
        user, _ = _authenticate(make_token(
            sub=sub, email="new.portal@example.com", email_verified=True,
            user_metadata={"first_name": "Nova", "last_name": "Portal"},
        ))
        self.assertPortalCustomer(user)
        self.assertEqual(user.email, "new.portal@example.com")
        customer = Customer.objects.get()
        self.assertEqual(customer.user, user)
        self.assertEqual(customer.email, "new.portal@example.com")
        self.assertEqual((customer.first_name, customer.last_name), ("Nova", "Portal"))
        self.assertEqual(customer.customer_type, Customer.CustomerType.INDIVIDUAL)
        self.assertTrue(customer.is_active)
        identity = UserIdentity.objects.get()
        self.assertEqual((identity.provider, identity.subject, identity.user),
                         ("supabase", sub, user))
        self.assertTrue(identity.email_verified)

    def test_unverified_sign_up_creates_nothing(self):
        """No squatting: an unconfirmed address never gets a Customer (review P2)."""
        self.assertRefused(make_token(email="brandnew@example.com", email_verified=False),
                           CODE_VERIFY_EMAIL, PortalAuthFailed)
        self.assertFalse(Customer.objects.exists())
        self.assertFalse(UserIdentity.objects.exists())

    def test_full_name_metadata_fallback(self):
        user, _ = _authenticate(make_token(
            email="fullname@example.com", email_verified=True,
            user_metadata={"full_name": "Ada Lovelace Byron"},
        ))
        self.assertEqual((user.customer.first_name, user.customer.last_name),
                         ("Ada", "Lovelace Byron"))

    def test_email_local_part_fallback(self):
        user, _ = _authenticate(make_token(email="localpart@example.com", email_verified=True))
        self.assertEqual((user.customer.first_name, user.customer.last_name), ("localpart", ""))

    def test_second_request_is_linked_not_recreated(self):
        token = make_token(email="repeat@example.com", email_verified=True)
        user1, _ = _authenticate(token)
        user2, _ = _authenticate(token)
        self.assertEqual(user1, user2)
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(User.objects.filter(username__startswith="u-").count(), 1)

    def test_missing_email_claim_raises(self):
        self.assertRefused(make_token(), CODE_NO_ACCOUNT, PortalAuthFailed)
        self.assertFalse(User.objects.filter(username__startswith="u-").exists())

    @override_settings(GOLDEN_IDENTITY_PROVIDERS=[supabase_provider(self_register=False)])
    def test_no_self_register_refuses(self):
        self.assertRefused(make_token(email="x@example.com", email_verified=True),
                           CODE_NO_ACCOUNT, PortalAuthFailed)
        self.assertFalse(Customer.objects.exists())

    def test_lost_race_refetches_the_winner(self):
        """A concurrent first login won: the create hits IntegrityError and re-fetches."""
        from core.identity import resolve
        from core.identity.providers import get_provider

        winner, sub = make_portal_customer("race@example.com")
        claims = resolve.Claims(subject=sub, email="race@example.com", email_verified=True,
                                raw={"sub": sub})
        # What the losing request does after its identity lookup missed.
        user, customer = resolve._match_0(get_provider("supabase"), claims)
        self.assertEqual((user, customer), (winner.user, winner))
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(User.objects.filter(username__startswith="u-").count(), 1)


@override_settings(SUPABASE_JWT_SECRET=SECRET)
class Match1Tests(_Base):
    """State match-1: one candidate and a verified email → link."""

    def test_links_customer_without_user(self):
        customer = Customer.objects.create(first_name="Guest", last_name="Shopper",
                                           email="guest@example.com")
        sub = str(uuid.uuid4())
        user, _ = _authenticate(make_token(sub=sub, email="guest@example.com", email_verified=True))
        self.assertPortalCustomer(user)
        customer.refresh_from_db()
        self.assertEqual(customer.user, user)
        self.assertEqual(UserIdentity.objects.get().user, user)
        self.assertEqual(Customer.objects.count(), 1)

    def test_email_link_is_case_insensitive(self):
        customer = Customer.objects.create(first_name="C", last_name="I",
                                           email="Mixed.Case@Example.COM")
        user, _ = _authenticate(make_token(email="mixed.case@example.com", email_verified=True))
        self.assertEqual(user.customer, customer)

    def test_email_link_case_insensitive_other_direction(self):
        customer = Customer.objects.create(first_name="L", last_name="S", email="lower@example.com")
        user, _ = _authenticate(make_token(email="LOWER@EXAMPLE.COM", email_verified=True))
        self.assertEqual(user.customer, customer)

    def test_top_level_email_verified_claim_accepted(self):
        customer = Customer.objects.create(first_name="T", last_name="L",
                                           email="toplevel@example.com")
        token = make_token(email="toplevel@example.com")
        payload = jwt.decode(token, SECRET, algorithms=["HS256"], audience="authenticated")
        payload["email_verified"] = True  # top-level instead of user_metadata
        user, _ = _authenticate(jwt.encode(payload, SECRET, algorithm="HS256"))
        self.assertEqual(user.customer, customer)

    def test_existing_user_without_role_gets_customer_role(self):
        user = User.objects.create_user("shopper-x", "shopper@example.com")
        customer = Customer.objects.create(first_name="S", last_name="X",
                                           email="shopper@example.com", user=user)
        resolved, _ = _authenticate(make_token(email="shopper@example.com", email_verified=True))
        self.assertEqual(resolved, user)
        self.assertEqual(resolved.customer, customer)
        self.assertTrue(user.groups.filter(name="customer").exists())
        user.refresh_from_db()
        self.assertFalse(user.is_staff)


@override_settings(SUPABASE_JWT_SECRET=SECRET)
class UnverifiedTests(_Base):
    """State unverified: an unverified email never links an existing record."""

    def setUp(self):
        portal_tenant()
        self.victim = Customer.objects.create(first_name="V", last_name="C",
                                              email="victim@example.com")

    def _assert_untouched(self):
        self.victim.refresh_from_db()
        self.assertIsNone(self.victim.user)
        self.assertEqual(Customer.objects.count(), 1)
        self.assertFalse(UserIdentity.objects.exists())
        self.assertFalse(User.objects.filter(username__startswith="u-").exists())

    def test_unverified_email_refused_with_code(self):
        exc = self.assertRefused(make_token(email="victim@example.com", email_verified=False),
                                 CODE_VERIFY_EMAIL, PortalAuthFailed)
        self.assertEqual(exc.status_code, 401)
        self._assert_untouched()

    def test_missing_claim_treated_as_unverified(self):
        self.assertRefused(make_token(email="victim@example.com"), CODE_VERIFY_EMAIL,
                           PortalAuthFailed)
        self._assert_untouched()

    def test_truthy_non_true_claim_treated_as_unverified(self):
        self.assertRefused(
            make_token(email="victim@example.com", user_metadata={"email_verified": "true"}),
            CODE_VERIFY_EMAIL, PortalAuthFailed,
        )
        self._assert_untouched()

    def test_api_body_carries_the_code(self):
        client = APIClient(HTTP_AUTHORIZATION=f"Bearer {make_token(email='victim@example.com')}")
        response = client.get(ORDERS_URL)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], CODE_VERIFY_EMAIL)
        self.assertEqual(response["WWW-Authenticate"], "Bearer")


@override_settings(SUPABASE_JWT_SECRET=SECRET)
class MatchNTests(_Base):
    """State match-N: the resolver never picks one of several candidates."""

    def setUp(self):
        # Two people of the tenant reachable at one address. Since CRM Phase 2
        # step 11 two customers cannot share it (uniq_customer_email_ci_company),
        # so the second person carries it as an extra address of its own.
        portal_tenant()
        self.a = Customer.objects.create(first_name="A", last_name="A", email="Dup@example.com")
        self.b = Customer.objects.create(first_name="B", last_name="B", email="b.other@example.com")
        PartyEmail.objects.create(party=self.b.party, email="dup@example.com", purpose="personal")

    def test_verified_email_with_two_candidates_is_409(self):
        exc = self.assertRefused(make_token(email="dup@example.com", email_verified=True),
                                 CODE_AMBIGUOUS)
        self.assertEqual(exc.status_code, 409)
        for customer in (self.a, self.b):
            customer.refresh_from_db()
            self.assertIsNone(customer.user)
        self.assertFalse(UserIdentity.objects.exists())
        self.assertEqual(
            # Step 7: the candidates are the customers' person parties.
            set(AuditLog.objects.filter(action="portal_refused").values_list("object_id", flat=True)),
            {self.a.party_id, self.b.party_id},
        )

    def test_unverified_with_two_candidates_is_verify_email(self):
        self.assertRefused(make_token(email="dup@example.com"), CODE_VERIFY_EMAIL,
                           PortalAuthFailed)

    def test_api_status_and_code(self):
        token = make_token(email="dup@example.com", email_verified=True)
        response = APIClient(HTTP_AUTHORIZATION=f"Bearer {token}").get(ORDERS_URL)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], CODE_AMBIGUOUS)


@override_settings(SUPABASE_JWT_SECRET=SECRET)
class CollisionTests(_Base):
    """State collision: D5, no takeover of staff accounts by an untrusted provider."""

    def _staff_customer(self, **user_kwargs):
        staff = make_staff_user("clerk", email="clerk@example.com", **user_kwargs)
        customer = Customer.objects.create(first_name="C", last_name="K",
                                           email="clerk@example.com", user=staff)
        return staff, customer

    def test_verified_email_of_staff_held_customer_refused(self):
        staff, customer = self._staff_customer()
        exc = self.assertRefused(make_token(email="clerk@example.com", email_verified=True),
                                 CODE_COLLISION)
        self.assertEqual(exc.status_code, 403)
        self.assertFalse(UserIdentity.objects.exists())
        customer.refresh_from_db()
        self.assertEqual(customer.user, staff)
        self.assertTrue(AuditLog.objects.filter(action="portal_refused",
                                                object_id=customer.party_id).exists())

    def test_superuser_held_customer_refused(self):
        admin = User.objects.create_superuser("root", "root@example.com", "pw")
        Customer.objects.create(first_name="R", last_name="T", email="root@example.com", user=admin)
        self.assertRefused(make_token(email="root@example.com", email_verified=True),
                           CODE_COLLISION)
        self.assertFalse(admin.identities.exists())

    def test_identity_on_staff_user_refused(self):
        """A UserIdentity whose user is staff is refused for portal use (untrusted)."""
        staff, _customer = self._staff_customer()
        sub = str(uuid.uuid4())
        UserIdentity.objects.create(user=staff, provider="supabase", subject=sub)
        self.snapshot()
        self.assertRefused(make_token(sub=sub, email="changed@example.com"), CODE_COLLISION)
        self.assertIdentitiesUnchanged()

    def test_identity_on_user_promoted_to_staff_later_refused(self):
        customer, sub = make_portal_customer("promoted@example.com")
        from core.roles import grant_role

        grant_role(customer.user, "staff")
        self.snapshot()
        self.assertRefused(make_token(sub=sub), CODE_COLLISION)
        self.assertIdentitiesUnchanged()

    @override_settings(GOLDEN_IDENTITY_PROVIDERS=[supabase_provider(trusted=True)])
    def test_trusted_provider_may_link_staff_held_customer(self):
        staff, customer = self._staff_customer()
        user, _ = _authenticate(make_token(email="clerk@example.com", email_verified=True))
        self.assertEqual(user, staff)

    @override_settings(GOLDEN_IDENTITY_PROVIDERS=[supabase_provider(link_by_verified_email=False)])
    def test_provider_without_email_linking_refuses_existing_customer(self):
        customer = Customer.objects.create(first_name="G", last_name="S", email="g@example.com")
        self.assertRefused(make_token(email="g@example.com", email_verified=True), CODE_COLLISION)
        customer.refresh_from_db()
        self.assertIsNone(customer.user)


@override_settings(SUPABASE_JWT_SECRET=SECRET)
class ClosedTests(_Base):
    """State closed: both User.is_active and Customer.is_active are required."""

    def test_inactive_customer(self):
        customer, sub = make_portal_customer("closed@example.com", is_active=False)
        self.snapshot()
        exc = self.assertRefused(make_token(sub=sub, email="new@example.com"), CODE_CLOSED)
        self.assertEqual(exc.status_code, 403)
        self.assertIdentitiesUnchanged()  # no last_login_at / email refresh

    def test_inactive_user(self):
        customer, sub = make_portal_customer("gone@example.com")
        User.objects.filter(pk=customer.user_id).update(is_active=False)
        self.snapshot()
        self.assertRefused(make_token(sub=sub), CODE_CLOSED)
        self.assertIdentitiesUnchanged()

    def test_identity_whose_user_lost_its_customer(self):
        customer, sub = make_portal_customer("orphan@example.com")
        Customer.objects.filter(pk=customer.pk).update(user=None)
        self.assertRefused(make_token(sub=sub), CODE_CLOSED)

    def test_match_1_on_inactive_customer_links_nothing(self):
        customer = Customer.objects.create(first_name="I", last_name="C",
                                           email="inactive@example.com", is_active=False)
        self.assertRefused(make_token(email="inactive@example.com", email_verified=True),
                           CODE_CLOSED)
        customer.refresh_from_db()
        self.assertIsNone(customer.user)
        self.assertFalse(User.objects.filter(username__startswith="u-").exists())


@override_settings(SUPABASE_JWT_SECRET=SECRET)
class IsPortalCustomerTests(TestCase):
    """The permission: portal authenticator, active user, portal role, active Customer."""

    def setUp(self):
        portal_tenant()

    def test_linked_customer_reaches_portal(self):
        customer, sub = make_portal_customer("ok@example.com")
        response = APIClient(HTTP_AUTHORIZATION=f"Bearer {make_token(sub=sub)}").get(ORDERS_URL)
        self.assertEqual(response.status_code, 200)

    def test_user_without_portal_role_is_403(self):
        customer, sub = make_portal_customer("norole@example.com")
        customer.user.groups.clear()
        response = APIClient(HTTP_AUTHORIZATION=f"Bearer {make_token(sub=sub)}").get(ORDERS_URL)
        self.assertEqual(response.status_code, 403)

    def test_customer_user_session_is_not_portal_auth(self):
        """Only the portal token class satisfies it, not a session for the same user."""
        customer, _sub = make_portal_customer("session@example.com")
        client = APIClient()
        client.force_login(customer.user)
        self.assertIn(client.get(ORDERS_URL).status_code, (401, 403))


@override_settings(SUPABASE_JWT_SECRET=SECRET, **ADMIN_API)
class ProviderConfirmationTests(_Base):
    """The Supabase Admin API, not the user-writable token flag, decides (review P1-P3)."""

    CONFIRMS = None  # the real SupabaseJWT.confirms_email, over a mocked HTTP API

    def test_stale_identity_replaced_when_provider_says_it_is_gone(self):
        """Provider account deleted and re-created: the confirmed email relinks."""
        customer, stale_sub = make_portal_customer("stale@example.com")
        new_sub = str(uuid.uuid4())
        with supabase_admin({new_sub: confirmed("stale@example.com")}) as get:
            user, _ = _authenticate(make_token(sub=new_sub, email="stale@example.com",
                                               email_verified=True))
        self.assertIn(f"/auth/v1/admin/users/{stale_sub}", get.call_args_list[-1].args[0])
        self.assertEqual(user, customer.user)
        self.assertEqual(list(user.identities.values_list("subject", flat=True)), [new_sub])
        customer.refresh_from_db()
        self.assertEqual(customer.user, user)

    def test_live_identity_is_never_replaced(self):
        """The customer's email drifted from its login's: a second person owns it now."""
        customer, live_sub = make_portal_customer("drift@example.com")
        new_sub = str(uuid.uuid4())
        self.snapshot()
        with supabase_admin({new_sub: confirmed("drift@example.com"),
                             live_sub: confirmed("moved@example.com")}):
            self.assertRefused(make_token(sub=new_sub, email="drift@example.com",
                                          email_verified=True), CODE_COLLISION)
        self.assertNothingWritten(customer)
        self.assertIdentitiesUnchanged()

    def test_held_identity_kept_when_provider_errors(self):
        customer, live_sub = make_portal_customer("unknown@example.com")
        new_sub = str(uuid.uuid4())
        self.snapshot()
        with supabase_admin({new_sub: confirmed("unknown@example.com")}, default=(503, None)):
            self.assertRefused(make_token(sub=new_sub, email="unknown@example.com",
                                          email_verified=True), CODE_COLLISION)
        self.assertIdentitiesUnchanged()

    def test_link_refused_when_provider_does_not_confirm_the_email(self):
        customer = Customer.objects.create(first_name="V", last_name="C", email="v@example.com")
        self.snapshot()
        with supabase_admin(default=(200, {"email": "v@example.com", "email_confirmed_at": None})):
            self.assertRefused(make_token(email="v@example.com", email_verified=True),
                               CODE_VERIFY_EMAIL, PortalAuthFailed)
        self.assertNothingWritten(customer)

    def test_link_proceeds_when_provider_confirms_the_email(self):
        customer = Customer.objects.create(first_name="V", last_name="C", email="ok@example.com")
        with supabase_admin(default=confirmed("OK@example.com")):
            user, _ = _authenticate(make_token(email="ok@example.com", email_verified=True))
        self.assertEqual(user.customer, customer)

    def test_p1_admin_api_down_never_falls_back_to_the_token_flag(self):
        """5xx/timeouts refuse; a self-set user_metadata.email_verified links nothing."""
        victim = Customer.objects.create(first_name="V", last_name="C", email="victim@example.com")
        self.snapshot()
        for failure in ((503, None), (500, None), (401, None)):
            with self.subTest(failure=failure), supabase_admin(default=failure):
                self.assertRefused(
                    make_token(email="victim@example.com",
                               user_metadata={"email_verified": True}),
                    CODE_VERIFY_EMAIL, PortalAuthFailed,
                )
        import httpx

        with mock.patch("httpx.get", side_effect=httpx.ConnectTimeout("slow")):
            self.assertRefused(make_token(email="victim@example.com", email_verified=True),
                               CODE_VERIFY_EMAIL, PortalAuthFailed)
        self.assertNothingWritten(victim)

    @override_settings(SUPABASE_SERVICE_ROLE_KEY="")
    def test_p1b_empty_service_key_refuses_instead_of_trusting_the_token(self):
        victim = Customer.objects.create(first_name="V", last_name="C", email="v2@example.com")
        self.snapshot()
        with mock.patch("httpx.get") as get:
            self.assertRefused(make_token(email="v2@example.com", email_verified=True),
                               CODE_VERIFY_EMAIL, PortalAuthFailed)
            self.assertRefused(make_token(email="new2@example.com", email_verified=True),
                               CODE_VERIFY_EMAIL, PortalAuthFailed)
        get.assert_not_called()
        self.assertNothingWritten(victim)
        self.assertFalse(Customer.objects.filter(email="new2@example.com").exists())

    def test_match_0_needs_a_confirmed_address(self):
        sub = str(uuid.uuid4())
        with supabase_admin({sub: (200, {"email": "n@example.com", "email_confirmed_at": None})}):
            self.assertRefused(make_token(sub=sub, email="n@example.com", email_verified=True),
                               CODE_VERIFY_EMAIL, PortalAuthFailed)
        self.assertFalse(Customer.objects.exists())
        with supabase_admin({sub: confirmed("n@example.com")}):
            user, _ = _authenticate(make_token(sub=sub, email="n@example.com",
                                               email_verified=True))
        self.assertEqual(user.customer.email, "n@example.com")

    def test_p2_an_unconfirmed_squat_is_replaced_by_the_confirmed_owner(self):
        """A squat left by pre-fix code (an unconfirmed subject) does not lock the owner out."""
        squat, squat_sub = make_portal_customer("owner@example.com")
        owner_sub = str(uuid.uuid4())
        with supabase_admin({owner_sub: confirmed("owner@example.com"),
                             squat_sub: (200, {"email": "other@example.net",
                                               "email_confirmed_at": None})}):
            user, _ = _authenticate(make_token(sub=owner_sub, email="owner@example.com",
                                               email_verified=True))
        self.assertEqual(user.customer, squat)
        self.assertEqual(list(user.identities.values_list("subject", flat=True)), [owner_sub])
        self.assertFalse(UserIdentity.objects.filter(subject=squat_sub).exists())

    def test_p2_a_new_unverified_sign_up_cannot_squat(self):
        squat_sub = str(uuid.uuid4())
        with supabase_admin({squat_sub: (200, {"email": "owner2@example.com",
                                              "email_confirmed_at": None})}):
            self.assertRefused(make_token(sub=squat_sub, email="owner2@example.com"),
                               CODE_VERIFY_EMAIL, PortalAuthFailed)
            self.assertRefused(make_token(sub=squat_sub, email="owner2@example.com",
                                          user_metadata={"email_verified": True}),
                               CODE_VERIFY_EMAIL, PortalAuthFailed)
        self.assertFalse(Customer.objects.filter(email="owner2@example.com").exists())

    def test_p3_held_subject_is_rechecked_under_the_lock(self):
        """A subject attached after the snapshot (a concurrent first login) is never replaced."""
        customer = Customer.objects.create(first_name="R", last_name="C", email="race@example.com")
        # What the second request's lookup saw: the candidate party, no login yet.
        stale = Party.objects.get(pk=customer.party_id)
        sub_a, sub_b = str(uuid.uuid4()), str(uuid.uuid4())
        conf = confirmed("race@example.com")
        with supabase_admin({sub_a: conf, sub_b: conf}):
            _authenticate(make_token(sub=sub_a, email="race@example.com", email_verified=True))
            with mock.patch("core.identity.resolve.find_candidates", return_value=[stale]):
                self.assertRefused(make_token(sub=sub_b, email="race@example.com",
                                              email_verified=True), CODE_COLLISION)
        customer.refresh_from_db()
        self.assertEqual(list(customer.user.identities.values_list("subject", flat=True)), [sub_a])

    def test_lookalike_unicode_address_never_links(self):
        """iexact is UPPER() on Postgres (ı→I); non-ASCII addresses never link by email."""
        customer = Customer.objects.create(first_name="V", last_name="C", email="iris@example.com")
        with supabase_admin(default=confirmed("ırıs@example.com")):
            try:
                _authenticate(make_token(email="ırıs@example.com", email_verified=True))
            except (PortalRefused, PortalAuthFailed):
                pass
        customer.refresh_from_db()
        self.assertIsNone(customer.user)

    @override_settings(GOLDEN_PORTAL_SIGNUP_RATE="2/hour")
    def test_self_registration_is_throttled_per_ip(self):
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)
        statuses = []
        for i in range(3):
            sub = str(uuid.uuid4())
            token = make_token(sub=sub, email=f"s{i}@example.com", email_verified=True)
            with supabase_admin({sub: confirmed(f"s{i}@example.com")}):
                response = APIClient(HTTP_AUTHORIZATION=f"Bearer {token}").get(ORDERS_URL)
            statuses.append(response.status_code)
        self.assertEqual(statuses, [200, 200, 429])
        self.assertEqual(Customer.objects.count(), 2)
