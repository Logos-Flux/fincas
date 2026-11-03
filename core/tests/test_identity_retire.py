"""Identity step 8: the pre-check before the legacy columns are dropped.

``core.identity.retire`` runs in ``customers.0008_identity_retire_precheck``
on the historical registry; see ``core.tests.legacy_schema`` for how the
dropped columns come back for these tests.
"""

import contextlib
import importlib
import io
import uuid

from django.contrib.auth.models import User

from core.identity import LEGACY_SUPABASE_PROVIDER
from core.identity.backfill import generated_username
from core.identity.retire import (
    LegacyIdentityNotCaptured,
    restore_legacy_columns,
    retire_legacy_columns,
)
from core.models import UserIdentity, UserProfile
from core.tests.helpers import make_portal_customer, make_staff_user, portal_tenant
from core.tests.legacy_schema import LegacyColumnsTestCase
from customers.models import Customer

MIGRATION = importlib.import_module("customers.migrations.0008_identity_retire_precheck")


def _customer(email, **extra):
    extra.setdefault("company", portal_tenant())  # NOT NULL since CRM Phase 2 step 11
    return Customer.objects.create(first_name="Pat", last_name="Doe", email=email, **extra)


def _portal_customer(email, **extra):
    extra.setdefault("company", portal_tenant())
    return make_portal_customer(email, **extra)


class CustomerPrecheckTests(LegacyColumnsTestCase):
    def test_captured_customer_passes(self):
        customer, sub = _portal_customer("ok@example.com")
        self.set_customer_uid(customer, uuid.UUID(sub))
        report = retire_legacy_columns(self.H)
        self.assertEqual(report["uncaptured_customers"], [])
        self.assertEqual(report["relinked_customers"], [])
        self.assertEqual(report["backfill"]["users_created"], 0)

    def test_uid_only_customer_is_captured_by_the_rerun(self):
        customer = _customer("late@example.com")
        uid = uuid.uuid4()
        self.set_customer_uid(customer, uid)
        report = retire_legacy_columns(self.H)
        customer.refresh_from_db()
        self.assertIsNotNone(customer.user)
        self.assertEqual(UserIdentity.objects.get(subject=str(uid)).user, customer.user)
        self.assertEqual(report["backfill"]["customers_linked"], 1)

    def test_identity_under_another_provider_name_counts(self):
        """The resolver attaches subjects under whatever the provider is named."""
        customer, sub = _portal_customer("shop@example.com", provider="shop-b")
        self.set_customer_uid(customer, uuid.UUID(sub))
        report = retire_legacy_columns(self.H)
        self.assertEqual(report["uncaptured_customers"], [])

    def test_staff_held_customer_is_refused_with_pks_only(self):
        staff = make_staff_user("clerk", email="clerk@example.com")
        customer = _customer("clerk@example.com", user=staff)
        self.set_customer_uid(customer, uuid.uuid4())
        with self.assertRaises(LegacyIdentityNotCaptured) as ctx:
            retire_legacy_columns(self.H)
        message = str(ctx.exception)
        self.assertIn(str(customer.pk), message)
        self.assertNotIn("clerk@example.com", message)
        self.assertNotIn(str(self.customer_uid(customer)), message)

    def test_squatted_generated_username_is_refused(self):
        customer = _customer("squat@example.com")
        self.set_customer_uid(customer, uuid.uuid4())
        User.objects.create_user(generated_username(customer.pk))
        with self.assertRaisesMessage(LegacyIdentityNotCaptured, str(customer.pk)):
            retire_legacy_columns(self.H)

    def test_an_inert_legacy_row_never_counts_as_capture(self):
        """A staff profile's historical uid equal to the customer's does not save it."""
        staff = make_staff_user("ex-customer")
        uid = uuid.uuid4()
        customer = _customer("ex@example.com", user=staff)
        self.set_customer_uid(customer, uid)
        self.make_profile(str(uid), user=staff)
        with self.assertRaisesMessage(LegacyIdentityNotCaptured, str(customer.pk)):
            retire_legacy_columns(self.H)
        self.assertTrue(UserIdentity.objects.filter(
            provider=LEGACY_SUPABASE_PROVIDER, subject=str(uid)).exists())

    def test_relinked_subject_is_reported_not_refused(self):
        """The subject moved to another customer's user: no login depends on the column."""
        holder, sub = _portal_customer("new@example.com")
        stale = _customer("old@example.com")
        self.set_customer_uid(stale, uuid.UUID(sub))
        report = retire_legacy_columns(self.H)
        self.assertEqual(report["relinked_customers"], [stale.pk])
        self.assertEqual(report["uncaptured_customers"], [])
        stale.refresh_from_db()
        self.assertIsNone(stale.user)
        self.assertEqual(UserIdentity.objects.get(subject=sub).user, holder.user)


    def test_subject_held_by_a_user_without_customer_is_refused(self):
        """Dropping the column would lose the only customer link of that login."""
        loner = User.objects.create_user("loner")
        sub = str(uuid.uuid4())
        UserIdentity.objects.create(user=loner, provider="supabase", subject=sub)
        customer = _customer("lost@example.com")
        self.set_customer_uid(customer, uuid.UUID(sub))
        with self.assertRaisesMessage(LegacyIdentityNotCaptured, str(customer.pk)):
            retire_legacy_columns(self.H)

    def test_relinked_customer_without_user_loses_account_holder(self):
        _holder, sub = _portal_customer("now@example.com")
        stale = _customer("then@example.com")
        self.set_customer_uid(stale, uuid.UUID(sub))

        # An individual's holder is its own party (customer_individual_holder_is_party).
        Customer.objects.filter(pk=stale.pk).update(account_holder_id=stale.party_id)
        report = retire_legacy_columns(self.H)
        self.assertEqual(report["account_holders_cleared"], 1)
        stale.refresh_from_db()
        self.assertIsNone(stale.account_holder_id)


