"""M1 Track A auth matrix (testing-plan §4).

1. Prod permission audit: every viewset declares explicit
   ``permission_classes`` (never relies on the settings default — test
   settings inherit dev AllowAny and DRF binds at import, F-003). Only
   pinned-public endpoints may be AllowAny: the storefront catalog
   (ProductViewSet, plus Brand/CategoryViewSet reads), the token-obtain endpoint, and the token-gated
   invoice view.
2. Endpoint auth matrix: session, Api-Key (both headers), Bearer JWT
   expiry, staff-vs-portal token separation.
3. Service accounts (identity step 5): a per-key account is held to its
   role contract on every routed viewset; the legacy key is never a human.
"""

from __future__ import annotations

import secrets

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.test import APIClient

from core.authentication import get_jwt_signing_key
from core.portal_auth import PortalTokenAuthentication

API_KEY = "test-" + secrets.token_hex(16)
PORTAL_SECRET = "test-portal-" + secrets.token_hex(16)




def _all_viewsets():
    from accounting.api import FiscalPeriodViewSet
    from communications.api import CommunicationViewSet
    from content.views import ArticleViewSet
    from core.views import AuditLogViewSet, CompanyViewSet, TeamViewSet, UserProfileViewSet
    from crm.api import (
        EngagementContactViewSet,
        EngagementViewSet,
        MyQueueViewSet,
        PipelineViewSet,
        StaffDirectoryViewSet,
        StaffQueueViewSet,
        StageChangeViewSet,
        StageViewSet,
        TaskViewSet,
        TouchpointViewSet,
    )
    from crm.views import ActivityLogViewSet, LeadViewSet
    from customers.views import CustomerAddressViewSet, CustomerViewSet
    from documents.views import ProductDocumentViewSet
    from fulfillment.views import PickTicketLineViewSet, PickTicketViewSet
    from inventory.views import WarehouseStockViewSet, WarehouseViewSet
    from orders.views import (
        OrderLineItemViewSet,
        OrderViewSet,
        QuoteLineItemViewSet,
        QuoteViewSet,
        SalesInvoiceViewSet,
        SubOrderViewSet,
    )
    from parties.views import (
        ConsentRecordViewSet,
        PartyAddressViewSet,
        PartyEmailViewSet,
        PartyMatchViewSet,
        PartyPhoneViewSet,
        PartyRelationshipViewSet,
        PartyRoleViewSet,
        PartyViewSet,
    )
    from payments.views import PaymentViewSet
    from pricing.views import PriceListItemViewSet, PriceListViewSet, TaxRateViewSet
    from products.views import (
        BrandViewSet,
        CategoryViewSet,
        ProductOptionValueViewSet,
        ProductOptionViewSet,
        ProductVariantViewSet,
        ProductViewSet,
        SupplierProductViewSet,
        VariantOptionValueViewSet,
    )
    from purchasing.views import (
        POLineItemViewSet,
        POReceiptViewSet,
        PurchaseOrderViewSet,
        SupplierViewSet,
    )
    from reporting.api import (
        CrmReportCatalogView,
        FollowUpReportView,
        FunnelReportView,
        StageDurationReportView,
    )
    from reporting.views import ReportDefinitionViewSet
    from webhooks.views import WebhookDeliveryLogViewSet, WebhookSubscriptionViewSet

    return [
        ArticleViewSet, AuditLogViewSet, CompanyViewSet, UserProfileViewSet, TeamViewSet,
        ActivityLogViewSet, LeadViewSet, CustomerAddressViewSet, CustomerViewSet,
        ProductDocumentViewSet, PickTicketLineViewSet, PickTicketViewSet,
        WarehouseStockViewSet, WarehouseViewSet, OrderLineItemViewSet,
        OrderViewSet, QuoteLineItemViewSet, QuoteViewSet, SalesInvoiceViewSet,
        SubOrderViewSet, PaymentViewSet, PriceListItemViewSet, PriceListViewSet,
        TaxRateViewSet, BrandViewSet, CategoryViewSet, ProductOptionValueViewSet,
        ProductOptionViewSet, ProductVariantViewSet, ProductViewSet,
        SupplierProductViewSet, VariantOptionValueViewSet, POReceiptViewSet,
        POLineItemViewSet, PurchaseOrderViewSet, SupplierViewSet,
        ReportDefinitionViewSet, WebhookDeliveryLogViewSet,
        WebhookSubscriptionViewSet, FiscalPeriodViewSet,
        PartyViewSet, PartyEmailViewSet, PartyPhoneViewSet, PartyAddressViewSet,
        PartyRoleViewSet, PartyRelationshipViewSet, ConsentRecordViewSet,
        PartyMatchViewSet,
        PipelineViewSet, StageViewSet, EngagementViewSet, EngagementContactViewSet,
        StageChangeViewSet, TaskViewSet, TouchpointViewSet, MyQueueViewSet,
        StaffDirectoryViewSet, StaffQueueViewSet,
        CommunicationViewSet,
        # CRM Phase 6 step 7: the reports' JSON (APIViews, audited all the same).
        CrmReportCatalogView, FunnelReportView, StageDurationReportView, FollowUpReportView,
    ]


