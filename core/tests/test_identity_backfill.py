"""Identity step 3: the ``UserIdentity`` backfill (spec §3.3, §8 step 3).

Test settings disable migrations, and identity step 8 dropped the legacy
columns the backfill reads. So the backfill and the data migration's own
``forwards``/``backwards`` run against the historical registry ``self.H``
(``core.tests.legacy_schema``), with the two columns put back on the test
tables. The real migrate path is exercised on Postgres by CI's
``migrations-postgres`` job.
"""

import contextlib
import importlib
import io
import secrets
import time
import uuid

import jwt
from django.contrib.auth.models import Group, User
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory

from core.identity import (
    LEGACY_SUPABASE_PROVIDER,
    LOCAL_PROVIDER,
    RESERVED_PROVIDER_NAMES,
    SUPABASE_PROVIDER,
)
from core.identity.backfill import (
    backfill_identities,
    generated_username,
    profile_uid_provider,
    reverse_backfill,
)
from core.models import UserIdentity, UserProfile
from core.portal_auth import PortalTokenAuthentication
from core.tests.helpers import make_staff_user, portal_tenant
from core.tests.legacy_schema import LegacyColumnsTestCase
from customers.models import Customer
from customers.pre_tighten import PreTightenSchemaMixin

MIGRATION = importlib.import_module("customers.migrations.0004_backfill_user_identity")
SECRET = "test-supabase-" + secrets.token_hex(16)


def _customer(email, **extra):
    extra.setdefault("company", portal_tenant())  # NOT NULL since CRM Phase 2 step 11
    return Customer.objects.create(
        first_name=extra.pop("first_name", "Pat"),
        last_name=extra.pop("last_name", "Doe"),
        email=email,
        **extra,
    )


class CustomerBackfillTests(LegacyColumnsTestCase):
    def setUp(self):
        self.sub = uuid.uuid4()
        self.portal = _customer("Pat@Example.com")
        self.set_customer_uid(self.portal, self.sub)
        self.offline = _customer("offline@example.com")

    def test_portal_customer_gets_user_and_identity(self):
        report = backfill_identities(self.H)

        self.portal.refresh_from_db()
        user = self.portal.user
        self.assertIsNotNone(user)
        self.assertEqual(user.username, f"u-{self.portal.pk.hex}")
        self.assertEqual(user.email, "pat@example.com")
        self.assertEqual((user.first_name, user.last_name), ("Pat", "Doe"))
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.is_active)
        self.assertTrue(user.groups.filter(name="customer").exists())
        self.assertFalse(user.groups.exclude(name="customer").exists())

        identity = UserIdentity.objects.get(user=user)
        self.assertEqual((identity.provider, identity.subject), (SUPABASE_PROVIDER, str(self.sub)))
        self.assertEqual(identity.email, "pat@example.com")
        self.assertFalse(identity.email_verified)
        self.assertEqual(identity.metadata, {})
        # The legacy column is left in place (step 8 drops it later).
        self.assertEqual(self.customer_uid(self.portal), self.sub)

        self.assertEqual(report["users_created"], 1)
        self.assertEqual(report["customers_linked"], 1)
        self.assertEqual(report[SUPABASE_PROVIDER], 1)

    def test_customer_without_portal_login_gets_no_user(self):
        users_before = User.objects.count()
        backfill_identities(self.H)
        self.offline.refresh_from_db()
        self.assertIsNone(self.offline.user)
        # Only the portal customer's user was created: never en masse.
        self.assertEqual(User.objects.count(), users_before + 1)

    def test_inactive_customer_gets_inactive_user(self):
        self.portal.is_active = False
        self.portal.save()
        backfill_identities(self.H)
        self.portal.refresh_from_db()
        self.assertFalse(self.portal.user.is_active)

    def test_idempotent(self):
        backfill_identities(self.H)
        counts = (User.objects.count(), UserIdentity.objects.count(),
                  Group.objects.get(name="customer").user_set.count())
        again = backfill_identities(self.H)
        self.assertEqual(
            counts,
            (User.objects.count(), UserIdentity.objects.count(),
             Group.objects.get(name="customer").user_set.count()),
        )
        self.assertEqual(again["users_created"], 0)
        self.assertEqual(again["customers_linked"], 0)
        self.assertEqual(again[SUPABASE_PROVIDER], 0)
        self.assertEqual(again["conflicts"], [])

    def test_login_holding_another_supabase_subject_is_reported(self):
        user = User.objects.create_user("holder")
        Customer.objects.filter(pk=self.portal.pk).update(user=user)
        UserIdentity.objects.create(user=user, provider=SUPABASE_PROVIDER, subject=str(uuid.uuid4()))
        report = backfill_identities(self.H)
        self.assertEqual(report["conflicts"], [self.portal.pk])
        self.assertEqual(user.identities.count(), 1)
        self.assertFalse(UserIdentity.objects.filter(subject=str(self.sub)).exists())

    def test_generated_username_held_by_staff_is_refused(self):
        make_staff_user(generated_username(self.portal.pk))
        report = backfill_identities(self.H)
        self.portal.refresh_from_db()
        self.assertIsNone(self.portal.user)
        self.assertFalse(UserIdentity.objects.filter(provider=SUPABASE_PROVIDER).exists())
        self.assertEqual(report["conflicts"], [self.portal.pk])

    def test_unlinked_generated_username_is_never_adopted(self):
        squatter = User.objects.create_user(generated_username(self.portal.pk))
        report = backfill_identities(self.H)
        self.portal.refresh_from_db()
        self.assertIsNone(self.portal.user)
        self.assertFalse(squatter.identities.exists())
        self.assertEqual(report["conflicts"], [self.portal.pk])
        reverse_backfill(self.H)
        self.assertTrue(User.objects.filter(pk=squatter.pk).exists())

    def test_customer_already_linked_to_staff_is_refused(self):
        staff = make_staff_user("pat-staff")
        Customer.objects.filter(pk=self.portal.pk).update(user=staff)
        report = backfill_identities(self.H)
        self.assertFalse(staff.identities.exists())
        self.assertFalse(staff.groups.filter(name="customer").exists())
        self.assertEqual(report["conflicts"], [self.portal.pk])

    def test_customer_already_linked_keeps_its_user(self):
        owner = User.objects.create_user("pat-own")
        Customer.objects.filter(pk=self.portal.pk).update(user=owner)
        report = backfill_identities(self.H)
        self.assertEqual(
            UserIdentity.objects.get(provider=SUPABASE_PROVIDER).user, owner
        )
        self.assertEqual(report["users_created"], 0)



