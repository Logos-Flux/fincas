"""Tests for the one-time tenancy backfill (core/management/commands/backfill_tenancy.py)."""

import io
import uuid

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from core.models import Company, UserProfile
from discounts.models import DiscountCode
from inventory.models import Warehouse


def _run(*args, **kwargs):
    out = io.StringIO()
    call_command("backfill_tenancy", *args, stdout=out, **kwargs)
    return out.getvalue()


class BackfillTenancyTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme Holdings")
        self.warehouse = Warehouse.objects.create(
            warehouse_code="WH-1", name="Main", company=None
        )

    # --- company resolution -------------------------------------------------

    def test_resolves_company_by_uuid(self):
        out = _run("--company", str(self.company.pk))
        self.assertIn(f"({self.company.pk})", out)
        self.assertIn("OK", out)

    def test_resolves_company_by_exact_name(self):
        out = _run("--company", "Acme Holdings")
        self.assertIn(f"({self.company.pk})", out)

    def test_unknown_company_is_an_error(self):
        with self.assertRaisesMessage(CommandError, "No Company matches"):
            _run("--company", str(uuid.uuid4()))

    def test_ambiguous_name_is_an_error(self):
        Company.objects.create(name="Acme Holdings")
        with self.assertRaisesMessage(CommandError, "ambiguous"):
            _run("--company", "Acme Holdings")
        self.warehouse.refresh_from_db()
        self.assertIsNone(self.warehouse.company_id)

    # --- step 1: warehouses -------------------------------------------------

    def test_warehouses_without_company_are_assigned(self):
        out = _run("--company", str(self.company.pk))
        self.warehouse.refresh_from_db()
        self.assertEqual(self.warehouse.company, self.company)
        self.assertIn("step 1 warehouses without company: 1 assigned", out)

    # --- step 2: profile creation -------------------------------------------

    def test_active_user_without_profile_gets_a_profile(self):
        User = get_user_model()
        named = User.objects.create_user("jdoe", first_name="Jane", last_name="Doe")
        bare = User.objects.create_user("sam")
        out = _run("--company", str(self.company.pk))

        profile = UserProfile.objects.get(user=named)
        self.assertEqual(profile.company, self.company)
        self.assertEqual(profile.display_name, "Jane Doe")
        # No placeholder uid since identity step 8 (the column is gone).
        self.assertEqual(profile.legacy_external_id, "")

        fallback = UserProfile.objects.get(user=bare)
        self.assertEqual(fallback.display_name, "sam")
        # Only the two humans: service accounts (storefront-api, api-service,
        # both present as in every real DB) get no profile (identity step 5).
        self.assertIn("step 2 active users without profile: 2 created", out)

    def test_placeholder_prefix_flag_is_gone(self):
        with self.assertRaises(CommandError):
            _run("--company", str(self.company.pk), "--placeholder-uid-prefix", "stage:")

    def test_customer_only_user_gets_no_staff_profile(self):
        from core.tests.helpers import make_staff_user
        from customers.models import Customer

        User = get_user_model()
        shopper = User.objects.create_user("u-shopper")
        Customer.objects.create(first_name="S", last_name="H", email="s@example.com", user=shopper)
        both = make_staff_user("dual-role")
        Customer.objects.create(first_name="D", last_name="R", email="d@example.com", user=both)
        _run("--company", str(self.company.pk))
        self.assertFalse(UserProfile.objects.filter(user=shopper).exists())
        self.assertTrue(UserProfile.objects.filter(user=both).exists())

    def test_inactive_user_is_skipped(self):
        User = get_user_model()
        User.objects.create_user("gone", is_active=False)
        _run("--company", str(self.company.pk))
        self.assertFalse(UserProfile.objects.filter(user__username="gone").exists())

    # --- step 3: existing profiles ------------------------------------------

    def test_profile_with_null_company_is_fixed(self):
        user = get_user_model().objects.create_user("linked")
        profile = UserProfile.objects.create(user=user)
        out = _run("--company", str(self.company.pk))
        profile.refresh_from_db()
        self.assertEqual(profile.company, self.company)
        self.assertIn("step 3 profiles without company: 1 assigned", out)

    def test_unlinked_profile_is_reported_but_never_linked(self):
        orphan = UserProfile.objects.create(display_name="Orphan Person")
        out = _run("--company", str(self.company.pk))
        orphan.refresh_from_db()
        self.assertIsNone(orphan.user_id)
        self.assertEqual(orphan.company, self.company)
        self.assertIn("unlinked profiles (user NULL, NOT linked): 1", out)
        self.assertIn(f"{orphan.pk} Orphan Person", out)

    # --- step 4: other scoped models ----------------------------------------

    def test_also_backfills_named_scoped_model_only(self):
        listed = DiscountCode.objects.create(code="LISTED", value=10, company=None)
        out = _run(
            "--company", str(self.company.pk), "--also", "discounts.DiscountCode"
        )
        listed.refresh_from_db()
        self.assertEqual(listed.company, self.company)
        self.assertIn("discounts.DiscountCode: 1 assigned (--also)", out)

    def test_scoped_model_without_also_is_reported_not_modified(self):
        code = DiscountCode.objects.create(code="UNTOUCHED", value=10, company=None)
        out = _run("--company", str(self.company.pk))
        code.refresh_from_db()
        self.assertIsNone(code.company_id)
        self.assertIn("discounts.DiscountCode: 1 NULL (not modified)", out)

    def test_also_rejects_a_model_that_is_not_company_scoped(self):
        with self.assertRaisesMessage(CommandError, "not a CompanyScopedManager model"):
            _run("--company", str(self.company.pk), "--also", "inventory.Warehouse")

    # --- dry run and idempotency --------------------------------------------

    def test_dry_run_leaves_database_unchanged(self):
        User = get_user_model()
        user = User.objects.create_user("dry")
        orphan = UserProfile.objects.create(company=None)
        code = DiscountCode.objects.create(code="DRY", value=10, company=None)

        out = _run(
            "--company", str(self.company.pk), "--dry-run",
            "--also", "discounts.DiscountCode",
        )

        self.warehouse.refresh_from_db()
        orphan.refresh_from_db()
        code.refresh_from_db()
        self.assertIsNone(self.warehouse.company_id)
        self.assertIsNone(orphan.company_id)
        self.assertIsNone(code.company_id)
        self.assertFalse(UserProfile.objects.filter(user=user).exists())
        # The report still describes what would have happened.
        # Service accounts (storefront-api, api-service) get no profile.
        self.assertIn("step 2 active users without profile: 1 created", out)
        self.assertIn("DRY RUN (rolled back): no changes were saved.", out)
        self.assertNotIn("\nOK\n", out)

    def test_second_run_reports_zero_changes(self):
        get_user_model().objects.create_user("once")
        DiscountCode.objects.create(code="ONCE", value=10, company=None)
        args = ["--company", str(self.company.pk), "--also", "discounts.DiscountCode"]
        _run(*args)

        out = _run(*args)
        self.assertIn("step 1 warehouses without company: 0 assigned", out)
        self.assertIn("step 2 active users without profile: 0 created", out)
        self.assertIn("step 3 profiles without company: 0 assigned", out)
        self.assertIn("discounts.DiscountCode: 0 assigned (--also)", out)
        self.assertIn("OK", out)