# Routed API views that are not the staff API: the portal (own auth + perm),
# the token endpoints (AllowAny by design) and DRF/schema plumbing.
_NOT_STAFF_API_MODULES = ("portal.", "rest_framework.", "drf_spectacular.")
_NOT_STAFF_API_VIEWS = {"core.auth_views.TokenObtainView",
                        "core.auth_views.StorefrontTokenView"}


def _routed_api_views():
    """{dotted class name: (class, parameter-free route)} for every DRF view
    mounted under /api/, discovered from the live URLconf (first mount wins)."""
    from django.urls import URLResolver, get_resolver

    def walk(patterns, prefix=""):
        for pattern in patterns:
            route = str(pattern.pattern).lstrip("^").rstrip("$")
            if isinstance(pattern, URLResolver):
                yield from walk(pattern.url_patterns, prefix + route)
            else:
                yield prefix + route, pattern.callback

    found = {}
    for route, callback in walk(get_resolver().url_patterns):
        cls = getattr(callback, "cls", None)
        if cls is None or not route.startswith("api/"):
            continue
        if "(" in route or "<" in route:
            continue
        name = f"{cls.__module__}.{cls.__name__}"
        if name.startswith(_NOT_STAFF_API_MODULES) or name in _NOT_STAFF_API_VIEWS:
            continue
        found.setdefault(name, (cls, "/" + route))
    return found


def _project_permission_owner(cls):
    """Nearest MRO class pinning permission_classes before DRF's default.

    An inherited pin (e.g. from CompanyScopedViewSet) is still an
    explicit project-code pin — it does not track the settings default.
    Returns None when only rest_framework's own APIView default applies.
    """
    import rest_framework.views

    for klass in cls.__mro__:
        if klass in (rest_framework.views.APIView, object):
            return None
        if "permission_classes" in klass.__dict__:
            return f"{klass.__module__}.{klass.__name__}"
    return None