class EmailCaseDuplicateReportTests(PreTightenSchemaMixin, LegacyColumnsTestCase):
    """Case duplicates exist only before CRM Phase 2 step 11 forbids them."""

    def test_email_case_duplicates_reported_not_merged(self):
        upper = _customer("DUP@example.com")
        lower = _customer("dup@example.com")
        report = backfill_identities(self.H)
        self.assertEqual(report["email_case_duplicates"], [sorted([str(upper.pk), str(lower.pk)])])
        self.assertEqual(Customer.objects.filter(email__iexact="dup@example.com").count(), 2)


class ProfileBackfillTests(LegacyColumnsTestCase):
    def setUp(self):
        self.legacy_user = make_staff_user("legacy")
        self.legacy_uid = str(uuid.uuid4())
        self.make_profile(self.legacy_uid, user=self.legacy_user)
        self.local_user = make_staff_user("tenancy")
        self.local_uid = f"local:{self.local_user.pk}"
        self.make_profile(self.local_uid, user=self.local_user)
        self.session_user = make_staff_user("sess")
        self.make_profile(f"session:{self.session_user.pk}", user=self.session_user)
        self.null_user = make_staff_user("nouid")
        self.make_profile(None, user=self.null_user)
        self.orphan_uid = str(uuid.uuid4())
        self.orphan = self.make_profile(self.orphan_uid, user=None, display_name="Old rep")

    def test_uid_already_held_by_another_user_is_reported(self):
        other = User.objects.create_user("other")
        UserIdentity.objects.create(user=other, provider=LEGACY_SUPABASE_PROVIDER, subject=self.legacy_uid)
        report = backfill_identities(self.H)
        self.assertEqual(report["profile_conflicts"],
                         [UserProfile.objects.get(user=self.legacy_user).pk])
        self.assertFalse(self.legacy_user.identities.exists())

    def test_user_holding_another_subject_of_the_provider_is_reported(self):
        UserIdentity.objects.create(user=self.legacy_user, provider=LEGACY_SUPABASE_PROVIDER,
                                    subject=str(uuid.uuid4()))
        report = backfill_identities(self.H)
        self.assertEqual(report["profile_conflicts"],
                         [UserProfile.objects.get(user=self.legacy_user).pk])
        self.assertEqual(self.legacy_user.identities.count(), 1)

    def test_classification(self):
        self.assertIsNone(profile_uid_provider(None))
        self.assertIsNone(profile_uid_provider(""))
        self.assertIsNone(profile_uid_provider("session:7"))
        self.assertIsNone(profile_uid_provider(f"restored:{uuid.uuid4()}"))
        self.assertEqual(profile_uid_provider("local:7"), LOCAL_PROVIDER)
        self.assertEqual(profile_uid_provider(str(uuid.uuid4())), LEGACY_SUPABASE_PROVIDER)

    def test_rows_per_kind(self):
        report = backfill_identities(self.H)
        rows = {(i.provider, i.subject): i.user for i in UserIdentity.objects.all()}
        self.assertEqual(
            rows,
            {
                (LEGACY_SUPABASE_PROVIDER, self.legacy_uid): self.legacy_user,
                (LOCAL_PROVIDER, self.local_uid): self.local_user,
            },
        )
        self.assertEqual(report[LEGACY_SUPABASE_PROVIDER], 1)
        self.assertEqual(report[LOCAL_PROVIDER], 1)
        self.assertEqual(report["unlinked_profiles"], 1)
        # The orphan keeps its uid: it is an FK target for history.
        self.assertEqual(self.profile_uid(self.orphan), self.orphan_uid)

    def test_backfill_writes_no_user_for_profiles(self):
        before = User.objects.count()
        backfill_identities(self.H)
        self.assertEqual(User.objects.count(), before)

    def test_idempotent(self):
        backfill_identities(self.H)
        before = UserIdentity.objects.count()
        again = backfill_identities(self.H)
        self.assertEqual(UserIdentity.objects.count(), before)
        self.assertEqual(again[LEGACY_SUPABASE_PROVIDER], 0)
        self.assertEqual(again[LOCAL_PROVIDER], 0)