class ProfilePrecheckTests(LegacyColumnsTestCase):
    def test_orphan_uid_is_archived(self):
        uid = str(uuid.uuid4())
        orphan = self.make_profile(uid, user=None, display_name="Old rep")
        report = retire_legacy_columns(self.H)
        orphan.refresh_from_db()
        self.assertEqual(orphan.legacy_external_id, uid)
        self.assertEqual(report["archived_profiles"], 1)
        self.assertEqual(str(UserProfile.objects.get(pk=orphan.pk)), "Old rep")

    def test_placeholders_are_not_archived(self):
        session = self.make_profile("session:9", user=None)
        local = self.make_profile("local:9", user=None)
        restored = self.make_profile(f"restored:{uuid.uuid4()}", user=None)
        report = retire_legacy_columns(self.H)
        for profile in (session, local, restored):
            profile.refresh_from_db()
            self.assertEqual(profile.legacy_external_id, "")
        self.assertEqual(report["archived_profiles"], 0)

    def test_linked_profile_uid_becomes_an_inert_identity(self):
        staff = make_staff_user("old-staff")
        uid = str(uuid.uuid4())
        self.make_profile(uid, user=staff)
        tenancy = make_staff_user("tenancy")
        self.make_profile(f"local:{tenancy.pk}", user=tenancy)
        retire_legacy_columns(self.H)
        self.assertEqual(
            set(UserIdentity.objects.values_list("provider", "subject", "user_id")),
            {(LEGACY_SUPABASE_PROVIDER, uid, staff.pk), ("local", f"local:{tenancy.pk}", tenancy.pk)},
        )

    def test_linked_profile_uid_held_by_another_user_is_history_not_refusal(self):
        staff = make_staff_user("one")
        other = User.objects.create_user("two")
        uid = str(uuid.uuid4())
        UserIdentity.objects.create(user=other, provider=LEGACY_SUPABASE_PROVIDER, subject=uid)
        self.make_profile(uid, user=staff)
        report = retire_legacy_columns(self.H)
        self.assertEqual(report["uncaptured_profiles"], [])


class MigrationWiringTests(LegacyColumnsTestCase):
    def _forwards(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            MIGRATION.forwards(self.H, None)
        return out.getvalue()

    def test_silent_on_empty_database(self):
        self.assertEqual(self._forwards(), "")

    def test_prints_archive_and_link_counts(self):
        self.make_profile(str(uuid.uuid4()), user=None)
        self.set_customer_uid(_customer("print@example.com"), uuid.uuid4())
        text = self._forwards()
        self.assertIn("archived to legacy_external_id: 1", text)
        self.assertIn("customer users created: 1", text)
        self.assertIn("backfill_parties --step 7", text)

    def test_refusal_fails_the_migration(self):
        staff = make_staff_user("blocked")
        self.set_customer_uid(_customer("b@example.com", user=staff), uuid.uuid4())
        with self.assertRaises(LegacyIdentityNotCaptured):
            self._forwards()

    def test_backwards_refills_both_columns(self):
        customer, sub = _portal_customer("back@example.com")
        orphan_uid = str(uuid.uuid4())
        orphan = self.make_profile(orphan_uid, user=None)
        staff = make_staff_user("back-staff")
        staff_uid = str(uuid.uuid4())
        linked = self.make_profile(staff_uid, user=staff)
        self._forwards()
        # As if the columns had been dropped and re-added empty.
        self.set_customer_uid(customer, None)
        self.set_profile_uid(orphan, None)
        self.set_profile_uid(linked, None)

        MIGRATION.backwards(self.H, None)
        self.assertEqual(self.customer_uid(customer), uuid.UUID(sub))
        self.assertEqual(self.profile_uid(orphan), orphan_uid)
        self.assertEqual(self.profile_uid(linked), staff_uid)

    def test_restore_never_steals_a_held_value(self):
        customer, sub = _portal_customer("held@example.com")
        other = _customer("other@example.com")
        self.set_customer_uid(other, uuid.UUID(sub))
        report = restore_legacy_columns(self.H)
        self.assertEqual(report["customers"], 0)
        self.assertIsNone(self.customer_uid(customer))