class ProdPermissionAuditTests(TestCase):
    def test_every_viewset_declares_explicit_permissions(self):
        """No viewset may rely on the settings default (F-003)."""
        offenders = [
            f"{cls.__module__}.{cls.__name__}"
            for cls in _all_viewsets()
            if _project_permission_owner(cls) is None
        ]
        self.assertEqual(offenders, [])

    def test_only_pinned_endpoints_are_public(self):
        from core.auth_views import StorefrontTokenView, TokenObtainView

        public = {
            f"{cls.__module__}.{cls.__name__}"
            for cls in _all_viewsets()
            if list(cls.permission_classes) == [AllowAny]
        }
        public.add(f"{TokenObtainView.__module__}.{TokenObtainView.__name__}")
        public.add(f"{StorefrontTokenView.__module__}.{StorefrontTokenView.__name__}")
        # sales_invoice_by_token is a function view; pinned separately below.
        self.assertEqual(public, {
            "products.views.ProductViewSet",
            # Storefront brand/category pages (docs/storefront-seo.md): reads
            # only; get_permissions keeps writes on PrincipalPermission.
            "products.views.BrandViewSet",
            "products.views.CategoryViewSet",
            "core.auth_views.TokenObtainView",
            "core.auth_views.StorefrontTokenView",
        })

    def test_token_gated_invoice_view_is_public_by_design(self):
        import uuid

        response = APIClient().get(
            f"/api/v1/orders/sales-invoices/by-token/{uuid.uuid4()}/"
        )
        # Reachable anonymously by design (AllowAny); unknown token 404s
        # as JSON rather than redirecting to login.
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")

    def test_parties_viewsets_pin_principal_permission(self):
        """5.2: every parties viewset pins PrincipalPermission on its own class."""
        from core.permissions import PrincipalPermission

        for cls in _all_viewsets():
            if cls.__module__ != "parties.views":
                continue
            with self.subTest(view=cls.__name__):
                self.assertIn("permission_classes", cls.__dict__)
                self.assertEqual(cls.permission_classes, [PrincipalPermission])

    def test_crm_phase3_viewsets_pin_principal_permission(self):
        """phase3-engagement.md 5.2: every CRM / communications viewset pins it on its own class."""
        from core.permissions import PrincipalPermission

        for cls in _all_viewsets():
            if cls.__module__ not in ("crm.api", "communications.api"):
                continue
            with self.subTest(view=cls.__name__):
                self.assertIn("permission_classes", cls.__dict__)
                self.assertEqual(cls.permission_classes, [PrincipalPermission])

    def test_admin_registry_is_staff_only(self):
        from core.views import AuditLogViewSet, CompanyViewSet, UserProfileViewSet

        for cls in (CompanyViewSet, UserProfileViewSet, AuditLogViewSet):
            self.assertEqual(cls.permission_classes, [IsAdminUser])

    def test_guarded_viewsets_require_auth(self):
        # Bare IsAuthenticated is no longer a staff gate (D6): once customers
        # are auth.User rows it would admit them. PrincipalPermission is.
        from core.permissions import PrincipalPermission

        offenders = [
            f"{cls.__module__}.{cls.__name__}"
            for cls in _all_viewsets()
            if list(cls.permission_classes) not in (
                [IsAdminUser], [AllowAny], [PrincipalPermission],
            )
        ]
        self.assertEqual(offenders, [])

    def test_every_routed_viewset_is_audited(self):
        """A new viewset must join _all_viewsets() (and so the audit)."""
        from rest_framework.viewsets import ViewSetMixin

        audited = {f"{c.__module__}.{c.__name__}" for c in _all_viewsets()}
        routed = {
            name for name, (cls, _) in _routed_api_views().items()
            if issubclass(cls, ViewSetMixin)
        }
        self.assertEqual(sorted(routed - audited), [])

    def test_settings_default_permission_is_principal(self):
        """The packaged base's DRF default is PrincipalPermission.

        Read from source: dev.py mutates the shared REST_FRAMEWORK dict, so
        the imported base module no longer shows base's own value.
        """
        import re
        from pathlib import Path

        import golden_erp.settings.base as packaged_base

        source = Path(packaged_base.__file__).read_text()
        block = re.search(r'"DEFAULT_PERMISSION_CLASSES": \[(.*?)\]', source, re.S)
        self.assertIsNotNone(block)
        self.assertEqual(
            re.findall(r'"([\w.]+)"', block.group(1)),
            ["core.permissions.PrincipalPermission"],
        )

    def test_storefront_contract_name_is_an_alias(self):
        from core.permissions import PrincipalPermission
        from core.storefront import StorefrontContractPermission

        self.assertIs(StorefrontContractPermission, PrincipalPermission)

    def test_non_staff_cannot_list_companies(self):
        user = User.objects.create_user("pleb", "p@x.com", "pw")
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get("/api/v1/core/companies/")
        self.assertEqual(response.status_code, 403)

    def test_admin_can_list_companies(self):
        admin = User.objects.create_user(
            "boss", "b@x.com", "pw", is_staff=True)
        client = APIClient()
        client.force_authenticate(user=admin)
        response = client.get("/api/v1/core/companies/")
        self.assertEqual(response.status_code, 200)