@override_settings(SUPABASE_JWT_SECRET=SECRET)
class LegacySupabaseInertTests(LegacyColumnsTestCase):
    """A ``legacy_supabase`` row records history; it logs nobody in."""

    def setUp(self):
        portal_tenant()
        self.staff = make_staff_user("old-staff", email="old-staff@example.com")
        self.uid = str(uuid.uuid4())
        self.make_profile(self.uid, user=self.staff)
        backfill_identities(self.H)

    def _token(self):
        now = int(time.time())
        return jwt.encode(
            {"sub": self.uid, "aud": "authenticated", "iat": now, "exp": now + 3600,
             "email": "old-staff@example.com", "user_metadata": {"email_verified": True}},
            SECRET, algorithm="HS256",
        )

    def test_reserved_names(self):
        self.assertEqual(RESERVED_PROVIDER_NAMES, {LEGACY_SUPABASE_PROVIDER, LOCAL_PROVIDER})
        self.assertNotIn(SUPABASE_PROVIDER, RESERVED_PROVIDER_NAMES)
        self.assertTrue(
            UserIdentity.objects.filter(provider=LEGACY_SUPABASE_PROVIDER, user=self.staff).exists()
        )

    def test_portal_token_with_legacy_subject_does_not_reach_staff_user(self):
        from core.tests.test_portal_auth import provider_confirms

        patcher = provider_confirms(True)
        patcher.start()
        self.addCleanup(patcher.stop)
        request = APIRequestFactory().get("/", HTTP_AUTHORIZATION=f"Bearer {self._token()}")
        principal, _ = PortalTokenAuthentication().authenticate(request)
        # The resolver looks up UserIdentity("supabase", sub) only; the
        # legacy_supabase row never matches, so the subject is a brand-new
        # customer-only account, unrelated to the staff user.
        self.assertFalse(principal.is_staff)
        self.assertNotEqual(principal.pk, self.staff.pk)
        self.assertEqual(principal.customer.user, principal)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.is_staff)
        self.assertEqual(list(self.staff.identities.values_list("provider", flat=True)),
                         [LEGACY_SUPABASE_PROVIDER])


