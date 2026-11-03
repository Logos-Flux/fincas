"""Storefront service identity (F-037, docs/decisions-pending.md §2.2).

The storefront's shared api-service SUPERUSER JWT is replaced by a
dedicated least-privilege identity (``storefront-api`` user /
``storefront`` group — see core/storefront.py). These tests pin:

1. Provisioning: idempotent, non-staff, non-superuser, unusable password,
   and exactly the contract permissions (catalog read + order creation).
2. Issuance: ``issue_api_jwt(sub=...)`` + the public, throttled
   /api/v1/core/auth/storefront-token/ endpoint + the Api-Key-gated
   token endpoint's optional ``subject`` body field.
3. Positive contract: the storefront token reads the catalog and creates
   guest orders carrying ``guest_email`` with no Customer FK link.
4. Negative contract (the point of the decision): a leaked storefront
   token is near-powerless — no order reads/writes beyond create, no
   catalog writes, no ERP endpoints, no admin/company access, and it
   cannot forge a superuser token by tampering with ``sub``.
"""

from __future__ import annotations

import secrets
import uuid

import jwt
from django.contrib.auth.models import Group, User
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from core.authentication import get_jwt_signing_key, issue_api_jwt
from core.models import Company
from core.storefront import (
    STOREFRONT_GROUP,
    STOREFRONT_PERMISSIONS,
    STOREFRONT_USERNAME,
    ensure_storefront_identity,
    is_storefront_user,
)
from customers.models import Customer
from orders.models import Order

API_KEY = "test-" + secrets.token_hex(16)


def _storefront_token(lifetime_s: int = 3600) -> str:
    return issue_api_jwt(lifetime_s=lifetime_s, sub="storefront")


def _service_token() -> str:
    return issue_api_jwt()


def _checkout_payload(**overrides):
    payload = {
        "customer_name": "Guest Shopper",
        "company_name": "GuestCo",
        "email": "guest@example.com",
        "shipping_address": "1 Main St, Springfield",
        "payment_method": "net_30",
        "po_number": "PO-123",
        "notes": "Leave at the dock",
        "items": [{"product_id": 1, "quantity": 2, "unit_price": "10.00"}],
    }
    payload.update(overrides)
    return payload


@override_settings(API_KEY=API_KEY)
class StorefrontIdentityProvisioningTests(TestCase):
    def test_provisioning_creates_low_privilege_user(self):
        user = ensure_storefront_identity()
        self.assertEqual(user.username, STOREFRONT_USERNAME)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.is_active)
        self.assertFalse(user.has_usable_password())

    def test_provisioning_is_idempotent(self):
        first = ensure_storefront_identity()
        second = ensure_storefront_identity()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(User.objects.filter(username=STOREFRONT_USERNAME).count(), 1)
        self.assertEqual(Group.objects.filter(name=STOREFRONT_GROUP).count(), 1)

    def test_group_has_exactly_the_contract_permissions(self):
        ensure_storefront_identity()
        group = Group.objects.get(name=STOREFRONT_GROUP)
        actual = {
            f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.all()
        }
        expected = {f"{app}.{codename}" for app, codename in STOREFRONT_PERMISSIONS}
        self.assertEqual(actual, expected)

    def test_provisioning_repairs_escalated_flags(self):
        user = ensure_storefront_identity()
        user.is_staff = True
        user.is_superuser = True
        user.save()
        repaired = ensure_storefront_identity()
        self.assertFalse(repaired.is_staff)
        self.assertFalse(repaired.is_superuser)

    def test_is_storefront_user_marker(self):
        user = ensure_storefront_identity()
        self.assertTrue(is_storefront_user(user))
        self.assertFalse(is_storefront_user(User(username="someone-else")))