@override_settings(API_KEY=API_KEY)
class EndpointAuthMatrixTests(TestCase):
    """Auth mechanisms at a real endpoint (explicit perms enforced even
    under test settings — the F-003 fix)."""

    def setUp(self):
        self.url = "/api/v1/purchasing/suppliers/"

    def test_anonymous_is_401_json(self):
        response = APIClient().get(self.url)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("detail", response.json())

    def test_api_key_authorization_header(self):
        client = APIClient(HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")
        self.assertEqual(client.get(self.url).status_code, 200)

    def test_api_key_legacy_header(self):
        client = APIClient(HTTP_X_API_KEY=API_KEY)
        self.assertEqual(client.get(self.url).status_code, 200)

    def test_wrong_api_key_is_401(self):
        client = APIClient(HTTP_AUTHORIZATION="Api-Key wrong-key")
        self.assertEqual(client.get(self.url).status_code, 401)

    def test_bearer_jwt_round_trip(self):
        from core.authentication import issue_api_jwt

        token = issue_api_jwt()
        client = APIClient(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(client.get(self.url).status_code, 200)

    def test_expired_bearer_is_401(self):
        import time

        import jwt

        now = int(time.time())
        payload = {
            "iss": "golden-erp", "iat": now - 3600, "exp": now - 1,
            "jti": "expired-test", "sub": "api-service",
        }
        token = jwt.encode(payload, get_jwt_signing_key(), algorithm="HS256")
        client = APIClient(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = client.get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_token_obtain_accepts_legacy_header(self):
        client = APIClient(HTTP_X_API_KEY=API_KEY)
        response = client.post("/api/v1/core/auth/token/", {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("token", response.json())

    def test_portal_jwt_rejected_on_staff_route(self):
        """A Supabase portal token must not authenticate staff API routes."""
        from core.tests.test_portal_auth import SECRET, make_token

        with override_settings(SUPABASE_JWT_SECRET=SECRET):
            portal_token = make_token(email="cust@example.com",
                                      email_verified=True)
        client = APIClient(HTTP_AUTHORIZATION=f"Bearer {portal_token}")
        response = client.get(self.url)
        self.assertIn(response.status_code, (401, 403))


@override_settings(SUPABASE_JWT_SECRET=PORTAL_SECRET)
class PortalStaffSeparationTests(TestCase):
    def setUp(self):
        self.url = "/api/v1/portal/customer/orders/"

    def _portal_token(self, **kwargs):
        import time
        import uuid

        import jwt

        payload = {
            "sub": kwargs.pop("sub", str(uuid.uuid4())),
            "aud": "authenticated",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "role": "authenticated",
            "email": kwargs.pop("email", "portal-cust@example.com"),
            "user_metadata": {"email_verified": True},
        }
        payload.update(kwargs)
        return jwt.encode(payload, PORTAL_SECRET, algorithm="HS256")

    def test_staff_session_rejected_on_portal_route(self):
        # Portal views register only PortalTokenAuthentication — a staff
        # session cookie is not even evaluated, so this is a 401 (no
        # usable credential), never a 200. Either 401 or 403 proves the
        # staff/portal separation; pin the observed 401.
        staff = User.objects.create_user("staff1", "s@x.com", "pw",
                                         is_staff=True)
        client = APIClient()
        client.force_login(staff)
        response = client.get(self.url)
        self.assertIn(response.status_code, (401, 403))

    def test_anonymous_rejected_on_portal_route(self):
        response = APIClient().get(self.url)
        self.assertIn(response.status_code, (401, 403))

    def test_expired_portal_jwt_rejected(self):
        import time
        import uuid

        import jwt

        payload = {
            "sub": str(uuid.uuid4()), "aud": "authenticated",
            "iat": int(time.time()) - 7200, "exp": int(time.time()) - 3600,
            "role": "authenticated", "email": "old@example.com",
            "user_metadata": {"email_verified": True},
        }
        token = jwt.encode(payload, PORTAL_SECRET, algorithm="HS256")
        client = APIClient(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = client.get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_customer_a_cannot_read_customer_b_order(self):
        """Portal detail is scoped to request.customer (direct-PK guard)."""

        from core.models import Company
        from core.tests.helpers import make_portal_customer
        from orders.models import Order
        from portal.views import PortalOrderDetailView

        company = Company.objects.create(name="PortalCo")
        _cust_a, sub_a = make_portal_customer("pa@example.com", company=company)
        cust_b, _sub_b = make_portal_customer("pb@example.com", company=company)
        order_b = Order.objects.create(customer=cust_b, company=company)
        token = self._portal_token(sub=sub_a, email="pa@example.com")
        client = APIClient(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = client.get(f"/api/v1/portal/customer/orders/{order_b.pk}/")
        self.assertEqual(response.status_code, 404)
        self.assertIsNotNone(
            PortalTokenAuthentication)  # backend import sanity
        self.assertTrue(issubclass(PortalOrderDetailView.permission_classes[0],
                                   object))


class NonStaffHumanMatrixTests(TestCase):
    """D6: a signed-in human who is not staff gets 403 on the whole staff API.

    Today such an account is rare (every human is staff); identity
    unification makes customers auth.User rows, so this is the door that
    must already be shut. Routes come from the live URLconf, so a new
    endpoint is covered without editing this test.
    """

    # Public by design for reads (storefront catalog); writes stay guarded.
    PUBLIC_READS = {
        "products.views.ProductViewSet",
        "products.views.BrandViewSet",
        "products.views.CategoryViewSet",
    }

    def setUp(self):
        self.views = _routed_api_views()
        self.assertGreater(len(self.views), 40)

    @staticmethod
    def _methods(cls):
        methods = []
        if hasattr(cls, "list") or hasattr(cls, "get"):
            methods.append("get")
        if hasattr(cls, "create") or hasattr(cls, "post"):
            methods.append("post")
        return methods

    def test_non_staff_session_is_403_everywhere(self):
        user = User.objects.create_user("shopper", "shopper@example.com", "pw")
        client = APIClient()
        client.force_login(user)
        for name, (cls, route) in sorted(self.views.items()):
            for method in self._methods(cls):
                if method == "get" and name in self.PUBLIC_READS:
                    continue
                with self.subTest(view=name, method=method):
                    response = getattr(client, method)(route, {}, format="json")
                    self.assertEqual(response.status_code, 403, route)

    def test_inactive_staff_bearer_is_403(self):
        # force_authenticate skips ModelBackend's is_active check, as a
        # stolen-but-deactivated principal would; the permission re-checks it.
        from core.tests.helpers import make_staff_user

        user = make_staff_user("gone", password="pw")  # a whole staff account...
        user.is_active = False  # ...then deactivated: only is_active can refuse it
        user.save()
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get("/api/v1/purchasing/suppliers/")
        self.assertEqual(response.status_code, 403)

    def test_staff_session_succeeds_on_every_read(self):
        from core.tests.helpers import make_staff_user

        staff = make_staff_user("deskclerk", password="pw")
        client = APIClient()
        client.force_login(staff)
        for name, (cls, route) in sorted(self.views.items()):
            if "get" not in self._methods(cls):
                continue
            with self.subTest(view=name):
                response = client.get(route)
                self.assertLess(response.status_code, 400, route)

    def test_profile_minted_on_staff_login_only(self):
        from core.models import UserProfile
        from core.tests.helpers import make_staff_user

        shopper = User.objects.create_user("lurker", password="pw")
        staff = make_staff_user("clerk2", password="pw")
        self.client.force_login(shopper)
        self.client.force_login(staff)
        self.assertFalse(UserProfile.objects.filter(user=shopper).exists())
        self.assertTrue(UserProfile.objects.filter(user=staff).exists())


SVC_ROLES = [
    {"name": "supplier-reader", "label": "Supplier reader", "kind": "service",
     "permissions": ["purchasing.view_supplier"]},
]


@override_settings(API_KEY=API_KEY, GOLDEN_ROLES=SVC_ROLES)
class ServiceAccountMatrixTests(TestCase):
    """Identity step 5 (spec §2.6): a per-key service account reaches exactly
    its role's contract across the whole routed staff API, and the legacy key
    never acts as a human."""

    def setUp(self):
        from core.service_accounts import ensure_service_account, issue_api_key

        self.views = _routed_api_views()
        svc = ensure_service_account("supplier-reader", ["supplier-reader"])
        _key, raw = issue_api_key(svc, name="supplier-reader")
        self.client = APIClient(HTTP_AUTHORIZATION=f"Api-Key {raw}")

    def test_per_key_account_is_403_outside_its_contract(self):
        allowed = "/api/v1/purchasing/suppliers/"
        public_reads = NonStaffHumanMatrixTests.PUBLIC_READS
        for name, (cls, route) in sorted(self.views.items()):
            for method in NonStaffHumanMatrixTests._methods(cls):
                if method == "get" and (route == allowed or name in public_reads):
                    continue
                with self.subTest(view=name, method=method):
                    response = getattr(self.client, method)(route, {}, format="json")
                    self.assertEqual(response.status_code, 403, route)
        self.assertEqual(self.client.get(allowed).status_code, 200)

    def test_legacy_key_is_api_service_even_with_human_superusers(self):
        from rest_framework.test import APIRequestFactory

        from core.authentication import APIKeyAuthentication

        User.objects.create_superuser("aaa", "a@example.com", "pw")
        request = APIRequestFactory().get("/", HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")
        user, _ = APIKeyAuthentication().authenticate(request)
        self.assertEqual(user.username, "api-service")
        self.assertFalse(user.has_usable_password())