class ReverseTests(LegacyColumnsTestCase):
    def setUp(self):
        self.sub = uuid.uuid4()
        self.customer = _customer("rev@example.com")
        self.set_customer_uid(self.customer, self.sub)
        self.staff = make_staff_user("rev-staff")
        self.make_profile(str(uuid.uuid4()), user=self.staff)
        self.local_user = make_staff_user("rev-local")
        self.make_profile(f"local:{self.local_user.pk}", user=self.local_user)

    def test_reverse_removes_what_forward_created(self):
        users_before = set(User.objects.values_list("pk", flat=True))
        backfill_identities(self.H)
        report = reverse_backfill(self.H)

        self.assertFalse(UserIdentity.objects.exists())
        self.assertEqual(set(User.objects.values_list("pk", flat=True)), users_before)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.user)
        self.assertEqual(self.customer_uid(self.customer), self.sub)
        self.assertEqual(UserProfile.objects.count(), 2)
        self.assertEqual(report["users_deleted"], 1)
        self.assertEqual(report["identities_deleted"], 3)

    def test_forward_after_reverse(self):
        backfill_identities(self.H)
        reverse_backfill(self.H)
        report = backfill_identities(self.H)
        self.assertEqual(report["users_created"], 1)
        self.assertEqual(UserIdentity.objects.count(), 3)

    def test_reverse_removes_a_stale_row_left_by_a_relink(self):
        backfill_identities(self.H)
        self.customer.refresh_from_db()
        user = self.customer.user
        # The column was relinked to a new Supabase uid; the user's row still
        # holds the old one. (A second row beside it is impossible since
        # core.0012's one-subject-per-provider constraint.)
        self.set_customer_uid(self.customer, uuid.uuid4())
        self.assertEqual(user.identities.count(), 1)
        reverse_backfill(self.H)
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(UserIdentity.objects.exists())

    def test_reverse_keeps_a_generated_user_that_became_staff(self):
        backfill_identities(self.H)
        self.customer.refresh_from_db()
        user = self.customer.user
        from core.roles import grant_role

        grant_role(user, "staff")
        report = reverse_backfill(self.H)
        self.assertTrue(User.objects.filter(pk=user.pk).exists())
        self.assertEqual(report["users_kept"], [user.username])

    def test_reverse_leaves_hand_linked_users(self):
        owner = User.objects.create_user("hand-linked")
        Customer.objects.filter(pk=self.customer.pk).update(user=owner)
        backfill_identities(self.H)
        reverse_backfill(self.H)
        self.assertTrue(User.objects.filter(pk=owner.pk).exists())
        self.assertFalse(owner.identities.exists())