@override_settings(API_KEY=API_KEY)
class StorefrontTokenIssuanceTests(TestCase):
    def test_public_storefront_token_endpoint(self):
        response = APIClient().post("/api/v1/core/auth/storefront-token/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["subject"], "storefront")
        payload = jwt.decode(body["token"], get_jwt_signing_key(), algorithms=["HS256"])
        self.assertEqual(payload["sub"], "storefront")

    def test_public_endpoint_follows_api_key_unless_switched(self):
        url = "/api/v1/core/auth/storefront-token/"
        with override_settings(API_KEY=""):
            self.assertEqual(APIClient().post(url).status_code, 500)
            with override_settings(GOLDEN_STOREFRONT_TOKENS=True):
                self.assertEqual(APIClient().post(url).status_code, 200)
        with override_settings(GOLDEN_STOREFRONT_TOKENS=False):
            self.assertEqual(APIClient().post(url).status_code, 500)

    def test_token_endpoint_subject_selection(self):
        client = APIClient(HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")
        response = client.post(
            "/api/v1/core/auth/token/",
            {"subject": "storefront"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["subject"], "storefront")

    def test_token_endpoint_default_subject_unchanged(self):
        client = APIClient(HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")
        response = client.post("/api/v1/core/auth/token/", {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["subject"], "api-service")

    def test_unknown_subject_refused_at_issuance(self):
        with self.assertRaises(ValueError):
            issue_api_jwt(sub="admin")
        with self.assertRaises(ValueError):
            issue_api_jwt(sub="storefront-api")

    def test_bearer_auth_maps_storefront_subject_to_identity(self):
        token = _storefront_token()
        client = APIClient(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = client.get("/api/v1/products/brands/")
        self.assertEqual(response.status_code, 200)

    def test_forged_subject_does_not_resolve_to_superuser(self):
        import time

        now = int(time.time())
        payload = {
            "iss": "golden-erp", "iat": now, "exp": now + 60,
            "jti": "forged", "sub": "admin",
        }
        token = jwt.encode(payload, get_jwt_signing_key(), algorithm="HS256")
        client = APIClient(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = client.get("/api/v1/products/brands/")
        self.assertEqual(response.status_code, 401)


@override_settings(API_KEY=API_KEY)
class StorefrontGuestContractPositiveTests(TestCase):
    """What the storefront token legitimately CAN do."""

    def setUp(self):
        self.company = Company.objects.create(name="SoloCo")
        self.client = APIClient(HTTP_AUTHORIZATION=f"Bearer {_storefront_token()}")

    def _create_guest_order(self, **overrides):
        return self.client.post(
            "/api/v1/orders/orders/", _checkout_payload(**overrides), format="json"
        )

    def test_catalog_read(self):
        response = self.client.get("/api/v1/products/products/")
        self.assertEqual(response.status_code, 200)

    def test_brand_and_variant_reads(self):
        self.assertEqual(self.client.get("/api/v1/products/brands/").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/products/variants/").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/pricing/price-lists/").status_code, 200)

    def test_guest_order_created_with_email_and_no_customer(self):
        response = self._create_guest_order()
        self.assertEqual(response.status_code, 201, response.json())
        order = Order.objects.get(pk=response.json()["id"])
        self.assertIsNone(order.customer)
        self.assertEqual(order.guest_email, "guest@example.com")
        self.assertEqual(order.company, self.company)
        self.assertEqual(order.channel, "storefront")
        self.assertEqual(order.status, Order.Status.PENDING)
        # The checkout payload snapshot survives for staff review.
        self.assertEqual(order.metadata["customer_name"], "Guest Shopper")
        self.assertEqual(order.metadata["shipping_address"], "1 Main St, Springfield")
        self.assertEqual(order.metadata["payment_method"], "net_30")
        self.assertEqual(order.customer_po_number, "PO-123")
        self.assertEqual(order.customer_message, "Leave at the dock")
        self.assertEqual(order.payment_terms, "net_30")

    def test_guest_order_never_trusts_client_financials(self):
        payload = _checkout_payload()
        payload["grand_total"] = "0.01"
        payload["status"] = Order.Status.COMPLETED
        payload["discount_amount"] = "999.00"
        response = self.client.post("/api/v1/orders/orders/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        order = Order.objects.get(pk=response.json()["id"])
        self.assertEqual(order.grand_total, 0)
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(order.discount_amount, 0)

    def test_guest_order_requires_email(self):
        response = self._create_guest_order(email="")
        self.assertEqual(response.status_code, 400)
        self.assertIn("guest_email", response.json())

    def test_guest_order_fails_closed_with_multiple_companies(self):
        Company.objects.create(name="SecondCo")
        response = self._create_guest_order()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Order.objects.exists())


@override_settings(API_KEY=API_KEY)
class StorefrontTokenCannotWriteErpTests(TestCase):
    """A leaked storefront token is near-powerless outside its contract."""

    def setUp(self):
        self.company = Company.objects.create(name="SoloCo")
        self.customer = Customer.objects.create(
            first_name="C", last_name="Cust", email="c@example.com",
            company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer, company=self.company,
        )
        self.client = APIClient(HTTP_AUTHORIZATION=f"Bearer {_storefront_token()}")

    def test_extra_group_perms_never_grant_change_or_delete(self):
        # Its only write verb is `add`, whatever an admin adds to the group.
        from django.contrib.auth.models import Permission

        from products.models import Brand

        brand = Brand.objects.create(title="Acme")
        group = Group.objects.get(name=STOREFRONT_GROUP)
        for codename in ("change_brand", "delete_brand"):
            group.permissions.add(
                Permission.objects.get(content_type__app_label="products", codename=codename))
        url = f"/api/v1/products/brands/{brand.pk}/"
        self.assertEqual(self.client.patch(url, {"title": "Pwned"}, format="json").status_code, 403)
        self.assertEqual(self.client.delete(url).status_code, 403)
        brand.refresh_from_db()
        self.assertEqual(brand.title, "Acme")

    def test_cannot_list_or_read_orders(self):
        self.assertEqual(self.client.get("/api/v1/orders/orders/").status_code, 403)
        self.assertEqual(
            self.client.get(f"/api/v1/orders/orders/{self.order.pk}/").status_code, 403
        )

    def test_cannot_patch_order(self):
        response = self.client.patch(
            f"/api/v1/orders/orders/{self.order.pk}/",
            {"customer_message": "tampered"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.order.refresh_from_db()
        self.assertEqual(self.order.customer_message, "")

    def test_cannot_transition_order(self):
        response = self.client.post(
            f"/api/v1/orders/orders/{self.order.pk}/transition/",
            {"status": Order.Status.COMPLETED},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_cannot_create_catalog_rows(self):
        response = self.client.post(
            "/api/v1/products/products/",
            {"title": "Hacked", "part_number": "HK-1"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_cannot_write_global_lookup_tables(self):
        self.assertEqual(
            self.client.post("/api/v1/products/brands/", {"title": "X"}, format="json").status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/pricing/tax-rates/", {"name": "X"}, format="json"
            ).status_code,
            403,
        )

    def test_cannot_reach_erp_endpoints(self):
        # Suppliers / purchasing, customers, invoices, companies, audit log.
        for url in (
            "/api/v1/purchasing/suppliers/",
            "/api/v1/customers/customers/",
            "/api/v1/orders/sales-invoices/",
            "/api/v1/core/companies/",
            "/api/v1/core/audit-logs/",
            "/api/v1/reporting/reports/",
            "/api/v1/webhooks/subscriptions/",
        ):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_cannot_reach_any_parties_route(self):
        """phase2-party.md 5.2: the storefront group holds no parties permission."""
        from parties.models import Party

        party = Party.objects.create(company=self.company, kind="person", given_name="P",
                                     display_name="P", origin="erp")
        for route in ("parties/", "emails/", "phones/", "addresses/", "roles/",
                      "relationships/", "consents/", "match/"):
            url = f"/api/v1/parties/{route}"
            self.assertEqual(self.client.get(url).status_code, 403, url)
            self.assertEqual(self.client.post(url, {}, format="json").status_code, 403, url)
        detail = f"/api/v1/parties/parties/{party.pk}/"
        self.assertEqual(self.client.get(detail).status_code, 403)
        self.assertEqual(self.client.patch(detail, {"tags": ["x"]}, format="json").status_code, 403)

    def test_cannot_reach_any_crm_phase3_route(self):
        """phase3-engagement.md 5.2: the storefront group holds no crm / communications permission."""
        from crm.engagements import create

        engagement = create(company=self.company)
        routes = [f"/api/v1/crm/{r}" for r in (
            "pipelines/", "stages/", "engagements/", "engagement-contacts/", "stage-changes/",
            "tasks/", "touchpoints/", "me/")] + ["/api/v1/communications/communications/"]
        for url in routes:
            self.assertEqual(self.client.get(url).status_code, 403, url)
            self.assertEqual(self.client.post(url, {}, format="json").status_code, 403, url)
        detail = f"/api/v1/crm/engagements/{engagement.pk}/"
        self.assertEqual(self.client.get(detail).status_code, 403)
        self.assertEqual(self.client.post(f"{detail}win/", {}, format="json").status_code, 403)

    def test_cannot_reconcile_invoices(self):
        from orders.models import SalesInvoice

        invoice = SalesInvoice.objects.create(
            order=self.order, company=self.company,
            payment_token=uuid.uuid4(),
        )
        response = self.client.post(
            f"/api/v1/orders/sales-invoices/by-token/{invoice.payment_token}/reconcile/",
            {"event": "paid", "amount_cents": 100, "stripe_payment_intent_id": "pi_1"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_cannot_redeem_discounts(self):
        response = self.client.post(
            "/api/v1/discounts/redeem/",
            {"code": "ABC", "order_number": "SO-00001", "discount_applied": "1.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_cannot_open_admin_or_erp_html(self):
        self.assertNotEqual(self.client.get("/erp/").status_code, 200)
        self.assertNotEqual(self.client.get("/admin/").status_code, 200)

    def test_api_service_token_still_has_full_access(self):
        """The superuser path is unchanged for genuine consumers."""
        client = APIClient(HTTP_AUTHORIZATION=f"Bearer {_service_token()}")
        self.assertEqual(client.get("/api/v1/purchasing/suppliers/").status_code, 200)
        self.assertEqual(client.get("/api/v1/orders/orders/").status_code, 200)


class StorefrontIdentityNoBootWritesTests(TestCase):
    """App start never touches the database; migrate and the command do."""

    def _wipe(self):
        User.objects.filter(username=STOREFRONT_USERNAME).delete()
        Group.objects.filter(name=STOREFRONT_GROUP).delete()

    def _assert_identity(self):
        user = User.objects.get(username=STOREFRONT_USERNAME)
        self.assertFalse(user.is_staff or user.is_superuser or user.has_usable_password())
        perms = {
            f"{p.content_type.app_label}.{p.codename}"
            for p in Group.objects.get(name=STOREFRONT_GROUP).permissions.all()
        }
        self.assertEqual(perms, {f"{a}.{c}" for a, c in STOREFRONT_PERMISSIONS})

    def test_core_ready_performs_no_queries(self):
        from django.apps import apps
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._wipe()
        with CaptureQueriesContext(connection) as ctx:
            apps.get_app_config("core").ready()
        self.assertEqual(len(ctx.captured_queries), 0, ctx.captured_queries)
        self.assertFalse(User.objects.filter(username=STOREFRONT_USERNAME).exists())

    def test_post_migrate_recreates_missing_identity(self):
        from django.core.management.sql import emit_post_migrate_signal

        self._wipe()
        emit_post_migrate_signal(verbosity=0, interactive=False, db="default")
        self._assert_identity()

    def test_post_migrate_receiver_ignores_other_databases(self):
        from core.storefront import ensure_storefront_identity_after_migrate

        self._wipe()
        ensure_storefront_identity_after_migrate(sender=None, using="other")
        self.assertFalse(User.objects.filter(username=STOREFRONT_USERNAME).exists())

    def test_command_creates_and_repairs_identity(self):
        from io import StringIO

        from django.core.management import call_command

        self._wipe()
        out = StringIO()
        call_command("ensure_storefront_identity", stdout=out)
        self._assert_identity()
        self.assertIn("7/7 contract permissions", out.getvalue())

        user = User.objects.get(username=STOREFRONT_USERNAME)
        user.is_superuser = True
        user.set_password("x")
        user.save()
        call_command("ensure_storefront_identity", stdout=StringIO())
        self._assert_identity()


class StorefrontBootSubprocessTests(SimpleTestCase):
    def test_manage_check_boots_without_database_warning(self):
        """A fresh process start must not access the database.

        Django emits "Accessing the database during app initialization is
        discouraged" (RuntimeWarning) when ready() queries; -W error turns
        that into a non-zero exit.
        """
        import subprocess
        import sys

        from django.conf import settings

        result = subprocess.run(
            [sys.executable, "-W", "error::RuntimeWarning", "manage.py", "check",
             "--settings=config.settings.test"],
            cwd=settings.BASE_DIR, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])


class StorefrontIdentityMigrationTests(TestCase):
    """``core.0002`` provisions the identity on its historical models (issue #88)."""

    def test_the_migration_runs_on_historical_models(self):
        import importlib

        from django.db.migrations.loader import MigrationLoader

        with override_settings(MIGRATION_MODULES={}):  # the test settings disable migrations
            loader = MigrationLoader(None, ignore_no_migrations=True)
        state_apps = loader.project_state(("core", "0002_storefront_identity")).apps
        User.objects.filter(username=STOREFRONT_USERNAME).delete()
        module = importlib.import_module("core.migrations.0002_storefront_identity")
        module._provision(state_apps, None)
        user = User.objects.get(username=STOREFRONT_USERNAME)
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertTrue(user.groups.filter(name=STOREFRONT_GROUP).exists())