class MigrationWiringTests(LegacyColumnsTestCase):
    def test_forwards_prints_counts(self):
        self.set_customer_uid(_customer("print@example.com"), uuid.uuid4())
        self.make_profile(str(uuid.uuid4()), user=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            MIGRATION.forwards(self.H, None)
        text = out.getvalue()
        self.assertIn("customer users created: 1", text)
        self.assertIn("profiles with a uid but no user (left in place): 1", text)

    def test_forwards_silent_on_empty_database(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            MIGRATION.forwards(self.H, None)
        self.assertEqual(out.getvalue(), "")

    def test_backwards_undoes_forwards(self):
        self.set_customer_uid(_customer("wire@example.com"), uuid.uuid4())
        with contextlib.redirect_stdout(io.StringIO()):
            MIGRATION.forwards(self.H, None)
        MIGRATION.backwards(self.H, None)
        self.assertFalse(UserIdentity.objects.exists())
        self.assertFalse(User.objects.filter(username__startswith="u-").exists())


class SchemaTests(TestCase):
    def test_provider_subject_unique(self):
        user = User.objects.create_user("uniq")
        UserIdentity.objects.create(user=user, provider="supabase", subject="abc")
        with self.assertRaises(IntegrityError), transaction.atomic():
            UserIdentity.objects.create(user=user, provider="supabase", subject="abc")
        UserIdentity.objects.create(user=user, provider="other", subject="abc")

    def test_one_subject_per_provider_per_user(self):
        user = User.objects.create_user("one-per-provider")
        UserIdentity.objects.create(user=user, provider="supabase", subject="first")
        with self.assertRaises(IntegrityError), transaction.atomic():
            UserIdentity.objects.create(user=user, provider="supabase", subject="second")
        UserIdentity.objects.create(user=user, provider="authentik", subject="second")

    def test_email_lower_cased_on_save(self):
        user = User.objects.create_user("lower")
        row = UserIdentity.objects.create(user=user, provider="p", subject="s", email="Mixed@Example.COM")
        row.refresh_from_db()
        self.assertEqual(row.email, "mixed@example.com")

    def test_profile_str_without_display_name(self):
        user = User.objects.create_user("nameless")
        profile = UserProfile.objects.create(user=user)
        self.assertEqual(str(profile), "nameless")


class UserDeletionTests(LegacyColumnsTestCase):
    def test_deleting_user_unlinks_customer(self):
        customer = _customer("del@example.com")
        self.set_customer_uid(customer, uuid.uuid4())
        backfill_identities(self.H)
        customer.refresh_from_db()
        customer.user.delete()
        customer.refresh_from_db()
        self.assertIsNone(customer.user)
        self.assertFalse(UserIdentity.objects.filter(provider=SUPABASE_PROVIDER).exists())


class SurfaceTests(TestCase):
    """Schema-adjacent behaviour step 3 changes outside the backfill."""

    def test_staff_login_profile_gets_no_placeholder_uid(self):
        staff = make_staff_user("signal", password="pw")
        self.assertTrue(self.client.login(username="signal", password="pw"))
        staff.refresh_from_db()
        self.assertEqual(staff.userprofile.legacy_external_id, "")

    def test_identity_admin_is_read_only(self):
        User.objects.create_superuser("root", "root@example.com", "pw")
        self.client.login(username="root", password="pw")
        user = User.objects.create_user("listed")
        row = UserIdentity.objects.create(user=user, provider="supabase", subject="s-1")
        base = "/admin/core/useridentity/"
        self.assertEqual(self.client.get(base).status_code, 200)
        self.assertEqual(self.client.get(f"{base}add/").status_code, 403)
        self.assertEqual(self.client.post(f"{base}{row.pk}/delete/", {"post": "yes"}).status_code, 403)
        self.assertTrue(UserIdentity.objects.filter(pk=row.pk).exists())

    def test_only_view_permission_exists(self):
        from django.contrib.auth.models import Permission

        codenames = set(
            Permission.objects.filter(content_type__app_label="core",
                                      content_type__model="useridentity")
            .values_list("codename", flat=True)
        )
        self.assertEqual(codenames, {"view_useridentity"})

    def test_customer_user_not_writable_through_api(self):
        from rest_framework.test import APIClient

        from core.models import Company

        company = Company.objects.create(name="Identity Co")
        staff = make_staff_user("api-staff")
        UserProfile.objects.create(user=staff, company=company)
        api = APIClient()
        api.force_authenticate(user=staff)
        victim_login = User.objects.create_user("victim-login")
        attacker = User.objects.create_user("attacker-login")
        customer = _customer("api@example.com", company=company)
        Customer.objects.filter(pk=customer.pk).update(user=victim_login)
        url = f"/api/v1/customers/customers/{customer.pk}/"
        response = api.patch(url, {"user": attacker.pk, "first_name": "Changed"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        customer.refresh_from_db()
        self.assertEqual(customer.user, victim_login)
        self.assertEqual(customer.first_name, "Changed")


class OneSubjectPerProviderMigrationTests(TestCase):
    """core.0012 refuses existing duplicates by name instead of deleting any."""

    MIGRATION = importlib.import_module("core.migrations.0012_useridentity_user_provider_uniq")

    def _run(self):
        from types import SimpleNamespace

        from django.apps import apps
        from django.db import connection

        # It only reads, through schema_editor.connection; SQLite refuses a
        # real schema editor inside the test transaction.
        self.MIGRATION.refuse_duplicates(apps, SimpleNamespace(connection=connection))

    def test_clean_table_passes(self):
        user = User.objects.create_user("clean")
        UserIdentity.objects.create(user=user, provider="supabase", subject="a")
        UserIdentity.objects.create(user=user, provider="authentik", subject="a")
        self._run()

    def test_duplicates_are_listed_and_kept(self):
        from django.db import connection

        if connection.vendor != "postgresql":
            self.skipTest("needs transactional DDL to drop the constraint inside the test")
        constraint = next(c for c in UserIdentity._meta.constraints
                          if c.name == "core_useridentity_user_provider_uniq")
        with connection.schema_editor(atomic=False) as editor:
            editor.remove_constraint(UserIdentity, constraint)
        user = User.objects.create_user("dupe")
        rows = [UserIdentity.objects.create(user=user, provider="supabase", subject=s)
                for s in ("one", "two")]
        with self.assertRaises(self.MIGRATION.DuplicateIdentities) as ctx:
            self._run()
        message = str(ctx.exception)
        self.assertIn(f"user {user.pk}, provider 'supabase'", message)
        for row in rows:
            self.assertIn(str(row.pk), message)
        self.assertEqual(UserIdentity.objects.filter(user=user).count(), 2)
